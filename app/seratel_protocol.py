"""
Protocolo Seratel (amplificador FM) - descifrado por ingeniería inversa
a partir de capturas reales del diálogo SupCE <-> amplificador.

Formato de trama:
    [STX=0x02] [payload...] [CHECKSUM]
    CHECKSUM = XOR(STX, payload...) ^ 0x5A

Comandos confirmados:
    B0 E8  -> lectura de alarmas generales (respuesta 6 bytes: 02 83 [ALM] 00 00 CHK)
    BC E4  -> lectura de medidas (respuesta 13 bytes):
              02 80 [C1][C2][C3][C4][C5][C6] [PWR] 00 [VDC] 00 CHK
              C1..C6 = corriente de cada ampli en Amperios (valor directo)
              PWR    = potencia FWD en vatios = byte * 25
              VDC    = tensión DC en voltios  = byte / 2
    EC B4  -> Reset de alarmas (respuesta ACK 3 bytes)

Pendiente de confirmar con más capturas: RFL Power, resto de bits de
alarma individuales, comandos Start/Stop y control de potencia.
"""

import asyncio
import datetime

import serial_asyncio

STX = 0x02


def checksum(payload: bytes) -> int:
    x = STX
    for b in payload:
        x ^= b
    return x ^ 0x5A


def build_command(cmd_byte: int) -> bytes:
    chk = checksum(bytes([cmd_byte]))
    return bytes([STX, cmd_byte, chk])


def verify_checksum(frame: bytes) -> bool:
    if len(frame) < 2 or frame[0] != STX:
        return False
    body = frame[1:-1]
    return checksum(body) == frame[-1]


class SeratelAsyncClient:
    """Cliente asíncrono para usar dentro de FastAPI (asyncio).

    Dos transportes posibles hacia el mismo protocolo Seratel:
    - "tcp": vía ser2net (host:puerto), como usa la app legacy en red.
    - "serial": puerto serie directo en la máquina donde corre esta app
      (p.ej. /dev/ttyUSB0), sin pasar por ser2net.

    AVISO: el amplificador solo admite un maestro a la vez en el enlace
    RS232. Si ser2net (para SupCE) y este cliente en modo "serial" abren
    el mismo dispositivo físico a la vez, las tramas de ambos pueden
    mezclarse o perderse — no hay arbitraje automático entre los dos.
    Ver HALLAZGOS.md.
    """

    def __init__(self, transport: str = "tcp", *, host: str | None = None, port: int | None = None,
                 device: str | None = None, baudrate: int = 9600, timeout: float = 2.0):
        if transport not in ("tcp", "serial"):
            raise ValueError(f"Transporte desconocido: {transport}")
        if transport == "tcp" and not (host and port):
            raise ValueError("Transporte 'tcp' requiere host y port")
        if transport == "serial" and not device:
            raise ValueError("Transporte 'serial' requiere device")

        self.transport = transport
        self.host = host
        self.port = port
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self.reader = None
        self.writer = None
        self._lock = asyncio.Lock()

    @classmethod
    def via_tcp(cls, host: str, port: int, timeout: float = 2.0) -> "SeratelAsyncClient":
        return cls("tcp", host=host, port=port, timeout=timeout)

    @classmethod
    def via_serial(cls, device: str, baudrate: int = 9600, timeout: float = 2.0) -> "SeratelAsyncClient":
        return cls("serial", device=device, baudrate=baudrate, timeout=timeout)

    @property
    def description(self) -> str:
        return f"{self.host}:{self.port}" if self.transport == "tcp" else f"{self.device}@{self.baudrate}"

    async def connect(self):
        if self.transport == "tcp":
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=self.timeout
            )
        else:
            self.reader, self.writer = await asyncio.wait_for(
                serial_asyncio.open_serial_connection(
                    url=self.device, baudrate=self.baudrate, bytesize=8, parity="N", stopbits=1
                ),
                timeout=self.timeout,
            )

    async def close(self):
        if self.writer:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None

    @property
    def is_connected(self) -> bool:
        return self.writer is not None and not self.writer.is_closing()

    async def _send_and_read(self, cmd_byte: int, expected_len: int, retries: int = 3) -> bytes:
        frame = build_command(cmd_byte)
        last_exc = None
        async with self._lock:
            for _ in range(retries):
                try:
                    self.writer.write(frame)
                    await self.writer.drain()
                    data = b""
                    try:
                        while len(data) < expected_len:
                            chunk = await asyncio.wait_for(
                                self.reader.read(64), timeout=self.timeout
                            )
                            if not chunk:
                                break
                            data += chunk
                    except asyncio.TimeoutError:
                        pass
                    if len(data) >= expected_len:
                        candidate = data[:expected_len]
                        if verify_checksum(candidate):
                            return candidate
                    last_exc = ValueError(f"Respuesta incompleta/checksum inválido: {data.hex()}")
                except Exception as e:
                    last_exc = e
                await asyncio.sleep(0.1)
        raise RuntimeError(f"Fallo tras {retries} intentos en cmd {cmd_byte:02X}: {last_exc}")

    # Umbral heurístico (no viene del protocolo): en todas las capturas
    # reales, un amplificador operativo nunca ha bajado de ~10A, mientras
    # que uno caído se ha visto en 1A con el resto en 17-35A. Se usa como
    # aproximación hasta tener más datos de otros tipos de fallo (ej.
    # sobrecorriente), ver COMANDOS_SERATEL.md.
    LOW_CURRENT_THRESHOLD_A = 5

    async def read_measurements(self) -> dict:
        raw = await self._send_and_read(0xBC, expected_len=13)
        currents = list(raw[2:8])
        pwr_byte = raw[8]
        vdc_byte = raw[10]
        # Bytes 9 y 11: siempre 00 en todas las capturas, incluida una con
        # el amplificador 5 realmente averiado (corriente en 1A mientras
        # el resto estaba en 17-35A) — se descarta la hipótesis anterior
        # de que fueran un bitmap de alarma por amplificador.
        return {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "idc_amps": currents,
            "fwd_power_w": pwr_byte * 25,
            "vdc_volts": vdc_byte / 2,
            "current_alarms": [c < self.LOW_CURRENT_THRESHOLD_A for c in currents],
            "raw_hex": raw.hex(),
        }

    async def read_alarms(self) -> dict:
        raw = await self._send_and_read(0xB0, expected_len=6)
        alm_byte = raw[2]
        # Mapa de bits confirmado por ingeniería inversa estática de
        # SUPCE.EXE: los TShape del panel "Alarmas Generales" en el
        # recurso de formulario se llaman g0..g7 (g6 no existe, sin
        # usar), y el número coincide con el índice de bit. Confirmado
        # de forma independiente con captura real para bit1 (FWD Power,
        # correlación con potencia alta) y bit5 (Ext. Interlock, Reset
        # real) — encajan perfectamente con esta numeración.
        # bit7 (Control Unit Power) siempre ha valido 1 en toda captura
        # real que tenemos, con el indicador en verde en pantalla, así
        # que se asume polaridad invertida (1=OK) sin confirmar del
        # todo (nunca hemos visto ese bit a 0).
        return {
            "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
            "alarm_byte": alm_byte,
            "alarm_bits": f"{alm_byte:08b}",
            "rfl_power_alarm": bool(alm_byte & 0x01),
            "fwd_power_alarm": bool(alm_byte & 0x02),
            "exc_power_alarm": bool(alm_byte & 0x04),
            "temp_combiner_alarm": bool(alm_byte & 0x08),
            "permanent_fail_alarm": bool(alm_byte & 0x10),
            "ext_interlock_alarm": bool(alm_byte & 0x20),
            "control_unit_power_alarm": not bool(alm_byte & 0x80),
            "raw_hex": raw.hex(),
        }

    async def reset_alarms(self) -> bytes:
        return await self._send_and_read(0xEC, expected_len=3)

    async def send_raw_command(self, cmd_byte: int, timeout: float = 1.5) -> bytes:
        """Envía un comando de un solo byte y devuelve lo que responda el
        amplificador, sin asumir longitud fija. Se usa para los comandos de
        potencia (C0/C3), que aún no tienen su trama de respuesta confirmada."""
        frame = build_command(cmd_byte)
        async with self._lock:
            self.writer.write(frame)
            await self.writer.drain()
            data = b""
            try:
                while True:
                    chunk = await asyncio.wait_for(self.reader.read(64), timeout=timeout)
                    if not chunk:
                        break
                    data += chunk
            except asyncio.TimeoutError:
                pass
        if not data:
            # Timeout real sin ninguna respuesta - el mismo caso que en SupCE
            # da "El equipo remoto no responde". Distinto del caso 0x64, que
            # sí responde pero rechaza el comando.
            raise RuntimeError("El equipo remoto no responde (timeout esperando ACK)")
        return data

    async def power_up(self) -> bytes:
        return await self.send_raw_command(0xC3)

    async def power_down(self) -> bytes:
        return await self.send_raw_command(0xC0)

    async def set_power_percent(self, percent: int) -> bytes:
        """Fija la potencia a un % del máximo (confirmado con captura real:
        cmd_byte = 0x90 + percent/10, ej. 90% -> 0x99, 40% -> 0x94).
        Respuesta observada siempre: ACK 02 50 08, sea cual sea el %."""
        cmd_byte = 0x90 + (percent // 10)
        return await self.send_raw_command(cmd_byte)

    async def power_max(self) -> bytes:
        """Botón "Máxima" de SupCE. Confirmado con captura real: comando
        0x90, distinto de la fórmula aritmética de los presets (que
        empieza en 0x94 para 40%). Mismo ACK genérico 02 50 08."""
        return await self.send_raw_command(0x90)

    async def stop(self) -> bytes:
        """Botón "Stop" de SupCE. Confirmado con captura real: comando
        0xB3, ACK 02 70 28. Respondió limpio y rápido las dos veces
        capturadas (sin relación con el error "equipo remoto no
        responde" que apareció después, al usar Disminuir con el
        equipo ya parado)."""
        return await self.send_raw_command(0xB3)

    async def start(self) -> bytes:
        """Botón "Start" de SupCE. HIPÓTESIS SIN CONFIRMAR EN VIVO:
        encontrado por ingeniería inversa estática de SUPCE.EXE (todas
        las llamadas a la función que arma tramas usan los bytes B0,
        E0, B3, EC, C3, C0, 90 y presets — E0 es el único sin capturar
        en tráfico real, y aparece en el código justo entre "leer
        alarmas" y Stop). Ver HALLAZGOS.md §6.2. Pone el transmisor al
        aire de verdad si el amplificador lo acepta."""
        return await self.send_raw_command(0xE0)
