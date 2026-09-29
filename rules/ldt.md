# Herramientas de desarrollo (`ldt`)

Hay un CLI, `ldt`, con herramientas para trabajar sobre **cualquier** repo: levantar la
app, verla en un navegador real, leer logs, consultar la base. Antes de escribir un script
suelto para probar algo, fijarse si `ldt` ya lo hace.

Si `ldt` no está en el PATH, invocarlo como `python <RUTA-DEL-REPO>/ldt.py <...>`.

```
ldt                                       menú interactivo (para una persona, en su terminal)
ldt help                                  la lista completa de comandos, con ejemplos
ldt scan                                  qué es este proyecto y cómo se levanta
ldt status                                qué dejó ldt corriendo: dev servers, browsers, puertos
ldt browser check <url>                   abrir la app en un Chromium real: errores + screenshot
ldt browser elements | click @N | fill    interactuar con la página (la sesión sigue viva)
ldt browser errors                        errores de JS, de consola y requests fallidos
ldt browser login <url>                   abrir el Chromium VISIBLE para que el usuario se loguee
ldt dev start | logs --errors | stop      server de desarrollo en background + sus logs
ldt test                                  correr la suite de tests del proyecto
ldt ports 3000 --kill                     ver o liberar un puerto ocupado
ldt db tables | schema <t> | q "<sql>"    consultar la base PostgreSQL del proyecto
ldt http get /api/health                  requests contra el server local
ldt env check                             variables de entorno que faltan (sin mostrar valores)
ldt cleanup                               cerrar lo que abrió ldt (al final, no entre pasos)
```

Reglas:

- **Un cambio de UI no está verificado hasta verlo en el browser.** Que compile y que
  pasen los tipos no dice nada del runtime: usar `ldt browser check` sobre la ruta tocada y
  mirar `ldt browser errors`.
- **No cerrar nada que no haya abierto `ldt`.** Nunca `ldt ports --kill`, `taskkill` ni
  `kill` sobre un dev server que no aparezca en `ldt dev list`: el usuario suele tener el
  suyo levantado a mano. Si el puerto está tomado por algo ajeno, `ldt dev start` ya
  arranca en el siguiente puerto libre y lo avisa; `--force` (matar al ocupante) sólo si el
  usuario lo pide. Lo mismo vale para lo que `ldt` levantó para **otro** proyecto: suele ser
  de otra sesión de agente trabajando en paralelo. `dev start`, `ports --kill`, `cleanup` y
  `browser close --all` ya lo dejan en paz; no forzarlos con `--force` / `--global`.
- **`ldt cleanup` va una sola vez, al terminar, y no si el usuario sigue trabajando.**
  Entre pasos **no se cierra nada**. El browser de desarrollo es efímero a propósito:
  cerrarlo borra las cookies, el login y el estado de la página, así que cerrarlo antes de
  terminar de probar la feature obliga a rehacer todo el flujo. Si queda abierto no pasa
  nada: se apaga solo tras 30 minutos sin uso. Para ver qué hay vivo, `ldt status`.
- **Un login lo hace siempre el usuario, a mano.** Nunca hardcodear ni tipear credenciales.
  Si una ruta pide login, `ldt browser check` lo detecta, deja el Chromium **visible** y
  frena (exit code 2); ahí hay que pedirle al usuario que se loguee en esa ventana
  (`ldt browser login <url>`). Ese login vale **sólo mientras ese browser siga abierto**:
  después de loguearse, no cerrarlo hasta terminar. Si se sabe de antemano que la ruta pide
  login, empezar directo por `ldt browser login`.
- **Si el repo tiene su propio navegador o su propio runner, usar ese.** Un proyecto con un
  entorno fijado (su `.venv`, sus versiones) no se prueba con el Chromium de `ldt`: sería
  probar algo distinto de lo que corre de verdad. `ldt browser` es para las UIs; el resto
  de `ldt` (`db`, `ports`, `dev`, `http`, `env`, `scan`) sirve igual en cualquier caso.
- `ldt db` es read-only por defecto; escribir necesita `--write` **y** que el usuario lo
  autorice, porque la URL del proyecto suele apuntar a una base real.
- `ldt env` nunca imprime secretos salvo `--reveal`, que no hay que usar por iniciativa
  propia.
- El detalle está en `ldt help` y `ldt <grupo> --help`.
