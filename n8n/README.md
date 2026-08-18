# Workflow n8n — Amplificador Seratel: Control y Dashboard

Hay dos ficheros:

- **`Amplificador_Seratel_-_Control_y_Dashboard.json`** — la copia real,
  con tu `API_KEY`, IP Tailscale, chat_id y user ID ya rellenados.
  **Está en `.gitignore`, no se sube al repo.** Es la que importas en n8n.
- **`Amplificador_Seratel_-_Control_y_Dashboard.template.json`** — la
  misma estructura con placeholders en vez de esos datos. Esta es la
  que sí está en git, para compartir el workflow sin filtrar secretos
  (mismo patrón que `Meshview_-_..._.template.json` en el otro proyecto).

Si reconstruyes la copia real a partir del template, sustituye:
`PON_AQUI_TU_API_KEY`, `http://IP_TAILSCALE_RASPBERRY:8000/api`,
`CHAT_ID = 0` y `AUTHORIZED_USERS = [0]` por tus valores reales, en los
nodos "Leer estado y preparar dashboard" y "Procesar comando".

El workflow (real, importable) conecta con la [API](../API.md) de la
app y expone un bot de Telegram con:

- Un **mensaje fijado** que se edita cada minuto con el estado del
  amplificador (potencia, corrientes, alarmas, conexión).
- **Notificaciones de evento**: cuando una alarma se activa/resuelve, o
  el amplificador se conecta/desconecta, manda un mensaje nuevo aparte.
- **Comandos** para operar en remoto: `/estado`, `/alarmas`, `/log [n]`,
  `/subir`, `/bajar`, `/potencia <pct>`, `/maxima`, `/conectar`,
  `/desconectar`, `/arrancar`, `/parar`, `/reset`, `/ayuda`.
- `/arrancar`, `/parar` y `/reset` piden **confirmación con botones**
  (Confirmar/Cancelar) antes de ejecutar — caduca a los 60s.
- **Whitelist de usuarios de Telegram**: solo los IDs en
  `AUTHORIZED_USERS` (dentro del nodo "Procesar comando") pueden usar
  el bot; cualquier otro recibe "🔒 No autorizado."

## Arquitectura (dos triggers, un solo static data global)

Mismo patrón que el workflow de Meshview de nodos: un Schedule Trigger
para el dashboard/eventos, y un Telegram Trigger para comandos y
botones, ambos compartiendo `$getWorkflowStaticData('global')`
(`pinnedMessageId`, `lastAlarms`, `lastConnected`, `pendingConfirm`).
Se pierde si reimportas el workflow desde cero.

## Puesta en marcha

### 1. Crea el bot de Telegram

Con [@BotFather](https://t.me/BotFather): `/newbot`, dale un nombre
(ej. `SupCe bot`). Guarda el token.

### 2. Configura la API_KEY, URL y chat_id

Esta instancia de n8n bloquea el acceso a `$env` desde nodos Code
(`N8N_BLOCK_ENV_ACCESS_IN_NODE`), así que no se usan variables de
entorno del contenedor: `API_URL`, `API_KEY` y `CHAT_ID` van como
constantes literales directamente en los nodos "Leer estado y preparar
dashboard" y "Procesar comando" de la copia real del JSON (la que
**no** está en git). Ya vienen rellenadas en esa copia — si algún día
rotas la `API_KEY` (`.env` de `supCE_Modernizado`), edítalas ahí a
mano.

### 3. Averigua tu chat_id

Manda cualquier mensaje al bot (o al grupo donde quieras el dashboard)
y consulta `https://api.telegram.org/bot<TOKEN>/getUpdates` — el
`chat.id` de la respuesta es el que necesitas para la constante
`CHAT_ID` del nodo "Leer estado y preparar dashboard" (ver paso 2).

### 4. Averigua tu Telegram user ID

Escríbele a [@userinfobot](https://t.me/userinfobot) si necesitas
añadir más gente — te devuelve el ID numérico. En la copia real ya
está puesto el tuyo (`379659659`, Sremylio) en `AUTHORIZED_USERS`
dentro del nodo "Procesar comando"; añade ahí más IDs separados por
comas si hace falta.

### 5. Importa el workflow

En n8n: *Import from File* → selecciona
`Amplificador_Seratel_-_Control_y_Dashboard.json`.

### 6. Credencial de Telegram

Todos los nodos Telegram del workflow apuntan a una credencial llamada
`SupCe bot` que no existe todavía en tu instancia — n8n te pedirá
crearla/asignarla al importar. Crea una credencial tipo **Telegram
API** con el token de BotFather y asígnala a los 8 nodos Telegram
(Comandos y botones, Editar dashboard, Enviar dashboard, Enviar
evento, Responder callback, Actualizar confirmación, Pedir
confirmación, Responder).

Los 6 nodos que mandan/editan texto (todos menos "Comandos y botones"
y "Responder callback") llevan además `appendAttribution: false`, para
que Telegram no añada el pie "This message was sent automatically
with n8n" a cada mensaje.

### 7. Activa el workflow

Al activarlo, el Schedule Trigger empieza a mandar el primer dashboard
en <1 minuto, y el Telegram Trigger registra el webhook automáticamente.

## Notas y limitaciones conocidas

- **Las llamadas a la API van con `$helpers.httpRequest`, no `fetch`.**
  Esta instancia de n8n corre en un runtime donde `fetch` no existe como
  global dentro de los nodos Code (a diferencia del workflow de
  Meshview, más antiguo, que sí podía usarlo) — por eso los nodos
  "Leer estado y preparar dashboard" y "Procesar comando" usan el
  helper propio de n8n en su lugar.
- **Los botones inline** (nodo "Pedir confirmación") usan el formato
  estándar de n8n para teclados inline de Telegram, pero no se ha
  podido probar contra una instancia real al construir este workflow
  (edición de JSON local, sin acceso a tu n8n — ver la nota de siempre
  sobre cómo trabajamos estos workflows). Si al importar ese nodo en
  concreto da error o no muestra los botones, es el punto más probable
  a retocar a mano: abre el nodo, y en el desplegable "Reply Markup"
  vuelve a seleccionar "Inline Keyboard" y reintroduce los dos botones
  (texto `✅ Confirmar` / `callback_data` = `{{ $json.confirmData }}`,
  y `❌ Cancelar` / `{{ $json.cancelData }}`).
- Tras confirmar o cancelar, el mensaje de confirmación se edita para
  mostrar el resultado, pero **los botones no se retiran** (limitación
  deliberada para no complicar más el nodo de edición) — si se pulsan
  otra vez, el bot responde "caducado o inválido" en vez de re-ejecutar
  la acción, así que es inofensivo.
- El histórico de `/log` viene directamente de `GET /api/history` de
  la API — no se duplica almacenamiento en n8n.
- **Riesgo de doble maestro sin cambios**: nada de esto cambia el
  aviso ya conocido de `/api/connect` en modo `serial` — sigue siendo
  responsabilidad de quien opera no usar ser2net (SupCE) y el modo
  serie a la vez. Ver [API.md](../API.md).
