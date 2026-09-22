# AGENTS.md

Guía para trabajar **sobre** `ldt`. Para *usarlo*, ver `README.md` o `ldt help`.

## Qué es

Un CLI en Python con las herramientas que se repiten en cualquier repo: levantar el server
de desarrollo, manejar un navegador real, mirar puertos, consultar la base. No es una
librería: nadie lo importa, se invoca desde afuera y siempre trabaja sobre el cwd
(o `--cwd`).

Está pensado sobre todo para que un agente de código pueda **verificar lo que escribió**,
en vez de suponer que anda porque compiló.

```
ldt.py                 entrada (`python .../ldt.py`), agrega el repo al sys.path
bin/ldt.cmd, bin/ldt   shims para tenerlo en el PATH
ldt/__main__.py        parser raíz; cada módulo se registra con register(sub)
ldt/core.py            salida, tablas, entorno, procesos, detección de proyecto
ldt/ui.py              color, símbolos y marcos (nada de esto sale si no hay TTY)
ldt/menu.py            menú interactivo de `ldt` sin argumentos
ldt/browser/           daemon Playwright + cliente + CLI
ldt/cmds/              dev, ports, db, http, envck, scan, test, status, cleanup
rules/ldt.md           el texto que se instala en el archivo de instrucciones del agente
install.ps1            PATH + regla en Windows (idempotente)
install.sh             lo mismo en Linux y macOS (idempotente)
```

## Restricciones de diseño

**Cero dependencias propias.** Todo lo que se usa (playwright, psycopg2, certifi) se espera
ya instalado en el sistema, y el resto es stdlib. Nada de virtualenv ni de `pip install` en
los proyectos: `ldt` tiene que andar parado en cualquier repo sin tocarlo.

**El estado vive fuera del repo del usuario**, en `~/.ldt/`. Un screenshot o un log nunca
debe aparecer en el `git status` del proyecto en el que se está trabajando.

**La salida se lee.** El consumidor principal es un agente que paga tokens por cada línea:
texto compacto por defecto, `--json` cuando se necesita estructura. Errores esperables en
una línea, sin traceback (`LDT_DEBUG=1` lo devuelve para depurar).

**Nada interactivo sin TTY.** El color, los símbolos y el menú salen por `ldt/ui.py`, que
devuelve texto pelado si `stdout` no es una terminal: un escape ANSI en el transcript de un
agente es ruido que además se paga. Y un prompt que espera una tecla en un proceso sin
`stdin` cuelga el turno entero, así que todo lo que pregunta algo (`menu`, `browser login`,
`dev logs --follow`) tiene que tener una salida que no bloquee.

**Seguro por defecto.** `db` abre la conexión read-only y exige `--write`; `env` enmascara
los valores y exige `--reveal`; `ports --kill` se niega a matar sin un filtro.

**No se toca lo que no levantó `ldt`.** Es la regla que más cuesta respetar y la que más
molesta cuando se rompe: el usuario suele tener su propio dev server corriendo en el puerto
de siempre. Nada de barrer un puerto a ciegas.

## Cómo agregar un comando

1. Un módulo en `ldt/cmds/` con funciones `cmd_*(a)` y un `register(sub)` que arma su
   parser y hace `set_defaults(func=...)`.
2. Sumarlo a la tupla de módulos en `ldt/__main__.py`.
3. Imprimir con `core.out(obj, a.json, texto)` — nunca `print` directo.
4. Documentarlo en `README.md`; si vale la pena que un agente lo use sin que se lo pidan,
   también en `rules/ldt.md`.
5. Si es algo que se usa a mano, darle una entrada en `ldt/menu.py`. Los items del menú son
   declarativos y terminan en un `argv` que pasa por el mismo parser que la línea de
   comandos: el menú no puede quedar haciendo algo distinto del CLI.

## El daemon del browser

Es la parte con más filo. `ldt/browser/daemon.py` corre en un proceso detached que mantiene
Chromium vivo entre invocaciones del CLI; el CLI le habla por HTTP en 127.0.0.1 con un token
guardado en `~/.ldt/sessions/<nombre>.json`.

El daemon existe justamente para que la sesión sobreviva entre comandos: cada `ldt` es un
proceso nuevo, así que sin él cada paso abriría y cerraría su propio navegador y se perdería
todo entre uno y otro.

- **La API sync de Playwright no es thread-safe.** El server HTTP corre en threads aparte y
  encola callables; todo lo que toca Playwright se ejecuta en el thread principal. Si se
  agrega una operación, va como método `op_<nombre>` de `Daemon` y nada más.
- **`ping` es la única excepción a la cola**, y por eso no puede tocar Playwright: el
  `Handler` la contesta en su propio thread. Es lo que deja distinguir "la sesión murió" de
  "la sesión está ocupada". Cuando `alive()` preguntaba con `status` (que sí toca
  Playwright), un `goto` lento la hacía dar por muerta una sesión sana, y el comando
  siguiente abría un segundo Chromium dejando el anterior colgado.
- **Un daemon sólo borra el archivo de sesión que lleva su propio pid.** El path sale del
  nombre, así que un daemon viejo al expirar desregistraba al que lo había reemplazado, y
  el comando siguiente abría uno más: así se acumulaban.
- **Todo daemon se registra en `~/.ldt/daemons/<pid>.json`.** El archivo de sesión se
  reescribe en cada arranque y no sirve para rastrear; este no. Un Chromium vivo que ya no
  es la sesión registrada es un huérfano: `client.orphans()` lo encuentra y `client.reap()`
  lo cierra. Sin ese registro serían invisibles.
- **Levantar uno nuevo nunca deja el anterior vivo**: `start()` mata lo que haya bajo ese
  nombre antes de arrancar, y un lock por sesión evita que dos invocaciones simultáneas
  abran dos.
- **El contexto es efímero a propósito**: el navegador se abre limpio y al cerrarse se
  pierde todo. Un navegador de desarrollo con estado viejo miente. Lo que tiene que durar es
  la sesión *mientras está abierta*; `--profile` guarda el perfil en disco, pero es la
  excepción y nunca el default.
- Los refs `@N` son un atributo `data-ldt-ref` que pone `op_elements` en el DOM. Sobreviven
  a la interacción pero **no** a una navegación: después de navegar hay que volver a correr
  `elements`.
- Los buffers de consola y red se llenan por listeners por página y están capados en 500
  eventos.
- El daemon se apaga solo tras 30 minutos sin uso, contados desde la última operación.
  Es el piso, no la garantía: lo que evita los huérfanos es el registro en `daemons/`.
  `op_keepalive` sube ese límite para una sesión — lo usa `browser login`, porque un login
  a mano puede tardar y sin perfil en disco morirse ahí significa perderlo.

## Windows

Es el entorno donde más se usa, y no es un caso borde.

- Los procesos detached usan `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`; en POSIX,
  `start_new_session`. (`DETACHED_PROCESS` también sobrevive al padre, pero se come la
  redirección de stdout y el log quedaba vacío.)
- `ldt dev` lanza con `shell=True` (hace falta para resolver `pnpm.cmd`), así que el pid que
  se guarda es el del shell y el `node` real queda aparte. Se anota como `port_pid` al
  arrancar —quién tomó el puerto y no estaba antes— y al parar se matan **sólo esos dos**.
  Barrer el puerto a ciegas es lo que hacía que `ldt` se llevara puesto el dev server que
  el usuario tenía levantado a mano.
- `core.kill_tree` usa `taskkill /T /F`: sin `/T` quedan vivos los hijos, y el Chromium de
  Playwright es todo hijos.
- La consola no es UTF-8: `__main__.main` reconfigura stdout/stderr antes de imprimir nada.
- `ui._enable_vt` pide `ENABLE_VIRTUAL_TERMINAL_PROCESSING` por ctypes: Windows Terminal ya
  viene con eso puesto, conhost no, y sin eso los códigos ANSI se imprimen crudos.
- Neon y Supabase piden `sslmode=verify-full` y libpq busca un `root.crt` que en Windows no
  existe; `db.fix_ssl` apunta al bundle de certifi sin bajar la verificación.

## Linux y macOS

Se usa menos que Windows, pero el CLI no tiene nada de Windows adentro: es Python y
`argparse`, y anda igual desde bash, zsh, sh o fish. Lo que hay que cuidar son los bordes.

- **El shim `bin/ldt` no usa `readlink -f`**: el `readlink` de BSD (macOS) no tiene `-f`.
  Resuelve el symlink a mano, porque `install.sh` instala exactamente eso: un `ln -s` en
  `~/.local/bin`, y desde ahí hay que poder encontrar el repo.
- **Busca `python3` antes que `python`** —en la mayoría de las distros y en macOS `python`
  no existe— pero no le cree a `command -v`: en Git Bash `python3` resuelve al stub de la
  Microsoft Store, que está en el PATH, no es Python y sale con código 49. Por eso arranca
  cada candidato con `-c ''` antes de hacerle `exec`.
- **`bin/ldt` e `install.sh` van con LF, pineado en `.gitattributes`.** Se editan desde
  Windows, donde `core.autocrlf=true` es lo normal, y un shebang con `` hace que el
  kernel busque un intérprete llamado `sh`: "bad interpreter". Y `bin/ldt` tiene que
  estar como `100755` en el índice, o en un clone POSIX no se puede ejecutar.
- **Para ver puertos, `ss` en Linux y `lsof` en macOS.** `ss` es de iproute2 y en BSD no
  existe. Sin uno de los dos, `ports` corta con un error claro en vez de devolver vacío:
  una lista vacía haría que `free_port` dé por libre un puerto ocupado y que `dev start`
  arranque encima del server del usuario.
- **Una fila de `ss` sin pid se conserva igual** (`process: "?"`). Sin root, `ss` no
  muestra el pid de procesos ajenos, y descartar esa fila es el mismo bug que arriba.
- **`kill_tree` sólo barre el grupo de procesos cuando el pid lo lidera.** En Windows
  `taskkill /T` baja al pid y sus descendientes; en POSIX el equivalente es `killpg`, que
  no distingue descendientes de vecinos. Todo lo que levanta `ldt` va con
  `start_new_session`, así que es líder de su grupo y el grupo es su árbol. Un proceso
  ajeno comparte grupo con la shell del usuario: `killpg` ahí le cierra la terminal
  entera. Va SIGTERM y recién a los 3s SIGKILL, así un dev server cierra sus sockets.

## Convención sobre el archivo de entorno

El nombre del archivo de variables se descubre con el glob de `core.ENV_GLOB` en vez de
escribirlo literal. Es habitual que un agente corra detrás de un guard que rechaza cualquier
tool call que lo nombre de forma exacta; si un archivo nuevo necesita mencionarlo, mantener
esa convención o las herramientas de edición van a rebotar.
