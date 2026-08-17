# Hallazgos — Sustituto de SupCE 1.4c (Seratel)

Registro completo de la investigación: qué se ha confirmado, cómo, y
qué queda pendiente. Para la referencia rápida de comandos (formato
trama a trama, listos para copiar/pegar), ver
[COMANDOS_SERATEL.md](COMANDOS_SERATEL.md). Este documento es el
relato de cómo se llegó a cada cosa y por qué se puede confiar en ella
(o no).

## 1. Objetivo y arquitectura

Sustituir SupCE 1.4c (software Windows 98, único que sabe hablar con
el amplificador FM Seratel de esRadio Albacete) por una app web
moderna, hecha por ingeniería inversa completa del protocolo serie.

```
Amplificador Seratel --(RS232, 9600 8N1)--> Raspberry --(ser2net, TCP:3002)--> Tailscale
```

- ser2net expone el puerto serie como TCP puro en el puerto 3002 de
  la Raspberry (`100.113.52.81`).
- Solo admite **un cliente a la vez** — SupCE y nuestra app no pueden
  estar conectados simultáneamente.
- La app (`app/`, FastAPI + HTML/JS vanilla, en Docker) permite
  conectar/desconectar manualmente indicando IP y puerto, con
  confirmación real del protocolo (no solo apertura de TCP).

## 2. Metodología

Dos fuentes de evidencia, cada una con su propio nivel de confianza:

1. **Captura de tráfico real** (`tcpdump` en la Raspberry, puerto
   3002, mientras se opera SupCE de verdad) → analizado con `scapy`
   en Python. Es la fuente más fiable: confirma qué pasa en la
   práctica, incluyendo tiempos, reintentos y comportamiento real del
   amplificador.
2. **Ingeniería inversa estática del propio `SUPCE.EXE`** (binario
   Delphi nativo, sin intérprete de por medio) → desensamblado con
   `capstone` en Python. Permite ver la lógica exacta del programa
   (qué bytes construye, qué espera recibir, cómo reacciona), pero
   sin ejecutarlo — así que confirma "qué hace el código", no
   necesariamente "qué hace el amplificador" en todos los casos.

Cuando ambas fuentes coinciden (la mayoría de los casos), la
confianza es máxima. Cuando solo hay una, se marca explícitamente.

## 3. Protocolo — formato base

```
[STX=0x02] [payload...] [CHECKSUM]
CHECKSUM = XOR(STX, payload...) ^ 0x5A
```

**Confirmado por las dos vías**: coincide con decenas de tramas reales
capturadas, y además se localizó la función exacta en el binario
(`0x40364D` en `SUPCE.EXE`) que construye la trama con esta misma
fórmula, byte a byte.

## 4. Comandos confirmados

Ver tabla completa en [COMANDOS_SERATEL.md](COMANDOS_SERATEL.md). Resumen:

| Comando | Función | Cómo se confirmó |
|---|---|---|
| `02 BC E4` | Leer medidas (corrientes, FWD power, VDC) | Captura real, docenas de veces |
| `02 B0 E8` | Leer alarmas | Captura real |
| `02 EC B4` | Reset de alarmas | Captura real (interlock rojo→verde) |
| `02 C0 98` | Bajar potencia (−50W/pulsación) | Captura real, confirmado por el usuario en el ampli real |
| `02 C3 9B` | Subir potencia (+50W/pulsación) | Captura real, confirmado por el usuario |
| `02 90+n/10 CHK` | Fijar potencia a n% (40-90) | Captura real, correlacionado con FWD Power subiendo hasta el objetivo |
| `02 90 C8` | Potencia máxima | Captura real |
| `02 B3 EB` | Stop | Captura real, ACK limpio dos veces |
| `02 E0 B8` | **Start (hipótesis, sin confirmar en vivo)** | Solo análisis estático — ver §6.2 |

## 5. Alarmas y estados por amplificador

- **Ext. Interlock** (`bit 0x20` del byte de alarmas): confirmado con
  una captura real donde el LED pasó de rojo a verde justo al mandar
  Reset (`0xE0`→`0xC0` en el byte).
- **FWD Power** (`bit 0x02`): confirmado por correlación — el bit se
  activó exactamente cuando la potencia FWD cruzó ~2650-2700W durante
  una rampa hacia el 90%, y la potencia se quedó clavada en 3650W el
  resto de la sesión (protección real, no cosmética).
- **RFL Power, EXC. Power, Temp. Combiner, Permanent Fail** (bits
  0x01, 0x04, 0x08, 0x10): el bit de cada una está confirmado (ver
  §6.5, ingeniería inversa del `.exe` — los nombres de los `TShape`
  del formulario, `g0`..`g4`, coinciden exactamente). Ninguna se ha
  visto activa todavía en una captura real, pero el bit correcto ya
  no es una hipótesis.
- **Control Unit Power** (bit 0x80, no 0x40 como se pensó al
  principio): confirmado qué bit es (§6.5), con polaridad
  probablemente invertida (1=OK) sin confirmar del todo porque nunca
  se ha visto ese bit a 0.
- **Alarma por amplificador (temperatura/intensidad)**: se probó la
  hipótesis de que los bytes 9 y 11 de la trama de medidas fueran un
  bitmap por amplificador. **Descartada con un fallo real**: el
  amplificador 5 estuvo marcando 1A constante (el resto en 17-35A)
  durante 5 minutos, y esos bytes se quedaron en `0x00` todo el
  tiempo. El protocolo del amplificador **no manda ninguna alarma por
  ampli** — la única señal real es la corriente anómala, que ya se
  decodifica directamente. La app calcula un indicador local
  (heurístico, `corriente < 5A`) que reprodujo exactamente el fallo
  real observado.

## 6. Análisis estático de `SUPCE.EXE` (18/08)

El usuario proporcionó el instalador original (`supce14/`). Es un
binario Delphi nativo de 1.3MB (usa las librerías CPort/RX, sin
runtime de VB6), lo que permitió desensamblarlo directamente.

### 6.1 Función `EnviarComando(cmd: byte)`

Localizada en `0x403630` buscando la única instrucción `xor ..., 0x5A`
de todo el binario. Construye la trama de 3 bytes exactamente con la
fórmula ya conocida — confirmación directa desde el código fuente
compilado, no solo por inducción.

### 6.2 Comando nuevo: `0xE0` (candidato a Start)

Se listaron **las 18 llamadas** a `EnviarComando()` en todo el
binario. Los bytes usados son: `B0, E0, B3, EC, C3, C0, 90,
(0x94+preset)` — todos coinciden con lo ya confirmado por captura,
**salvo `0xE0`**, que nunca ha aparecido en ningún pcap. Aparece en el
código justo entre "leer alarmas" (`B0`) y "Stop" (`B3`), en una
secuencia que por orden encaja con un grupo Start/Stop/Reset.

**`02 E0 B8` = Start, hipótesis fuerte pero no probada en vivo.**
Disponible como botón en la app (panel "Pulsadores de control
remoto"), con doble confirmación explícita que avisa de que es una
hipótesis sin confirmar y que puede poner el transmisor al aire de
verdad — pensado para probarlo deliberadamente, en un momento seguro,
idealmente con tcpdump corriendo para verificar/documentar la
respuesta real.

### 6.3 Tabla de ACKs y corrección de un error previo

Se localizó la rutina de SupCE que interpreta el segundo byte de cada
respuesta (justo tras el STX) — una cadena de comparaciones típica de
un `case` de Pascal compilado. Simulada exactamente:

| Byte ACK | Resultado en SupCE | Comando |
|---|---|---|
| `0x50` | OK | Preset / Máxima — confirmado en real |
| `0x62` | OK | **Sin asignar** (Subir o Start) |
| `0x64` | OK | Bajar potencia — confirmado en real |
| `0x70` | OK | Stop — confirmado en real |
| `0x73` | OK | Reset — confirmado en real |
| `0x7C` | OK | **Sin asignar** (Subir o Start) |
| `0x80` / `0x83` | Inicio de respuesta larga | Medidas / Alarmas |

**No existe ningún código de error/rechazo distinto** — cualquier byte
no reconocido simplemente hace que SupCE reinicie el parser en
silencio (re-sincronización, no un error explícito).

Esto **corrige una hipótesis anterior**: se había documentado que
`0x64` (visto tras un "Disminuir" que acabó en "El equipo remoto no
responde") era un código de rechazo por "equipo parado". Es
incorrecto — `0x64` cae en la misma rama de éxito que el resto. La
causa real de aquel error de comunicación sigue sin explicación
firme; ya está corregido tanto en la documentación como en la app
(antes mostraba el ACK en rojo como "Rechazado", ahora en verde como
"OK — potencia bajada").

Quedan sin asignar `0x62` y `0x7C` — por descarte, uno es el ACK de
Subir potencia y el otro el de Start, pero no se sabe cuál sin
capturar cada uno por separado en vivo.

### 6.4 Primer intento fallido de localizar la asignación de bits

Se intentó localizar por desensamblado la rutina exacta que decide qué
LED de alarma pintar según qué bit. Se encontraron varios candidatos
en el código que comprueban los 8 bits de un byte, pero en un orden
que no encajaba con un simple bucle "por cada alarma en su posición",
y había decenas de sitios similares en el binario (mucho código de
librerías de terceros sin relación) — demasiada ambigüedad para
confiar en ninguno. Se abandonó esa vía en ese momento. Ver §6.5 para
cómo se resolvió por otro camino.

### 6.5 Asignación completa de bits de alarma — resuelto vía el recurso de formulario

En vez de perseguir la lógica de comparación de bits, se buscó
directamente en el **recurso de formulario compilado** (`.dfm`
embebido en `.rsrc`) los componentes visuales (`TShape`) que
representan cada LED de alarma. Resultado: están nombrados
literalmente `g0`, `g1`, `g2`, `g3`, `g4`, `g5`, `g7` (sin `g6`) — el
número coincide con el índice de bit, y aparecen en el `.dfm` en el
mismo orden que las etiquetas de texto ("EXC. Power", "FWD Power",
etc.), lo que permite emparejar cada alarma con su shape sin ambigüedad:

| Bit | Alarma | Shape |
|---|---|---|
| 0 (`0x01`) | RFL Power | `g0` |
| 1 (`0x02`) | FWD Power | `g1` |
| 2 (`0x04`) | EXC. Power | `g2` |
| 3 (`0x08`) | Temp. Combiner | `g3` |
| 4 (`0x10`) | Permanent Fail | `g4` |
| 5 (`0x20`) | Ext. Interlock | `g5` |
| 6 (`0x40`) | *(sin usar — no existe `g6`)* | — |
| 7 (`0x80`) | Control Unit Power | `g7` |

Esto **coincide de forma independiente y exacta** con los dos bits que
ya teníamos confirmados por captura real (FWD Power=bit1 por
correlación con potencia alta, Ext. Interlock=bit5 por el Reset real)
— refuerzo cruzado entre las dos fuentes de evidencia (§2), máxima
confianza posible sin ejecutar el propio programa.

**Corrige un error anterior**: la hipótesis inicial (por simple orden
de fila, bits 0-4) tenía **EXC. Power y RFL Power intercambiados**, y
había descartado sin más "Control Unit Power" al no encajar en el
bit 6 — ahora se sabe que su bit real es el 7, no el 6, y que el 6
sencillamente no tiene indicador en el panel (bit de relleno/reservado).

**Aplicado en la app y en `COMANDOS_SERATEL.md`.** El único cabo
suelto que queda es la *polaridad* de Control Unit Power (bit 7):
siempre ha valido 1 en toda captura real, con el indicador siempre en
verde, así que se asume 1=OK/0=alarma (invertida respecto al resto),
pero nunca se ha visto ese bit a 0 para confirmarlo del todo.

### 6.6 Límites reales del análisis estático (qué NO se puede sacar del `.exe`)

Se siguió el flujo de control real (no ya búsqueda de patrones a
ciegas) desde el punto donde el contador de bytes recibidos
`[0x504e98]` llega a su longitud objetivo, hasta la función que
procesa cada trama completa (`0x407bf0` y sus dependientes,
`0x405ccc`/`0x4047ac`). Esto permitió confirmar dos límites reales de
lo que este método puede dar de sí:

- **No se puede saber cuál de `0x62`/`0x7C` es el ACK de Subir o de
  Start.** La rutina que valida el ACK recibido comprueba únicamente
  "¿es uno de los 6 bytes que reconozco como válido?" — no comprueba
  que sea el ACK *correcto para el comando que se mandó*. Los 6
  valores son intercambiables a ojos del programa. Esto no es una
  limitación del análisis, es una propiedad real del programa: la
  información que buscamos sencillamente no existe en su lógica.
- **El comportamiento físico del amplificador ante cada alarma no
  está en el código de SupCE.** SupCE solo decide qué LED pintar; qué
  hace el amplificador de verdad (¿limita potencia? ¿se resetea solo?)
  es un comportamiento del hardware, invisible para un programa que
  solo lee su puerto serie. Es la misma razón por la que el límite de
  3650W de FWD Power (§6.2 del hallazgo de esa sesión) tampoco se
  podría haber deducido leyendo el código — solo se vio capturando el
  tráfico real.

A partir de aquí, cualquier otro hallazgo posible requiere pruebas en
vivo, no más tiempo de desensamblado.

## 7. Bugs encontrados y corregidos en la app durante el desarrollo

- **LEDs invisibles en la tabla de Amplificadores**: `<span class="led">`
  llevaba `width`/`height` explícitos pero sin `display:inline-block`.
  Dentro de un contenedor flex (como las filas de "Alarmas Generales")
  funcionaba por casualidad (los hijos de un flex container se
  "blockifican" automáticamente); dentro de una `<td>` normal de la
  tabla de amplificadores, el navegador ignoraba el tamaño y el LED
  quedaba prácticamente invisible. Detectado comparando una captura de
  pantalla real de la app (vía Playwright) contra la captura de SupCE
  que pasó el usuario.
- **Columna "M" mal etiquetada**: se había añadido como un LED más
  (sin datos, siempre verde) cuando en realidad "M" en el original es
  el número del amplificador (1-6), no un indicador — corregido
  quitando el LED falso.
- **Falso éxito en timeout real**: si el amplificador no respondía
  nada (timeout real, el mismo caso que en SupCE da "El equipo remoto
  no responde"), el backend devolvía `{"ok": true}` con una respuesta
  vacía. Corregido para que ese caso lance un error explícito
  ("El equipo remoto no responde (timeout esperando ACK)"),
  distinguible de una respuesta válida pero inesperada.
- **Hipótesis de bits 0x64 = error**: ver §6.3, corregida tras el
  análisis estático.

## 8. Pendiente

Solo queda lo que ya no se puede sacar mirando el código en frío — el
`.exe` está exprimido para lo que da de sí en estos puntos. Se
intentó explícitamente antes de dejarlo por escrito (ver §6.6).

- **Confirmar Start (`0xE0`)** con una captura real deliberada, en
  condiciones seguras para transmitir.
- **Asignar `0x62` / `0x7C`** a Subir potencia y Start respectivamente
  — **confirmado que es imposible por análisis estático** (§6.6): el
  propio SupCE valida el ACK de forma genérica, sin comprobar que
  corresponda al comando que mandó. Solo una captura real de cada uno
  por separado lo resuelve.
- **Ver en acción las alarmas restantes** (EXC. Power, RFL Power,
  Temp. Combiner, Permanent Fail) y **confirmar la polaridad de
  Control Unit Power** — el bit de cada una ya está confirmado
  (§6.5), pero su *comportamiento real* (¿limitan algo como FWD
  Power? ¿son latcheadas como Interlock?) lo decide el amplificador,
  no SupCE — no hay nada de esto en el código del programa (igual que
  el límite de 3650W de FWD Power tampoco estaba ahí, ver §6.6).
- **RFL Power**: nunca se ha visto un valor distinto de 0W en ninguna
  captura, así que no se ha podido localizar su byte/escala en la
  trama de medidas. La app lo muestra como "N/D" honestamente en vez
  de inventar un 0 fijo.
- **Explicación firme del incidente "El equipo remoto no responde"**
  visto tras Stop + Disminuir (ver §6.3) — se descartó la explicación
  inicial, pero no hay una nueva confirmada.
