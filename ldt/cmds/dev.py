"""`ldt dev` — levantar el server de desarrollo en background y leer sus logs.

Un agente necesita el server corriendo para probar cualquier cosa en el browser, pero un
proceso que vive dentro de la sesion se muere con ella. Esto lo despega: el proceso
sobrevive, la salida queda en un archivo y `ldt dev logs` la lee cuando haga falta.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ldt import core, ui
from ldt.cmds.ports import free_port, kill_pid, listeners, owner

PROCS = core.PROCS

# Frameworks cuyo script `dev` entiende `--port`: si el puerto esta ocupado por algo que
# no es nuestro nos corremos a otro, y hay que avisarle al server a cual.
PORT_FLAG = ("next", "vite", "astro", "nuxt", "remix", "angular", "sveltekit")

# Donde va el puerto en los comandos que arma `core.detect` cuando ya traen uno. Los que
# no traen puerto ni aparecen aca (CRA, express, ...) lo leen de la variable PORT.
PORT_ARGS = (
    (r"--port[= ]\d+", "--port {port}"),
    (r"--web-port[= ]\d+", "--web-port {port}"),  # flutter web
    (r"runserver \d+", "runserver {port}"),  # django
    (r"(?<= )-p \d+", "-p {port}"),  # rails
    (r"-S (\S+):\d+", "-S {0}:{port}"),  # php -S host:puerto
)


def proc_file(name: str) -> Path:
    return PROCS / f"{name}.json"


def log_file(name: str) -> Path:
    return core.LOGS / f"{name}.log"


def load(name: str) -> dict | None:
    f = proc_file(name)
    return core.read_json(f) if f.exists() else None


running = core.running


def default_name(root: Path) -> str:
    return core.project_name(root, lambda name: (load(name) or {}).get("cwd"))


ldt_owns = core.ldt_owns


def with_port(cmd: str, port: int, info: dict, explicit: bool) -> str:
    """Le pasa el puerto elegido al comando de dev, si sabemos como.

    Con `--cmd` del usuario no se toca nada: el que escribio el comando manda.
    """
    if explicit:
        return cmd
    for pattern, template in PORT_ARGS:
        if re.search(pattern, cmd):
            return re.sub(pattern, lambda m: template.format(*m.groups(), port=port), cmd, count=1)
    framework = (info.get("framework") or "").split("@")[0]
    if framework in PORT_FLAG:
        # `npm run` se queda con los flags que no van despues de `--`.
        sep = " --" if cmd.startswith("npm run ") else ""
        return f"{cmd}{sep} --port {port}"
    return cmd


def cmd_start(a):
    core.ensure_dirs()
    PROCS.mkdir(parents=True, exist_ok=True)
    root = core.find_root(Path(a.cwd))
    info = core.detect(root)
    name = a.name or default_name(root)
    cmd = a.cmd or info["dev_cmd"]
    if not cmd:
        core.die(f"no supe que correr en {root}. Pasalo con --cmd \"pnpm dev\"")

    prev = load(name)
    if prev and running(prev["pid"]):
        if not a.restart:
            core.die(f"'{name}' ya esta corriendo (pid {prev['pid']}). Usá --restart o `ldt dev stop {name}`")
        stop_proc(name)

    port = a.port or info["port"]
    notes: list[str] = []
    before = {(r["port"], r["pid"]) for r in listeners()}
    if port:
        taken = owner(port)
        held = ldt_owns(taken["pid"]) if taken else None
        if taken and a.force:
            kill_pid(taken["pid"])
            notes.append(f":{port} lo tenia {taken['process']} (pid {taken['pid']}) - lo mate por --force")
        elif held and core.belongs(held.get("cwd"), root):
            # Sobra de una corrida nuestra anterior: esa si es basura y se limpia sola.
            kill_pid(taken["pid"])
            notes.append(f":{port} lo tenia una corrida vieja de ldt - liberado")
        elif taken:
            # No es nuestro: o lo levanto el usuario a mano, o es el dev server de ldt de
            # *otro* proyecto, que puede ser el de otra sesion de agente trabajando en
            # paralelo (dos repos con :3000 por defecto). Antes se mataba en los dos casos.
            # Ahora nos corremos.
            who = (
                f"el dev server '{held['name']}' de ldt (otro proyecto: {held.get('cwd')})"
                if held
                else f"{taken['process']} (pid {taken['pid']}, no es de ldt)"
            )
            other = free_port(port + 1)
            if a.port:
                core.die(f":{port} lo tiene {who}. Usa --force para matarlo, o --port {other} para arrancar al lado")
            if not other:
                core.die(f":{port} ocupado y no hay ninguno libre cerca. Liberalo o pasa --port")
            notes.append(f":{port} lo tiene {who} - arranco en :{other}")
            port = other
        cmd = with_port(cmd, port, info, explicit=bool(a.cmd))

    log = log_file(name)
    log.write_text("", encoding="utf-8")
    handle = open(log, "ab")
    kwargs: dict = {
        "cwd": str(root),
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "shell": True,  # hace falta para resolver pnpm.cmd / npm.cmd en Windows
        "env": {**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1", **({"PORT": str(port)} if port else {})},
    }
    if core.IS_WIN:
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: sin ventana y fuera del grupo de
        # la consola (asi no se lleva un Ctrl+C ajeno), pero conservando la redireccion
        # de stdout. DETACHED_PROCESS tambien sobrevive al padre, pero se come la salida:
        # el log quedaba vacio.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)

    meta = {"name": name, "pid": proc.pid, "cmd": cmd, "cwd": str(root), "port": port, "started_at": time.time()}
    proc_file(name).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    time.sleep(a.settle)
    # Quien quedo escuchando y no estaba antes es nuestro hijo real: con shell=True el pid
    # que guardamos es el del shell. Anotarlo es lo que despues deja matar lo nuestro sin
    # barrer a ciegas al que tenga el puerto.
    if port:
        now = owner(port)
        if now and (port, now["pid"]) not in before:
            meta["port_pid"] = now["pid"]
            proc_file(name).write_text(json.dumps(meta, indent=2), encoding="utf-8")

    tail = log.read_text(encoding="utf-8", errors="replace")[-1500:]
    # Un `dev start` que dice "arrancado" sobre un proceso que ya se murio manda a buscar
    # el problema al lugar equivocado. El caso tipico es el puerto ocupado.
    died = not running(proc.pid) and not meta.get("port_pid")
    meta["alive"] = not died
    if died:
        lines = notes + [f"{ui.sym('err')} '{name}' murio al arrancar", f"$ {cmd}", f"log: {log}", "", tail]
        core.out({**meta, "log": str(log), "tail": tail, "notes": notes}, a.json, "\n".join(lines))
        raise SystemExit(1)

    lines = notes + [
        f"{ui.sym('ok')} '{name}' arrancado (pid {proc.pid}) en {root}",
        f"$ {cmd}",
        f"log: {log}",
        "sigue corriendo entre comandos; para verlo: `ldt dev logs` / `ldt status`",
        "",
        tail,
    ]
    core.out({**meta, "log": str(log), "tail": tail, "notes": notes}, a.json, "\n".join(lines))


def stop_proc(name: str) -> bool:
    meta = load(name)
    if not meta:
        return False
    ok = kill_pid(meta["pid"])
    # Solo el hijo que anotamos al arrancar. Antes se barria el puerto entero: si el
    # proceso ya habia muerto y otra cosa lo habia tomado, se mataba lo ajeno.
    if meta.get("port_pid"):
        kill_pid(meta["port_pid"])
    proc_file(name).unlink(missing_ok=True)
    return ok


def cmd_stop(a):
    if a.all:
        # Scopeado al proyecto, igual que `cleanup`: puede haber otra sesion de agente
        # trabajando en otro repo. `--global` para el barrido completo.
        root = core.find_root(Path(a.cwd))
        names = [
            f.stem
            for f in PROCS.glob("*.json")
            if getattr(a, "glob_all", False) or core.belongs(core.read_json(f).get("cwd"), root)
        ]
    else:
        names = [a.name or default_name(core.find_root(Path(a.cwd)))]
    stopped = [n for n in names if stop_proc(n)]
    core.out({"stopped": stopped}, a.json, f"detenidos: {', '.join(stopped) or '(ninguno)'}")


URL_RE = re.compile(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d+)[^\s'\"<>]*")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def log_url(name: str, port: int | None) -> str | None:
    """La URL que el server anuncio en su log ("Local: http://localhost:5173/").

    Se lee solo el principio: es donde la imprimen, y el log de un server que corre hace
    horas puede pesar megas. Si hay varias se prefiere la del puerto que le dimos.
    """
    log = log_file(name)
    try:
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            head = ANSI_RE.sub("", fh.read(64 * 1024))
    except OSError:
        return None
    found = [(m.group(0).rstrip(".,;)"), int(m.group(1))) for m in URL_RE.finditer(head)]
    if not found:
        return None
    url = next((u for u, p in found if p == port), found[0][0])
    # "Escucho en todas las interfaces" no es una direccion que se pueda abrir.
    return url.replace("0.0.0.0", "localhost").replace("[::]", "localhost")


def live(meta: dict, rows: list[dict] | None = None) -> dict:
    """Estado de una corrida: si sigue viva, si ya escucha, y en que URL.

    `ready` es lo que tiene que mirar quien arranca con `--settle 0` y hace polling: el
    proceso puede estar vivo varios segundos (compilando) antes de tomar el puerto.

    Si el hijo real todavia no estaba anotado (`--settle 0` vuelve antes de que escuche),
    se anota aca: al arrancar el puerto estaba libre, asi que quien lo tome mientras el
    proceso siga vivo es el. Sin eso `stop` y `ldt_owns` no lo reconocen.
    """
    alive = running(meta.get("pid", -1))
    port = meta.get("port")
    listening = False
    if port:
        rows = listeners() if rows is None else rows
        who = next((r for r in rows if r["port"] == port), None)
        if who and meta.get("port_pid"):
            listening = who["pid"] == meta["port_pid"]
        elif who and alive:
            listening = True
            if who["pid"]:
                meta["port_pid"] = who["pid"]
                proc_file(meta["name"]).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    # Sin puerto (docker compose, flutter a un dispositivo) no hay forma de saber cuando
    # termino de arrancar: vivo es lo mas que se puede decir.
    ready = listening if port else alive
    url = log_url(meta["name"], port) if ready else None
    if ready and not url and port:
        url = f"http://localhost:{port}"
    return {
        "name": meta.get("name"),
        "pid": meta.get("pid"),
        "port_pid": meta.get("port_pid"),
        "alive": alive or listening,
        "ready": ready,
        "port": port,
        "url": url,
        "cmd": meta.get("cmd"),
        "cwd": meta.get("cwd"),
        "log": str(log_file(meta["name"])),
        "uptime_s": round(time.time() - meta.get("started_at", time.time())),
    }


def all_live() -> list[dict]:
    PROCS.mkdir(parents=True, exist_ok=True)
    metas = [m for m in (core.read_json(f) for f in sorted(PROCS.glob("*.json"))) if m.get("name")]
    rows = listeners() if any(m.get("port") for m in metas) else []
    return [live(m, rows) for m in metas]


def cmd_list(a):
    procs = all_live()
    cols = ["name", "pid", "alive", "ready", "port", "url", "uptime_s"]
    core.out({"procs": procs}, a.json, core.table(procs, cols))


def cmd_logs(a):
    name = a.name or default_name(core.find_root(Path(a.cwd)))
    log = log_file(name)
    if not log.exists():
        core.die(f"no hay log para '{name}' (arrancalo con: ldt dev start)")
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    if a.grep:
        lines = [l for l in lines if a.grep.lower() in l.lower()]
    if a.errors:
        needles = ("error", "failed", "exception", "traceback", "warn", "econnrefused")
        lines = [l for l in lines if any(n in l.lower() for n in needles)]
    tail = lines[-a.tail :]
    core.out({"name": name, "log": str(log), "lines": tail}, a.json, "\n".join(tail) or "(log vacio)")
    if getattr(a, "follow", False):
        follow(log, a.limit)


def follow(log: Path, limit: float) -> None:
    """Sigue el log imprimiendo solo lo nuevo.

    Sin TTY (el caso del agente) se corta solo: un comando que no termina nunca bloquea
    el turno entero.
    """
    import sys

    if limit <= 0:
        limit = 3600 if sys.stdout.isatty() else 60
    pos = log.stat().st_size
    deadline = time.time() + limit
    try:
        while time.time() < deadline:
            size = log.stat().st_size
            if size < pos:
                pos = 0  # el log se trunco: arranco de nuevo
            if size > pos:
                with log.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                print(chunk, end="", flush=True)
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass


def register(sub):
    p = sub.add_parser("dev", help="server de desarrollo en background + sus logs")
    ds = p.add_subparsers(dest="dev_cmd", required=True)

    sp = ds.add_parser("start", help="arrancar el server de dev del proyecto")
    sp.add_argument("--cmd", help='comando a correr (default: el detectado, p.ej. "pnpm dev")')
    sp.add_argument("--name", help="nombre del proceso (default: el del directorio)")
    sp.add_argument("--port", type=int, help="puerto a liberar antes de arrancar")
    sp.add_argument("--restart", action="store_true", help="matar la corrida anterior si existe")
    sp.add_argument(
        "--force",
        action="store_true",
        help="matar lo que tenga el puerto aunque no sea de ldt (por defecto arranca en otro)",
    )
    sp.add_argument("--settle", type=float, default=4, help="segundos a esperar antes de mostrar el log")
    sp.set_defaults(func=cmd_start)

    sp = ds.add_parser("stop", help="detener el server")
    sp.add_argument("name", nargs="?")
    sp.add_argument("--all", action="store_true", help="todos los de este proyecto")
    sp.add_argument(
        "--global", dest="glob_all", action="store_true", help="con --all, tambien los de otros proyectos"
    )
    sp.set_defaults(func=cmd_stop)

    sp = ds.add_parser("list", help="procesos levantados por ldt")
    sp.set_defaults(func=cmd_list)

    sp = ds.add_parser("logs", help="leer el log del server")
    sp.add_argument("name", nargs="?")
    sp.add_argument("-n", "--tail", type=int, default=60)
    sp.add_argument("--grep", help="filtrar lineas que contengan este texto")
    sp.add_argument("--errors", action="store_true", help="solo lineas que parezcan errores")
    sp.add_argument("-f", "--follow", action="store_true", help="seguir el log en vivo")
    sp.add_argument(
        "--for", dest="limit", type=float, default=0, help="con --follow, cortar tras N segundos"
    )
    sp.set_defaults(func=cmd_logs)
