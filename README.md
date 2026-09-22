# Lumel Devtools

Un CLI con las herramientas de desarrollo que se repiten en todos los repos. Se corre
**desde el directorio de cualquier proyecto** y deduce del cwd lo que necesita saber:
stack, puerto, base de datos, variables de entorno.

Está pensado sobre todo para que un agente de código pueda **verificar lo que escribió**:
levantar la app, abrirla en un navegador real, hacer click, y leer los errores de consola y
los requests fallidos en vez de suponer que anda. Y para que una persona haga lo mismo sin
acordarse de ningún comando: `ldt` solo abre un menú.

No se instala en ningún proyecto ni le agrega dependencias: es Python puro contra librerías
que ya están en el sistema.

```
$ ldt browser check http://localhost:3000/panel
HTTP 200 http://localhost:3000/panel
titulo: Panel
problemas: 2
  [console]   Warning: Each child in a list should have a unique "key" prop.
  [network]   GET /api/stats -> HTTP 500
screenshot: C:\Users\vos\.ldt\shots\default-1790024289.png
```

## Requisitos

- Python 3.10+
- [Playwright](https://playwright.dev/python/) y su Chromium, para `ldt browser`
- `psycopg2`, para `ldt db`
- en Linux/macOS, `ss` (iproute2) o `lsof`, para `ldt ports` — en Windows alcanza con
  `netstat`, que ya viene

`ldt doctor` dice qué falta y `ldt doctor --install` lo instala.

## Instalación

En Windows:

```powershell
git clone https://github.com/<tu-usuario>/ldt.git
python .\ldt\ldt.py doctor --install                          # dependencias
powershell -ExecutionPolicy Bypass -File .\ldt\install.ps1    # PATH + regla para agentes
```

En Linux o macOS:

```sh
git clone https://github.com/<tu-usuario>/ldt.git
python3 ./ldt/ldt.py doctor --install    # dependencias
./ldt/install.sh                         # PATH + regla para agentes
```

Los dos instaladores son idempotentes y hacen lo mismo: dejan `ldt` invocable y escriben
`rules/ldt.md` entre marcadores en el archivo de instrucciones de tus agentes
(`~/.claude/CLAUDE.md` por defecto; `--agent-file` / `-AgentFile` apunta a otro, y
`--no-rule` / `-NoRule` saltea ese paso). `install.sh` linkea `bin/ldt` en `~/.local/bin`
y, si ese directorio no está en tu PATH, te imprime la línea para agregarlo — no edita el
rc de tu shell por su cuenta.

Sin PATH, todo se puede invocar como `python /ruta/a/ldt/ldt.py <...>`.

## Comandos

| Comando | Para qué |
|---|---|
| `ldt` | Menú interactivo: elegir con las flechas, sin escribir comandos |
| `ldt help` | La lista completa de comandos, con ejemplos |
| `ldt scan` | Qué es este proyecto: stack, cómo se levanta, puerto, entorno, git |
| `ldt status` | Qué dejó `ldt` corriendo: dev servers, navegadores, puertos |
| `ldt browser` | Navegador real controlable, con sesión que sobrevive entre comandos |
| `ldt dev` | Server de desarrollo en background + sus logs |
| `ldt test` | Correr la suite de tests del proyecto |
| `ldt ports` | Qué escucha en cada puerto, y matar al que quedó colgado |
| `ldt db` | Consultar la base PostgreSQL del proyecto (solo lectura por defecto) |
| `ldt http` | Requests con salida legible; esperar a que un server levante |
| `ldt env` | Qué variables faltan, sin imprimir secretos |
| `ldt cleanup` | Cerrar el dev server y el navegador que quedaron abiertos |
| `ldt doctor` | Verificar (o instalar) lo que necesita; `--fix` limpia lo colgado |

Todos aceptan `--json` para salida estructurada y `--cwd <ruta>` para trabajar sobre otro
directorio.

### El menú

`ldt` sin argumentos abre un menú navegable con las flechas: los grupos arriba, y "todos
los comandos" como una opción más en vez de un volcado apenas se ejecuta el CLI. La
cabecera dice qué proyecto es y qué hay vivo ahora mismo.

Sólo se abre en una terminal de verdad. Si la salida está capturada (un agente), si se pasó
`--json` o si está `LDT_NO_MENU=1`, `ldt` imprime un cartel corto y sale: un menú que espera
una tecla en un proceso sin `stdin` cuelga para siempre.

### `ldt browser`

Un Chromium de Playwright que queda vivo entre invocaciones: cada comando actúa sobre la
misma pestaña, con la misma sesión y las mismas cookies. Es lo que permite hacer
"abrir → loguearse → click → mirar errores" en pasos separados.

```bash
ldt browser check http://localhost:3000    # cargar, reportar errores y sacar screenshot
ldt browser snapshot                       # árbol ARIA de la página (barato en tokens)
ldt browser elements                       # elementos interactivos, numerados @1, @2, ...
ldt browser click @3
ldt browser fill @2 "test@example.com"
ldt browser press Enter
ldt browser errors                         # errores de JS, de consola y requests fallidos
ldt browser shot --full                    # devuelve el path del png
ldt browser close
```

El target de `click`/`fill`/`hover` puede ser un `@N` de `elements` o cualquier selector de
Playwright (`#id`, `.clase`, `text=Guardar`, `role=button[name="Guardar"]`).

**El navegador se abre limpio y al cerrarse se pierde todo.** Es a propósito: un navegador
de desarrollo con estado viejo miente. Lo que dura es la sesión *mientras está abierta*,
que es justamente para lo que sirve el daemon.

**Login manual.** Cuando una ruta pide login, `check` lo detecta (input de password, URL de
login, 401/403) y relanza el Chromium **visible**, frenando con exit code 2. El login lo
hace una persona, no el agente:

```bash
ldt browser login http://localhost:3000/login   # ventana visible; el usuario se loguea
ldt browser goto http://localhost:3000/panel    # mismo browser, ya autenticado
```

Ese login vale mientras el browser siga abierto. `--profile <nombre>` guarda el perfil en
disco y sobrevive al cierre, pero es la excepción, no el default.

**Sesiones paralelas.** `-s <nombre>` abre navegadores independientes (p.ej. dos usuarios
distintos a la vez).

### `ldt dev`

```bash
ldt dev start            # detecta pnpm dev / uvicorn y arranca detached
ldt dev logs --errors    # solo las líneas que parecen errores
ldt dev logs --follow    # seguir el log en vivo
ldt dev stop
```

**No mata lo que no es suyo.** Si el puerto está tomado por otro proceso (típicamente el
`pnpm dev` que levantaste vos a mano), arranca en el siguiente libre y lo dice; `--force`
mata al ocupante. Y al parar mata sólo el proceso que arrancó él, no a quien tenga el
puerto en ese momento.

El proceso sobrevive a la sesión que lo lanzó y la salida queda en
`~/.ldt/logs/<proyecto>.log`.

### `ldt cleanup`

Lo que hace útiles a `dev` y a `browser` —que sobrevivan al comando que los lanzó— es
también lo que deja un `pnpm dev` tomando el puerto y un Chromium comiendo RAM cuando la
tarea terminó. `cleanup` es el "cerrar todo" de un solo paso, y va **al final de la tarea,
no entre pasos**: cerrar el browser a mitad de camino borra el login y el estado de la
página, y hay que rehacer el flujo.

```bash
ldt cleanup            # dev servers y browsers de ESTE proyecto
ldt cleanup --dev      # sólo el dev server, el browser sigue vivo
ldt cleanup --browser
ldt cleanup --all      # todo lo que ldt tenga vivo, de cualquier proyecto
```

Por defecto está scopeado al proyecto (los dev servers levantados desde su directorio y las
sesiones de browser abiertas desde ahí): pueden convivir dos sesiones de agente en repos
distintos sin apagarse el server entre sí. Lo de otros proyectos se lista, no se toca.

**Nada que no haya levantado `ldt` se toca nunca**, ni con `--all`. Lo único que se cierra
siempre son los "huérfanos": Chromiums que quedaron vivos sin archivo de sesión, que nadie
puede usar. `ldt status` los muestra y `ldt doctor --fix` también los limpia.

### `ldt db`

```bash
ldt db ping
ldt db tables
ldt db schema invoices
ldt db q "select count(*) from users"
ldt db q "update users set x = 1" --write   # las escrituras necesitan --write explícito
```

La URL sale de `DATABASE_URL` (o `POSTGRES_URL`, `DB_URL`, …) del proyecto. La conexión se
abre en modo read-only salvo `--write`.

## Dónde queda el estado

Todo fuera del repo en el que se trabaja, en `~/.ldt/`: `sessions/` (browsers vivos),
`daemons/` (registro por pid, para que un Chromium nunca quede invisible), `profiles/`
(perfiles con login, sólo si se usó `--profile`), `shots/` (screenshots y PDFs), `logs/`
(salida de los dev servers y de los tests), `procs/` (procesos levantados por `ldt dev`).

Se puede mover con la variable de entorno `LDT_HOME`.

## Para agentes

`rules/ldt.md` es el texto a pegar en el archivo de instrucciones global del agente
(`~/.claude/CLAUDE.md`, `AGENTS.md`, lo que use) para que sepa que `ldt` existe y cuándo
usarlo. `install.ps1` lo hace solo, entre marcadores, de forma idempotente.

Lo más importante que dice esa regla: **no cerrar lo que no abrió `ldt`**, y **no cerrar el
navegador entre pasos**.

Para trabajar sobre este repo, ver `AGENTS.md`.

## Licencia

MIT.
