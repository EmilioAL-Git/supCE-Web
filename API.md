# API — Seratel Monitor

Referencia de la API HTTP/JSON que expone la app (`app/main.py`, FastAPI).
Para el protocolo serie con el amplificador en sí (tramas, checksums,
comandos confirmados), ver [COMANDOS_SERATEL.md](COMANDOS_SERATEL.md) y
[HALLAZGOS.md](HALLAZGOS.md) — este documento es solo la capa HTTP por
encima.

## Base URL y autenticación

```
http://<host>:8000/api
```

En producción (Raspberry), `<host>` es la IP Tailscale del equipo.
Todos los endpoints bajo `/api` requieren la cabecera:

```
X-API-Key: <valor de API_KEY en .env>
```

Sin la cabecera (o con una key incorrecta) responden `401`. La página
web (`/`) no requiere autenticación para cargarse — el servidor le
inyecta la key automáticamente al servirla, ya que se asume que solo
llega tráfico de confianza (Tailscale) hasta ese puerto. Cualquier otro
consumidor (script, n8n, otro servicio, un LLM) debe mandar su propia
`X-API-Key`.

Documentación interactiva autogenerada (Swagger UI): `GET /api/docs`.
Esa ruta y `/api/openapi.json` **no** están protegidas por la API key
(limitación de FastAPI con las rutas de docs automáticas) — exponen el
esquema, no datos en vivo.

## Endpoints de solo lectura

### `GET /api/status`
Estado completo: conexión actual, transporte, última lectura de medidas
y alarmas, y los valores por defecto configurados por entorno.

```json
{
  "connected": true,
  "connecting": false,
  "transport": "tcp",
  "host": "127.0.0.1",
  "port": 3002,
  "device": null,
  "measurements": { "...": "ver /api/measurements" },
  "alarms": { "...": "ver /api/alarms" },
  "last_error": null,
  "default_host": "127.0.0.1",
  "default_port": 3002,
  "default_device": "/dev/ttyUSB0",
  "default_baudrate": 9600
}
```

### `GET /api/measurements`
Última lectura de medidas. `503` si todavía no hay ninguna (recién
conectado, esperando el primer ciclo, o no conectado).

```json
{
  "timestamp": "2026-08-18T10:32:01",
  "idc_amps": [22, 21, 23, 22, 20, 21],
  "fwd_power_w": 3650,
  "vdc_volts": 27.5,
  "current_alarms": [false, false, false, false, false, false],
  "raw_hex": "0280..."
}
```
`current_alarms[i]` es un heurístico local (corriente < 5A), no un bit
que mande el amplificador — ver HALLAZGOS.md §5.

### `GET /api/alarms`
Última lectura de alarmas generales. `503` en las mismas condiciones
que `/measurements`.

```json
{
  "timestamp": "2026-08-18T10:32:01",
  "alarm_byte": 128,
  "alarm_bits": "10000000",
  "rfl_power_alarm": false,
  "fwd_power_alarm": false,
  "exc_power_alarm": false,
  "temp_combiner_alarm": false,
  "permanent_fail_alarm": false,
  "ext_interlock_alarm": false,
  "control_unit_power_alarm": false,
  "raw_hex": "028300..."
}
```

### `GET /api/history?limit=100`
Histórico en memoria de lecturas (medidas + alarmas juntas), más
recientes al final. `limit` por defecto 100, tope `HISTORY_MAX_POINTS`
(por defecto 2000). Se pierde al reiniciar el contenedor — no hay
persistencia en disco.

```json
{
  "count": 812,
  "max_points": 2000,
  "points": [
    { "timestamp": "...", "measurements": { "...": "..." }, "alarms": { "...": "..." } }
  ]
}
```

### `GET /api/summary`
Resumen en una frase en español, pensado para que lo consuma un LLM o
cualquier integración que solo quiera "cómo está el ampli" sin parsear
los otros endpoints.

```json
{
  "text": "Potencia FWD 3650 W, tensión DC 27.5 V. Corrientes (ampli 1: 22 A, ...). Sin alarmas activas.",
  "connected": true,
  "host": "127.0.0.1",
  "port": 3002
}
```
Si no hay conexión: `{"text": "Sin conexión con el amplificador."}` (o
con el motivo del último error, si lo hay).

## Endpoints de control

Todos devuelven `503` si no hay conexión activa, y `500` si el
amplificador no responde o responde con un error.

| Método | Ruta | Body | Notas |
|---|---|---|---|
| `POST` | `/api/connect` | ver abajo | Confirma el protocolo, no solo abre el socket/puerto |
| `POST` | `/api/disconnect` | — | |
| `POST` | `/api/reset` | — | Reset de alarmas |
| `POST` | `/api/power/up` | — | +50W (botón "Subir") |
| `POST` | `/api/power/down` | — | −50W (botón "Bajar") |
| `POST` | `/api/power/set` | `{"percent": 40\|50\|60\|70\|80\|90}` | Preset directo |
| `POST` | `/api/power/max` | — | Potencia máxima |
| `POST` | `/api/stop` | — | Detiene la emisión |
| `POST` | `/api/start` | — | **Hipótesis sin confirmar en vivo** — ver HALLAZGOS.md §6.2/§8 |

### `POST /api/connect`

Dos modos, según qué campos mandes en el body (uno u otro, no ambos):

**Modo `tcp`** (vía ser2net, típicamente en red/Tailscale):
```json
{ "host": "100.113.52.81", "port": 3002 }
```

**Modo `serial`** (puerto serie directo, solo tiene sentido si la app
corre en la misma máquina que el amplificador — ver más abajo):
```json
{ "device": "/dev/ttyUSB0", "baudrate": 9600 }
```
`baudrate` es opcional, por defecto `SERIAL_BAUDRATE` (9600, el que usa
el amplificador — no hay motivo real para cambiarlo).

Si no mandas ni `host`+`port` ni `device`: `400`. Si ya hay una
conexión activa o en curso: `400`. Si conecta el transporte pero el
amplificador no responde con tramas válidas: `502` (ver
`seratel_protocol.py` — no basta con abrir el socket/puerto, se lee
alarmas y medidas de verdad antes de dar por buena la conexión).

Respuesta OK:
```json
{ "ok": true, "transport": "tcp", "host": "100.113.52.81", "port": 3002, "device": null }
```

⚠️ **Modo `serial` y ser2net comparten el mismo cable físico.** El
amplificador solo admite un maestro a la vez en el enlace RS232 — si
ser2net (para la app legacy SupCE) y esta app en modo `serial` hablan a
la vez, las tramas de ambos pueden mezclarse. No hay arbitraje
automático; es responsabilidad de quien opera no usar los dos a la vez.
Para que el contenedor pueda siquiera ver el dispositivo hace falta
activar `COMPOSE_FILE=docker-compose.yml:docker-compose.serial.yml` en
`.env` (ver `docker-compose.serial.yml`) — sin eso, `docker compose up`
funciona igual pero sin acceso al puerto serie.

### Ejemplos `curl`

```bash
# Estado
curl http://<host>:8000/api/status -H "X-API-Key: $API_KEY"

# Resumen en lenguaje natural
curl http://<host>:8000/api/summary -H "X-API-Key: $API_KEY"

# Conectar por red (ser2net)
curl -X POST http://<host>:8000/api/connect \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"host":"100.113.52.81","port":3002}'

# Conectar por puerto serie directo (misma máquina)
curl -X POST http://<host>:8000/api/connect \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"device":"/dev/ttyUSB0"}'

# Fijar potencia al 70%
curl -X POST http://<host>:8000/api/power/set \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"percent":70}'
```

## Variables de entorno relevantes

Ver `.env.example` para la lista completa. Las que afectan a la API:

| Variable | Por defecto | Efecto |
|---|---|---|
| `API_KEY` | *(obligatoria, sin default)* | Key exigida en `X-API-Key`. La app no arranca sin ella. |
| `SER2NET_HOST` / `SER2NET_PORT` | `127.0.0.1` / `3002` | Valores por defecto sugeridos en `/api/status` y precargados en la UI para el modo `tcp` |
| `SERIAL_DEVICE` / `SERIAL_BAUDRATE` | `/dev/ttyUSB0` / `9600` | Ídem para el modo `serial` |
| `POLL_INTERVAL` | `1.0` | Segundos entre lecturas del ciclo en background (`poller_loop`) |
| `HISTORY_MAX_POINTS` | `2000` | Tamaño máximo del buffer de `/api/history` |
