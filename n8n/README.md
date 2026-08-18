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
`PON_AQUI_TU_API_KEY`, `http://IP_TAILSCALE_RASPBERRY:8000/api` y
`AUTHORIZED_USERS = [0]` por tus valores reales, en los nodos "Leer
estado y preparar dashboard" y "Procesar comando". `CHAT_ID = 0` puedes
dejarlo tal cual — es solo un fallback hasta que alguien autorizado le
escriba al bot (ver más abajo).

El workflow (real, importable) conecta con la [API](../API.md) de la
app y expone un bot de Telegram con:

- Un **mensaje fijado** que se edita cada minuto con el estado del
  amplificador (potencia, corrientes, alarmas, conexión).
- **Notificaciones de evento**: cuando una alarma se activa/resuelve, o
  el amplificador se conecta/desconecta, manda un mensaje nuevo aparte.
- **Comandos** para operar en remoto: `/estado`, `/alarmas`, `/log [n]`,
  `/subir`, `/bajar`, `/potencia <pct>`, `/maxima`, `/conectar`,
  `/desconectar`, `/arrancar`, `/parar`, `/reset`, `/suscribir`,
  `/desuscribir`, `/ayuda`.
- `/arrancar`, `/parar` y `/reset` piden **confirmación con botones**
  (Confirmar/Cancelar) antes de ejecutar — caduca a los 60s.
- **Whitelist de usuarios de Telegram**: solo los IDs en
  `AUTHORIZED_USERS` (dentro del nodo "Procesar comando") pueden usar
  el bot; cualquier otro recibe "🔒 No autorizado."
- **Suscripción explícita por chat, con envío a todos los suscritos**:
  `/suscribir` añade el chat actual a `bot_state.chatIds`;
  `/desuscribir` lo quita. El dashboard (mensaje fijado, uno por chat)
  y las alertas se mandan/editan en **todos** los chats de esa lista.
  Un usuario autorizado puede hablar con el bot (consultar `/estado`,
  etc.) sin que eso lo suscriba automáticamente — hace falta el comando
  explícito. La constante `CHAT_ID` del nodo "Preparar dashboard" solo
  se usa como semilla mientras nadie se ha suscrito todavía (lista
  vacía).
- **Fijado nativo de Telegram**: la primera vez que se manda el
  dashboard a un chat (tras `/suscribir`), además de guardarlo como
  "mensaje a editar" internamente, se fija de verdad en el chat
  (`pinChatMessage`) para que quede arriba del todo. `/desuscribir`
  hace lo inverso: desfija (`unpinChatMessage`) y borra
  (`deleteMessage`) ese mensaje del chat, además de sacarlo de
  `chatIds`.

## Arquitectura (dos triggers, un solo static data global)

Mismo patrón que el workflow de Meshview de nodos: un Schedule Trigger
para el dashboard/eventos, y un Telegram Trigger para comandos y
botones, ambos compartiendo el estado persistido vía `/api/bot-state`
(`pinnedMessageIds` — uno por chat —, `lastAlarms`, `lastConnected`,
`pendingConfirm`, `chatIds`). Se pierde si el contenedor de la API se
reinicia.

En el ciclo del dashboard, "Preparar dashboard" genera un evento por
cada combinación (acción × chat de `chatIds`). Para el mensaje fijado,
hay dos nodos después de mandar/editar en Telegram:

- **"Extraer mensaje enviado (por chat)"** — Code en modo **"Run Once
  for Each Item"**, uno por cada chat. Aquí sí es fiable usar
  `$('Preparar dashboard').item` para recuperar el chat/messageId de
  origen de ese envío concreto. También marca `shouldPin` (true solo si
  no había mensaje fijado antes en ese chat) y, en paralelo — sin
  bloquear el guardado del estado —, dispara "¿Hay que fijar?" →
  "Fijar mensaje" (`pinChatMessage`) cuando toca.
- **"Preparar guardado (dashboard)"** — Code en modo "Run Once for All
  Items", agrega esos N items en un único `bot_state` (con el
  `pinnedMessageIds` actualizado de cada chat) antes de la única
  llamada `PUT /api/bot-state` del ciclo.

Están separados en dos nodos a propósito: agregar y correlacionar por
chat en el MISMO nodo "Run Once for All Items" (con `itemMatching()`)
se probó primero y no funcionaba de forma fiable con más de un chat —
el mensaje fijado se perdía y mandaba uno nuevo en cada ciclo en vez de
editar el existente. `$('nodo').item` sin índice, en modo "All Items",
es ambiguo en cuanto hay más de un item; en modo "Each Item" no lo es,
así que ahí se resuelve el emparejamiento y luego solo se agrega.

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

### 3. Los chat_id se detectan solos

No hace falta rellenar `CHAT_ID` a mano: cada usuario autorizado (ver
paso 4) que le escriba al bot o pulse un botón añade su `chat.id` a
`bot_state.chatIds`, y el dashboard/las alertas empiezan a mandarse
también a ese chat automáticamente — a todos los que estén en la
lista, no solo al último. Solo tendrías que tocar la constante
`CHAT_ID` del nodo "Preparar dashboard" si quieres forzar un chat de
arranque antes de que nadie le haya hablado al bot.

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
API** con el token de BotFather y asígnala a los 11 nodos Telegram
(Comandos y botones, Editar dashboard, Enviar dashboard, Enviar
evento, Responder callback, Actualizar confirmación, Pedir
confirmación, Responder, Fijar mensaje, Desfijar mensaje, Borrar
mensaje).

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
- **`pinnedMessageIds` se fusiona, no se reemplaza**: en "Preparar
  guardado (dashboard)" se parte del `pinnedMessageIds` anterior y solo
  se sobrescriben las entradas de los chats que tuvieron éxito ese
  ciclo. Si no fuera así, un fallo puntual al mandar/editar en un chat
  (Telegram caído un segundo, red, lo que sea) borraría su entrada para
  siempre y el bot le mandaría un mensaje nuevo en cada ciclo a partir
  de ahí, sin recuperarse solo. "Enviar dashboard" lleva además
  `onError: continueRegularOutput` para que un fallo en un chat no
  arrastre a los demás del mismo ciclo.
- **Si `bot_state` se corrompió con datos antiguos** (versiones previas
  a este workflow guardaban el envío entero de Telegram en vez de solo
  `nextBotState`, dejando un `nextBotState` anidado dentro de sí mismo
  cada vez más grande — inofensivo para el funcionamiento actual, pero
  cada vez más pesado): resetéalo a mano una vez con
  `curl -X PUT -H "X-API-Key: TU_API_KEY" -H "Content-Type: application/json" -d '{}' http://IP_TAILSCALE_RASPBERRY:8000/api/bot-state`.
  Se pierde el mensaje fijado (mandará uno nuevo el próximo ciclo) pero
  arranca limpio, sin la basura acumulada.
