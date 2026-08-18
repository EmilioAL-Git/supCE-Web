import asyncio
import os
import logging
import secrets
from collections import deque
from contextlib import asynccontextmanager

from fastapi import Body, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from seratel_protocol import SeratelAsyncClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("seratel_webapp")

DEFAULT_HOST = os.environ.get("SER2NET_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SER2NET_PORT", "3002"))
DEFAULT_SERIAL_DEVICE = os.environ.get("SERIAL_DEVICE", "/dev/ttyUSB0")
DEFAULT_SERIAL_BAUDRATE = int(os.environ.get("SERIAL_BAUDRATE", "9600"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
HISTORY_MAX_POINTS = int(os.environ.get("HISTORY_MAX_POINTS", "2000"))

API_KEY = os.environ.get("API_KEY")
if not API_KEY:
    raise RuntimeError(
        "Falta la variable de entorno API_KEY. Genera una (ej. "
        "`python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"`) "
        "y ponla en docker-compose.yml antes de arrancar."
    )

state = {
    "connected": False,
    "connecting": False,
    "transport": None,
    "host": None,
    "port": None,
    "device": None,
    "measurements": None,
    "alarms": None,
    "last_error": None,
}

history: deque[dict] = deque(maxlen=HISTORY_MAX_POINTS)

# Blob de estado libre para el bot de Telegram (n8n): qué mensaje del
# dashboard hay que editar, alarmas ya notificadas, confirmaciones
# pendientes, etc. Vive aquí porque el staticData de workflow de n8n no
# se puede dar por persistente en todas las instancias (task runner
# externo) — esta API sí es un proceso único y estable. Se pierde si el
# contenedor se reinicia, igual que `history`.
bot_state: dict = {}

client: SeratelAsyncClient | None = None
poller_task: asyncio.Task | None = None


async def require_api_key(x_api_key: str | None = Header(default=None)):
    # Comparación en tiempo constante para no filtrar la key por timing.
    if not x_api_key or not secrets.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="API key inválida o ausente (header X-API-Key)")


def _record_history():
    if state["measurements"] is not None or state["alarms"] is not None:
        history.append({
            "timestamp": (state["measurements"] or state["alarms"])["timestamp"],
            "measurements": state["measurements"],
            "alarms": state["alarms"],
        })


class ConnectRequest(BaseModel):
    # Modo "tcp" (vía ser2net, típicamente en red/Tailscale): host + port.
    host: str | None = Field(default=None, min_length=1, max_length=255)
    port: int | None = Field(default=None, gt=0, le=65535)
    # Modo "serial" (puerto serie directo en esta misma máquina): device.
    device: str | None = Field(default=None, min_length=1, max_length=255)
    baudrate: int = Field(default=DEFAULT_SERIAL_BAUDRATE, gt=0)

    def build_client(self) -> SeratelAsyncClient:
        if self.device:
            return SeratelAsyncClient.via_serial(self.device, baudrate=self.baudrate)
        if self.host and self.port:
            return SeratelAsyncClient.via_tcp(self.host, self.port)
        raise ValueError("Indica 'host'+'port' (modo tcp) o 'device' (modo serial)")


async def poller_loop():
    global client, poller_task
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL)
            try:
                meas = await client.read_measurements()
                alarms = await client.read_alarms()
                state["measurements"] = meas
                state["alarms"] = alarms
                state["last_error"] = None
                _record_history()
            except Exception as e:
                log.warning("Conexión perdida: %s", e)
                state["connected"] = False
                state["last_error"] = str(e)
                break
    finally:
        if client:
            await client.close()
            client = None
        poller_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if poller_task:
        poller_task.cancel()
    if client:
        await client.close()


app = FastAPI(
    title="Seratel Monitor API",
    description=(
        "API de monitorización y control del amplificador FM Seratel de "
        "esRadio Albacete. Todos los endpoints bajo /api requieren la "
        "cabecera `X-API-Key`."
    ),
    version="1.1.0",
    lifespan=lifespan,
)
api = FastAPI(dependencies=[Depends(require_api_key)])
app.mount("/api", api)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(os.path.dirname(__file__), "templates", "index.html"), encoding="utf-8") as f:
        html = f.read()
    return html.replace("%%API_KEY%%", API_KEY)


@api.get("/status", tags=["info"])
async def api_status():
    """Estado completo: conexión, última lectura de medidas y alarmas."""
    return {
        **state,
        "default_host": DEFAULT_HOST,
        "default_port": DEFAULT_PORT,
        "default_device": DEFAULT_SERIAL_DEVICE,
        "default_baudrate": DEFAULT_SERIAL_BAUDRATE,
    }


@api.get("/measurements", tags=["info"])
async def api_measurements():
    """Última lectura de medidas (corrientes, potencia FWD, VDC)."""
    if state["measurements"] is None:
        raise HTTPException(status_code=503, detail="Sin lecturas todavía (no conectado o primer ciclo pendiente)")
    return state["measurements"]


@api.get("/alarms", tags=["info"])
async def api_alarms():
    """Última lectura de alarmas generales."""
    if state["alarms"] is None:
        raise HTTPException(status_code=503, detail="Sin lecturas todavía (no conectado o primer ciclo pendiente)")
    return state["alarms"]


@api.get("/history", tags=["info"])
async def api_history(limit: int = 100):
    """Histórico de lecturas (medidas + alarmas) en memoria, más recientes al final."""
    limit = max(1, min(limit, HISTORY_MAX_POINTS))
    return {"count": len(history), "max_points": HISTORY_MAX_POINTS, "points": list(history)[-limit:]}


@api.get("/bot-state", tags=["info"])
async def api_get_bot_state():
    """Blob de estado libre (JSON) para integraciones externas (ej. el workflow n8n del bot de Telegram)."""
    return bot_state


@api.put("/bot-state", tags=["info"])
async def api_put_bot_state(payload: dict = Body(...)):
    """Sustituye por completo el blob de estado. El consumidor es responsable de hacer read-modify-write."""
    global bot_state
    bot_state = payload
    return {"ok": True}


@api.get("/summary", tags=["info"])
async def api_summary():
    """Resumen en lenguaje natural del estado actual, pensado para un LLM u otro consumidor automático."""
    if not state["connected"]:
        detail = f" ({state['last_error']})" if state["last_error"] else ""
        return {"text": f"Sin conexión con el amplificador{detail}."}

    m = state["measurements"]
    a = state["alarms"]
    parts = []

    if m:
        parts.append(f"Potencia FWD {m['fwd_power_w']} W, tensión DC {m['vdc_volts']} V.")
        amps_txt = ", ".join(f"ampli {i + 1}: {c} A" for i, c in enumerate(m["idc_amps"]))
        parts.append(f"Corrientes ({amps_txt}).")
        caidos = [i + 1 for i, alm in enumerate(m.get("current_alarms", [])) if alm]
        if caidos:
            parts.append(f"Corriente anómala (posible ampli caído) en: {', '.join(map(str, caidos))}.")
    else:
        parts.append("Sin lectura de medidas todavía.")

    if a:
        activas = [
            nombre for nombre, activa in [
                ("RFL Power", a["rfl_power_alarm"]),
                ("FWD Power", a["fwd_power_alarm"]),
                ("EXC. Power", a["exc_power_alarm"]),
                ("Temp. Combiner", a["temp_combiner_alarm"]),
                ("Permanent Fail", a["permanent_fail_alarm"]),
                ("Ext. Interlock", a["ext_interlock_alarm"]),
                ("Control Unit Power", a["control_unit_power_alarm"]),
            ] if activa
        ]
        parts.append(f"Alarmas activas: {', '.join(activas)}." if activas else "Sin alarmas activas.")

    return {"text": " ".join(parts), "connected": True, "host": state["host"], "port": state["port"]}


@api.post("/connect")
async def api_connect(req: ConnectRequest):
    global client, poller_task

    if state["connected"] or state["connecting"]:
        raise HTTPException(status_code=400, detail="Ya hay una conexión activa o en curso")

    state["connecting"] = True
    state["last_error"] = None
    try:
        candidate = req.build_client()
    except ValueError as e:
        state["connecting"] = False
        raise HTTPException(status_code=400, detail=str(e))

    try:
        await candidate.connect()
        # Confirmación real: no basta con abrir el enlace, hay que ver que
        # el amplificador responde al protocolo Seratel con tramas válidas.
        alarms = await candidate.read_alarms()
        meas = await candidate.read_measurements()
    except Exception as e:
        await candidate.close()
        state["connecting"] = False
        state["last_error"] = str(e)
        log.warning("Fallo al conectar a %s (%s) -> %s", candidate.description, candidate.transport, e)
        raise HTTPException(status_code=502, detail=f"No se pudo confirmar conexión: {e}")

    client = candidate
    state["transport"] = candidate.transport
    state["host"] = candidate.host
    state["port"] = candidate.port
    state["device"] = candidate.device
    state["measurements"] = meas
    state["alarms"] = alarms
    state["connected"] = True
    state["connecting"] = False
    _record_history()
    poller_task = asyncio.create_task(poller_loop())
    log.info("Conectado y confirmado a %s (%s)", candidate.description, candidate.transport)
    return {"ok": True, "transport": candidate.transport, "host": candidate.host, "port": candidate.port, "device": candidate.device}


@api.post("/disconnect")
async def api_disconnect():
    global client, poller_task

    if poller_task:
        poller_task.cancel()
        poller_task = None
    if client:
        await client.close()
        client = None

    state["connected"] = False
    state["connecting"] = False
    state["transport"] = None
    state["host"] = None
    state["port"] = None
    state["device"] = None
    state["last_error"] = None
    log.info("Desconectado")
    return {"ok": True}


@api.post("/reset")
async def api_reset():
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        ack = await client.reset_alarms()
        return {"ok": True, "ack_hex": ack.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/power/up")
async def api_power_up():
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        raw = await client.power_up()
        return {"ok": True, "raw_hex": raw.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/power/down")
async def api_power_down():
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        raw = await client.power_down()
        return {"ok": True, "raw_hex": raw.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


ALLOWED_POWER_PRESETS = {40, 50, 60, 70, 80, 90}


class PowerPresetRequest(BaseModel):
    percent: int


@api.post("/power/set")
async def api_power_set(req: PowerPresetRequest):
    if req.percent not in ALLOWED_POWER_PRESETS:
        raise HTTPException(status_code=400, detail=f"Porcentaje no soportado: {req.percent}")
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        raw = await client.set_power_percent(req.percent)
        return {"ok": True, "percent": req.percent, "raw_hex": raw.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/power/max")
async def api_power_max():
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        raw = await client.power_max()
        return {"ok": True, "raw_hex": raw.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/stop")
async def api_stop():
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        raw = await client.stop()
        return {"ok": True, "raw_hex": raw.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@api.post("/start")
async def api_start():
    if client is None or not client.is_connected:
        raise HTTPException(status_code=503, detail="No conectado al amplificador")
    try:
        raw = await client.start()
        return {"ok": True, "raw_hex": raw.hex()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
