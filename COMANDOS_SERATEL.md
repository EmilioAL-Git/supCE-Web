# Comandos conocidos — Protocolo Seratel (referencia rápida)

Todas las tramas siguen el mismo formato:
```
[STX=0x02] [payload...] [CHECKSUM]
CHECKSUM = XOR(STX, payload...) ^ 0x5A
```

Conexión: TCP a `100.113.52.81:3002` (ser2net en la Raspberry).
Solo un cliente conectado a la vez (SupCE o tu script, no ambos).

---

## 1. Lectura de medidas

**Enviar:** `02 BC E4`

**Respuesta (13 bytes):**
```
02 80 [C1] [C2] [C3] [C4] [C5] [C6] [PWR] 00 [VDC] 00 [CHK]
```

| Byte | Significado | Cómo interpretarlo |
|---|---|---|
| C1..C6 | Corriente amplificadores 1-6 | Valor directo en **Amperios** |
| PWR | Potencia directa (FWD) | **Vatios = byte × 25** |
| VDC | Tensión de alimentación | **Voltios = byte ÷ 2** |

**Ejemplo real:**
```
TX: 02 BC E4
RX: 02 80 10 0B 15 10 16 10 52 00 62 00 F0
```
→ Corrientes: 16, 11, 21, 16, 22, 16 A · FWD = 82×25 = 2050 W · VDC = 98÷2 = 49 V

**Python:**
```python
import socket

def checksum(payload):
    x = 0x02
    for b in payload:
        x ^= b
    return x ^ 0x5A

s = socket.create_connection(("100.113.52.81", 3002), timeout=2)
s.sendall(bytes([0x02, 0xBC, 0xE4]))
resp = s.recv(64)

currents = list(resp[2:8])
fwd_power_w = resp[8] * 25
vdc_volts = resp[10] / 2
print(currents, fwd_power_w, vdc_volts)
```

### Bytes 9 y 11 (los "00" fijos) — hipótesis descartada

Se probó la hipótesis de que fueran un bitmap de alarma de
temperatura/intensidad por amplificador (bit0=ampli1..bit5=ampli6).
**Descartada** con una captura real de un fallo genuino: el
amplificador 5 estuvo marcando **1A constante durante ~5 minutos**
mientras el resto estaba en 17-35A, y aun así estos dos bytes se
quedaron en `0x00` todo el tiempo — igual que el byte general de
alarmas (`B0 E8`), que tampoco reflejó nada.

**Conclusión:** el propio protocolo no manda ninguna alarma por
amplificador. La única señal real de un ampli caído es su corriente
anómalamente baja en la propia trama de medidas — que ya se decodifica
sin más. La app calcula un indicador local (no viene del protocolo):
`corriente < 5A` marca ese amplificador como sospechoso. Confirmado
con el caso real (ampli 5 en 1A, resto en 17-35A → solo el 5 se marca).

---

## 2. Lectura de alarmas

**Enviar:** `02 B0 E8`

**Respuesta (6 bytes):**
```
02 83 [ALM] 00 00 [CHK]
```

| Bit | Alarma | Estado |
|---|---|---|
| `0x01` (bit 0) | RFL Power | ✅ **confirmado** (ver nota) |
| `0x02` (bit 1) | FWD Power | ✅ **confirmado** (correlación real + .exe) |
| `0x04` (bit 2) | EXC. Power | ✅ **confirmado** (ver nota) |
| `0x08` (bit 3) | Temp. Combiner | ✅ **confirmado** (ver nota) |
| `0x10` (bit 4) | Permanent Fail | ✅ **confirmado** (ver nota) |
| `0x20` (bit 5) | Ext. Interlock | ✅ **confirmado** (Reset real + .exe) |
| `0x40` (bit 6) | — (sin usar) | no existe indicador para este bit |
| `0x80` (bit 7) | Control Unit Power | ✅ confirmado qué bit es; polaridad invertida sin terminar de confirmar |

**Confirmado por ingeniería inversa estática de `SUPCE.EXE`** (ver
HALLAZGOS.md §6.5 para el detalle completo): el recurso de formulario
compilado en el binario tiene 7 `TShape` (los LEDs de alarma) llamados
literalmente `g0` a `g7` (sin `g6` — ese bit no tiene indicador en el
panel), y el número coincide exactamente con el índice de bit de cada
alarma en el orden del panel. Esto corrige la hipótesis anterior, que
tenía EXC. Power y RFL Power intercambiados (se había asumido bit0..bit4
en orden simple de fila, sin saber que el bit 6 se salta).

Esta numeración coincide de forma independiente con los dos bits que ya
teníamos confirmados por captura real: FWD Power = bit 1 (correlación
con potencia alta) y Ext. Interlock = bit 5 (Reset real) — los nombres
de los shapes `g1` y `g5` encajan exactamente.

**Control Unit Power (bit 7)**: en todas las capturas reales que
tenemos vale 1, con el indicador en verde en pantalla siempre. Como
`g7` es el shape correcto para esa alarma (confirmado por su nombre),
la única explicación consistente es que su polaridad esté invertida
(1 = normal/OK, 0 = alarma) — pero nunca hemos visto ese bit a 0, así
que la *polaridad* (no el bit) queda como hipótesis razonable sin
confirmar del todo.

**Ejemplo real (con Ext. Interlock activa):**
```
TX: 02 B0 E8
RX: 02 83 E0 00 00 3B      → ALM=0xE0=11100000 → interlock ON
```
**Tras hacer Reset:**
```
RX: 02 83 C0 00 00 1B      → ALM=0xC0=11000000 → interlock OFF
```

**Python:**
```python
s.sendall(bytes([0x02, 0xB0, 0xE8]))
resp = s.recv(64)
alm_byte = resp[2]
ext_interlock = bool(alm_byte & 0x20)
exc_power = bool(alm_byte & 0x01)      # sin confirmar
fwd_power = bool(alm_byte & 0x02)      # sin confirmar
rfl_power = bool(alm_byte & 0x04)      # sin confirmar
temp_combiner = bool(alm_byte & 0x08)  # sin confirmar
permanent_fail = bool(alm_byte & 0x10) # sin confirmar
print(f"{alm_byte:08b}", "interlock activo:", ext_interlock)
```

---

## 3. Reset de alarmas

**Enviar:** `02 EC B4`

**Respuesta (ACK, 3 bytes):** `02 73 2B`

Limpia alarmas latcheadas (confirmado con Ext. Interlock real: pasó de
rojo a verde en SupCE justo tras mandar este comando).

**Python:**
```python
s.sendall(bytes([0x02, 0xEC, 0xB4]))
ack = s.recv(64)
print("ack:", ack.hex())   # esperado: 02732b
```

---

## 4. Control de potencia — subir / bajar

**Bajar potencia:** `02 C0 98`
**Subir potencia:** `02 C3 9B`

Cada pulsación cambia la potencia FWD en pasos de **50 W** (confirmado
en real: se probó subir/bajar repetidamente contra el amplificador
real de esRadio y la lectura de `BC E4` se movió en incrementos de 50 W
por cada envío, en la dirección esperada según el botón).

No se ha registrado todavía la trama de respuesta exacta (longitud y
contenido del ACK) — pendiente de documentar con una captura dedicada.
Tampoco se conoce el límite superior/inferior de potencia ni qué pasa
al enviar el comando en ese límite (¿se ignora? ¿ACK de error?).

**Python:**
```python
s.sendall(bytes([0x02, 0xC3, 0x9B]))  # subir 50 W
# s.sendall(bytes([0x02, 0xC0, 0x98]))  # bajar 50 W
resp = s.recv(64)
print(resp.hex())
```

---

## 5. Control de potencia — fijar a un % del máximo

**Confirmado con captura real** (`supce_subirbajar.pcap`, sesión donde
se pulsó 90→80→70→60→50→40% con pausas de ~10s entre cada uno).

**Fórmula:** `comando = 0x90 + (porcentaje ÷ 10)`

| % | Comando (hex) |
|---|---|
| 40% | `02 94 CC` |
| 50% | `02 95 CD` |
| 60% | `02 96 CE` |
| 70% | `02 97 CF` |
| 80% | `02 98 C0` |
| 90% | `02 99 C1` |

**Respuesta (ACK, 3 bytes):** siempre `02 50 08`, sea cual sea el
porcentaje — es un ACK genérico de "comando recibido", no confirma el
valor.

Tras el comando, el amplificador **no salta** al valor de golpe: la
potencia FWD (`BC E4`) va subiendo/bajando en rampa hacia el objetivo,
en pasos de 25-75W cada segundo aprox., hasta estabilizarse.

Solo se han confirmado estos 6 valores (40-90% en pasos de 10, que son
los botones que trae SupCE); no se ha probado si otros porcentajes
(ej. 45%) funcionan con la misma fórmula.

### Botón "Máxima"

**Confirmado con una segunda captura** (misma sesión: conectar, esperar
unos minutos, subir a 90%, y luego pulsar "Máxima").

**Comando:** `02 90 C8`

No sigue la fórmula aritmética de arriba (que empieza en `0x94` para
40%) — es un botón dedicado con su propio código `0x90`, con el mismo
ACK genérico `02 50 08`.

### FWD Power — causa del error visto al pedir 90%/Máxima

En esta segunda captura se confirmó por correlación qué dispara el
error de "FWD Power" que ya habíamos visto antes en pantalla: al subir
la potencia hacia el objetivo de 90%, el **byte de alarmas cambió de
`0xC0` a `0xC2`** justo cuando la lectura FWD pasaba de ~2600W a
~2700W — es decir, el **bit 1 (`0x02`) = alarma "FWD Power"**, ver
tabla de alarmas más abajo.

Una vez disparada esta alarma, la potencia se quedó **clavada en
3650W** el resto de la captura (~200s más), sin moverse ni con el
comando de 90% ni con "Máxima" enviados después — todo apunta a que es
una protección real del amplificador que limita la salida una vez
activada, no un simple indicador cosmético. No se probó "Reset" en
esta sesión para ver si la libera; pendiente de confirmar.

---

## 6. Stop

**Confirmado con captura real** (`supce_stop.pcap`), pulsado dos veces,
ambas con respuesta limpia e inmediata (~80-160ms):

**Comando:** `02 B3 EB`
**Respuesta (ACK, 3 bytes):** `02 70 28`

Tras cada pulsación, el polling normal (`B0 E8`/`BC E4`) siguió
funcionando sin problemas — el propio Stop no dio ningún síntoma de
fallo de comunicación.

### El error "El equipo remoto no responde" — no es un fallo real de Stop

En esa misma sesión, ~6s después del segundo Stop, se envió `02 C0 98`
(Disminuir potencia). El amplificador respondió rápido (81ms) con
`02 64 3C`. El cliente reintentó el mismo comando dos veces más, hubo
silencios anómalos de varios segundos (normalmente hay tráfico cada
<1s), y SupCE acabó cortando la conexión con un RST — mostrando el
error "El equipo remoto no responde" al usuario.

**Corrección (18/08, tras analizar `SUPCE.EXE` con ingeniería inversa
estática — ver §7):** en un primer momento pensé que `0x64` era un
código de error/rechazo. **Es incorrecto.** Desensamblando la rutina
de SupCE que procesa el segundo byte de cada respuesta, `0x64` cae
exactamente en la misma rama de "ACK válido" que `0x50`, `0x70` y
`0x73` (los ACK de preset/máxima, stop y reset, todos confirmados en
real). Es decir: **`0x64` es simplemente el ACK normal y esperado de
"Bajar potencia" (`C0`)**, nada de rechazo.

Entonces, ¿por qué el error? No lo sabemos con certeza — el propio
comando y su respuesta fueron correctos. Lo más probable es un
problema de comunicación a otro nivel (ej. algún hueco/retraso en
ser2net o en la red Tailscale en ese momento concreto), no algo
relacionado con el significado del comando. Sin nueva evidencia, se
deja como incidente puntual sin explicación firme.

**Python:**
```python
def set_power_percent(s, percent):
    cmd = 0x90 + percent // 10
    frame = bytes([0x02, cmd, checksum([cmd])])
    s.sendall(frame)
    return s.recv(64)

set_power_percent(s, 70)   # -> 02 97 CF, respuesta esperada: 02 50 08
```

---

## 7. Ingeniería inversa estática del binario SUPCE.EXE

El usuario proporcionó el instalador original (`supce14/`, con
`SUPCE.EXE` de 1.3MB). Es un binario **Delphi nativo** (usa las
librerías CPort y RX, sin runtime de VB6 ni .NET), lo que permite
desensamblarlo directamente sin capa de intérprete — a diferencia de
un pcap, aquí se ve la lógica real del programa, no solo lo que
decidió transmitir en una sesión concreta.

### 7.1 Función `EnviarComando(cmd: byte)` localizada

Se encontró la única instrucción `xor ..., 0x5A` de todo el binario
(en `0x40364D`), que resultó ser el final de la función que arma cada
trama:

```
mov byte ptr [frame+0], 0x02        ; STX
mov al, [parámetro]                 ; cmd_byte recibido
mov byte ptr [frame+1], al
mov dl, [frame+0]
xor dl, al                          ; STX ^ cmd
xor dl, 0x5A                        ; ^ 0x5A = CHECKSUM
mov byte ptr [frame+2], dl
; ... envía los 3 bytes por el puerto serie
```

Coincide exactamente con la fórmula que ya teníamos, ahora confirmada
directamente desde el código fuente compilado, no solo por inducción
desde capturas.

### 7.2 Todos los llamantes de esa función — comando nuevo encontrado

Se localizaron **todas** las llamadas a esa función en el binario (18
en total, agrupadas en dos bloques casi idénticos de un "dispatcher"
con tabla de saltos — probablemente los botones de "Control de
potencia" y "Pulsadores de control remoto"). Los bytes de comando
usados son exactamente:

```
B0, E0, B3, EC, C3, C0, 90, (0x94 + índice_preset)
```

Todos coinciden con lo ya confirmado por captura — **excepto `0xE0`**,
que no aparece en ninguno de nuestros pcaps. En el código está
colocado justo entre `B0` (leer alarmas) y `B3` (Stop), en una
secuencia que por orden de aparición encaja con Start/Stop/Reset como
grupo de botones contiguos.

**Hipótesis fuerte, no confirmada en vivo:** `02 E0 B8` = **Start**.

⚠️ A diferencia de Stop, probar esto en real pone el transmisor **al
aire**. No lo he añadido a la app como botón activo — si se quiere
confirmar, debe hacerse deliberadamente, con el equipo en un estado
seguro para transmitir, y a ser posible capturando con tcpdump para
verificar la respuesta.

### 7.3 Tabla completa de ACKs — y corrección del error sobre `0x64`

Se localizó la rutina que interpreta el segundo byte de cada
respuesta (justo tras el STX). Es una cadena de comparaciones
(`cmp`/`sub`/`je`) clásica de un `case` de Pascal compilado. Simulando
esa lógica exactamente byte a byte, los únicos valores que el
programa reconoce como "respuesta corta válida" son:

| Byte ACK | Estado en SupCE | Comando al que pertenece |
|---|---|---|
| `0x50` | OK | Preset de potencia (`94`-`99`) y Máxima (`90`) — confirmado en real |
| `0x62` | OK | **Sin asignar** — candidato a Subir (`C3`) o Start (`E0`) |
| `0x64` | OK | Bajar potencia (`C0`) — confirmado en real |
| `0x70` | OK | Stop (`B3`) — confirmado en real |
| `0x73` | OK | Reset (`EC`) — confirmado en real |
| `0x7C` | OK | **Sin asignar** — candidato a Subir (`C3`) o Start (`E0`) |
| `0x80` | Inicio de respuesta larga (medidas) | `BC E4` |
| `0x83` | Inicio de respuesta larga (alarmas) | `B0 E8` |

Todos estos bytes llevan al **mismo** estado interno "OK" — no hay
ningún código de error/rechazo distinto en esta rutina. Cualquier otro
byte simplemente reinicia el parser a la espera del siguiente byte
(re-sincronización silenciosa, no un error explícito).

**Esto corrige lo que documentamos antes**: `0x64` no es un código de
rechazo por "equipo parado" — es sencillamente el ACK normal y
esperado de `C0`. La causa real del error "El equipo remoto no
responde" visto en la captura de Stop sigue sin explicación firme
(ver §6).

Quedan sin asignar `0x62` y `0x7C`, que por descarte deben ser los
ACK de Subir (`C3`) y de Start (`E0`) — no se puede saber cuál es cuál
sin una captura en vivo de cada uno por separado.

---

## Tabla resumen para copiar/pegar

| # | Comando (hex) | Función | Longitud respuesta | Estado |
|---|---|---|---|---|
| 1 | `02 BC E4` | Leer medidas (corrientes, FWD power, VDC) | 13 bytes | ✅ Confirmado |
| 2 | `02 B0 E8` | Leer alarmas | 6 bytes | ✅ Confirmado (parcial: solo bit interlock) |
| 3 | `02 EC B4` | Reset de alarmas | 3 bytes (ACK `02 73 2B`) | ✅ Confirmado |
| 4 | `02 C0 98` | Bajar potencia (−50 W) | 3 bytes (ACK `02 64 3C`) | ✅ Confirmado — ACK normal, no error (ver §7.3) |
| 5 | `02 C3 9B` | Subir potencia (+50 W) | 3 bytes (ACK `02 62 ..` o `02 7C ..`, sin confirmar cuál) | ✅ Confirmado (funcional, en real) |
| 6 | `02 90+n/10 CHK` | Fijar potencia a n% (n=40..90) | 3 bytes (ACK `02 50 08`) | ✅ Confirmado por captura real |
| 7 | `02 90 C8` | Potencia máxima | 3 bytes (ACK `02 50 08`) | ✅ Confirmado por captura real |
| 8 | `02 B3 EB` | Stop | 3 bytes (ACK `02 70 28`) | ✅ Confirmado por captura real |
| 9 | `02 E0 B8` | Start (hipótesis) | 3 bytes (ACK sin confirmar) | ⚠️ Encontrado en el binario, **nunca probado en real** |

## Comandos NO confirmados todavía

- **Start** (`0xE0`): hipótesis fuerte por análisis estático del
  `.exe`, pendiente de confirmar con una captura real deliberada.
- No se ha encontrado en el binario ningún comando de protocolo
  adicional más allá de los 9 de la tabla — el listado de llamadas a
  `EnviarComando()` (§7.2) parece ser exhaustivo para los comandos
  simples de un byte. La lectura de medidas (`BC E4`) usa una rutina
  de envío distinta que no se localizó en el barrido estático, pero
  ya está confirmada de sobra por captura real.
