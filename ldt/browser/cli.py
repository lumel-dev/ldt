"""`ldt browser` — navegador manejable desde la linea de comandos.

Pensado para que un agente pueda probar de verdad lo que acaba de escribir: abrir la app,
interactuar, y leer errores de consola y requests fallidos sin adivinar.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from ldt import core, ui
from ldt.browser import client


def _launch_opts(a) -> dict:
    return {
        "headless": not a.headed,
        "profile": a.profile,
        "width": a.width,
        "height": a.height,
        "device": a.device,
        "timeout": a.timeout,
        "idle_timeout": a.idle_timeout,
        # De que proyecto es este Chromium: `ldt cleanup` cierra el de acá y no el de la
        # sesion de agente que esta trabajando en otro repo.
        "project": str(core.find_root(Path(a.cwd))),
    }


def default_session(root: Path) -> str:
    """Una sesion por proyecto, no una compartida.

    Antes todas se llamaban `default`: dos agentes en repos distintos manejaban el mismo
    Chromium, uno le navegaba la pagina al otro en medio de una prueba, y si pedia otros
    flags (`--headed`) se lo relanzaba y le borraba el login. `LDT_BROWSER_SESSION` o `-s`
    la cambian, p.ej. para dos agentes sobre el mismo repo.
    """
    env = os.environ.get("LDT_BROWSER_SESSION")
    if env:
        return env
    return core.project_name(root, lambda name: ((client.read_session(name) or {}).get("opts") or {}).get("project"))


def _in_session(fn):
    def run(a):
        if not a.session:
            a.session = default_session(core.find_root(Path(a.cwd)))
        return fn(a)

    return run


def _relaunch_needed(a, have: dict, opts: dict) -> list[str]:
    """Que cambios pedidos explicitamente obligan a levantar el browser de nuevo.

    Un flag es un **pedido**, no un estado completo: `--headed` pide ver la ventana, pero
    su ausencia no pide volver a headless. Si no fuera asi, despues de un `ldt browser
    login` el primer `check` sin flags relanzaria en headless y tiraria a la basura el
    login que la persona acaba de hacer a mano.
    """
    changed = []
    if ("headless" in opts or a.headed) and opts.get("headless", not a.headed) != have.get("headless", True):
        changed.append(("headless", have.get("headless", True), opts.get("headless", not a.headed)))
    if a.profile and a.profile != have.get("profile"):
        changed.append(("profile", have.get("profile"), a.profile))
    if a.device and a.device != have.get("device"):
        changed.append(("device", have.get("device"), a.device))
    return [f"{k}: {old} -> {new}" for k, old, new in changed]


def _need(a, **opts) -> bool:
    """Toda operacion asume una sesion viva; si no hay, se levanta con los defaults.

    Devuelve True cuando la levanto en este comando: ahi vale la pena recordar el cierre.
    """
    want = {**_launch_opts(a), **opts}
    info = client.read_session(a.session)
    if info and client.alive(a.session):
        have = info.get("opts") or {}
        changed = _relaunch_needed(a, have, opts)
        if changed:
            # Antes esto se ignoraba en silencio: pedias --headed y seguias viendo headless.
            print(f"relanzando el browser ({', '.join(changed)}); se pierde la sesion actual", file=sys.stderr)
            client.stop(a.session)
        else:
            # Conservar lo que la sesion ya tiene: si la levantamos visible para un login,
            # el comando siguiente no tiene por que devolverla a headless.
            want = {**want, **{k: have[k] for k in ("headless", "profile", "device") if k in have}}
    _, started = client.ensure(a.session, **want)
    return started


# El browser queda vivo a proposito: es lo que permite probar una feature en varios pasos
# sin perder cookies ni el estado de la pagina. Cerrarlo antes de terminar obliga a rehacer
# todo, asi que el recordatorio apunta a que se apaga solo.
CLEANUP_HINT = "\n(la sesion queda abierta; se apaga sola tras 30 min sin uso, o con `ldt cleanup`)"


LOGIN_TIP = (
    "hace falta un login manual ({reasons}).\n"
    "   abri el Chromium visible y logueate:\n\n"
    "   ldt browser login {url}\n\n"
    "(la sesion vive mientras el browser este abierto: no lo cierres hasta terminar)"
)


def _login_guard(a) -> bool:
    """Si la pagina pide login, deja el browser visible y frena.

    Se dispara temprano a proposito: como el contexto es efimero, relanzar en modo visible
    pierde lo que hubiera en la sesion, y cuanto antes pase menos se pierde.
    """
    if getattr(a, "no_auto_login", False) or a.headed:
        return False
    try:
        hint = client.call(a.session, "login_hint")
    except client.BrowserError:
        return False
    if not hint.get("likely"):
        return False
    info = client.read_session(a.session) or {}
    if (info.get("opts") or {}).get("ldt_login_prompted"):
        return False  # ya avisamos por esta sesion: no entrar en loop de relanzados

    url = hint.get("url") or ""
    client.stop(a.session)
    client.ensure(a.session, **{**_launch_opts(a), "headless": False, "ldt_login_prompted": True})
    if url:
        client.call(a.session, "goto", {"url": url})
    print()
    print(ui.warn("!! " + LOGIN_TIP.format(reasons=", ".join(hint.get("reasons") or []), url=url)))
    return True


def cmd_login(a):
    """Abre el Chromium visible para que el login lo haga una persona.

    Nunca tipear credenciales desde aca: el punto es justamente que las ponga el usuario.
    """
    started = _need(a, headless=False)
    if a.url:
        client.call(a.session, "goto", {"url": a.url, "wait": "load"})
    client.call(a.session, "keepalive", {"seconds": a.keepalive})
    before = len(client.call(a.session, "cookies").get("cookies") or [])
    where = client.call(a.session, "login_hint")

    print(f"Chromium visible abierto en {where.get('url')}")
    print("logueate en la ventana; la sesion vive mientras el browser siga abierto.")

    # Hay alguien mirando solo si la terminal esta de los dos lados: con stdout capturado
    # (un agente) nadie va a leer el prompt ni apretar Enter.
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            input("  [Enter] cuando termines > ")
        except (EOFError, KeyboardInterrupt):
            pass
    else:
        # Sin terminal (el caso del agente) no se puede bloquear esperando un Enter que
        # nadie va a escribir: se espera a que la pagina deje de ser la de login.
        deadline = time.time() + a.wait_for
        while time.time() < deadline:
            time.sleep(2)
            try:
                now = client.call(a.session, "login_hint")
            except client.BrowserError:
                break
            if a.until_url and re.search(a.until_url, now.get("url") or ""):
                break
            if not a.until_url and not now.get("likely"):
                break

    after = client.call(a.session, "cookies").get("cookies") or []
    final = client.call(a.session, "login_hint")
    done = not final.get("likely")
    res = {
        "url": final.get("url"),
        "cookies": len(after),
        "new_cookies": len(after) - before,
        "logged_in": done,
        "started": started,
    }
    mark = ui.sym("ok") if done else ui.sym("warn")
    text = (
        f"{mark} {final.get('url')}\n"
        f"cookies: {len(after)} ({len(after) - before} nuevas)\n"
        + ("sesion iniciada." if done else "sigue pareciendo la pagina de login.")
        + "\nno cierres el browser hasta terminar de probar: al cerrarlo se pierde el login."
    )
    core.out(res, a.json, text)


def _errors_text(err: dict) -> str:
    lines = []
    for e in err.get("page_errors", []):
        lines.append(f"  [pageerror] {e['text'].splitlines()[0][:200]}")
    for c in err.get("console_errors", []):
        lines.append(f"  [console]   {c['text'][:200]}")
    for r in err.get("failed_requests", []):
        state = r["failure"] or f"HTTP {r['status']}"
        lines.append(f"  [network]   {r['method']} {r['url']} -> {state}")
    return "\n".join(lines)


# ------------------------------------------------------------------------ comandos


def cmd_open(a):
    if a.fresh:
        client.stop(a.session)
    started = _need(a)
    res = client.call(a.session, "goto", {"url": a.url, "wait": a.wait}) if a.url else client.call(a.session, "status")
    text = f"{res.get('status', '')} {res.get('url', a.session)}\n{res.get('title', '')}".strip()
    core.out(res, a.json, text + (CLEANUP_HINT if started else ""))


def cmd_goto(a):
    started = _need(a)
    res = client.call(a.session, "goto", {"url": a.url, "wait": a.wait})
    if _login_guard(a):
        sys.exit(2)
    core.out(res, a.json, f"HTTP {res['status']} {res['url']}\n{res['title']}")


def cmd_simple(op: str, arg_names: tuple[str, ...] = ()):
    def run(a):
        _need(a)
        args = {name: getattr(a, name) for name in arg_names if getattr(a, name, None) is not None}
        res = client.call(a.session, op, args)
        core.out(res, a.json, json.dumps(res, ensure_ascii=False)[:2000])

    return run


def cmd_text(a):
    _need(a)
    res = client.call(a.session, "text", {"target": a.target, "limit": a.limit})
    core.out(res, a.json, res["text"] + ("\n... (truncado)" if res["truncated"] else ""))


def cmd_html(a):
    _need(a)
    res = client.call(a.session, "html", {"target": a.target, "limit": a.limit})
    core.out(res, a.json, res["html"] + ("\n... (truncado)" if res["truncated"] else ""))


def cmd_snapshot(a):
    _need(a)
    res = client.call(a.session, "snapshot", {"target": a.target, "limit": a.limit})
    core.out(res, a.json, res["snapshot"] + ("\n... (truncado)" if res["truncated"] else ""))


def cmd_elements(a):
    _need(a)
    res = client.call(a.session, "elements")
    rows = [
        {
            "ref": f"@{e['ref']}",
            "tag": e["tag"] + (f":{e['type']}" if e["type"] else ""),
            "label": e["label"],
            "name": e["name"],
            "href": e["href"],
        }
        for e in res["elements"]
        if not a.viewport or e["in_viewport"]
    ]
    core.out(res, a.json, core.table(rows) + f"\n\n{len(rows)} elementos. Usá @N como target.")


def cmd_shot(a):
    _need(a)
    res = client.call(a.session, "screenshot", {"path": a.path, "full_page": a.full, "target": a.target})
    core.out(res, a.json, res["path"])


def cmd_console(a):
    _need(a)
    res = client.call(a.session, "console", {"level": a.level, "limit": a.limit, "clear": a.clear})
    text = "\n".join(f"[{c['type']}] {c['text']}  ({c['at']})" for c in res["console"]) or "(consola limpia)"
    core.out(res, a.json, text)


def cmd_network(a):
    _need(a)
    res = client.call(a.session, "network", {"limit": a.limit, "clear": a.clear})
    text = "\n".join(
        f"{r['method']} {r['url']} -> {r['failure'] or ('HTTP ' + str(r['status']))}" for r in res["requests"]
    ) or "(sin requests fallidos)"
    core.out(res, a.json, text)


def cmd_errors(a):
    _need(a)
    res = client.call(a.session, "errors")
    text = _errors_text(res) or "sin errores"
    core.out(res, a.json, f"{res['url']}\n{text}")
    if res["count"] and a.strict:
        sys.exit(1)


def cmd_wait(a):
    _need(a)
    res = client.call(
        a.session,
        "wait",
        {"target": a.target, "text": a.text, "url": a.url, "state": a.state, "ms": a.ms, "timeout": a.timeout},
    )
    core.out(res, a.json, f"{res['url']}\n{res['title']}")


def cmd_status(a):
    if not client.alive(a.session):
        core.out({"alive": False, "session": a.session}, a.json, f"sesion '{a.session}': cerrada")
        return
    res = client.call(a.session, "status")
    rows = [{"tab": p["index"], "url": p["url"], "title": p["title"]} for p in res["pages"]]
    core.out(res, a.json, f"sesion '{a.session}' viva ({res['uptime_s']}s)\n" + core.table(rows))


def cmd_sessions(a):
    items = client.sessions()
    rows = [
        {"name": s["name"], "pid": s["pid"], "port": s["port"], "estado": client.state(s["name"])}
        for s in items
    ]
    # Un huerfano es un Chromium vivo que ya no es la sesion registrada: sin esto no
    # aparece en ningun lado y se acumula hasta que alguien mira el administrador de tareas.
    orphans = client.orphans()
    rows += [{"name": o["name"], "pid": o["pid"], "port": o["port"], "estado": "huerfano"} for o in orphans]
    text = core.table(rows)
    if orphans:
        text += f"\n{len(orphans)} huerfano(s): quedaron sin sesion. Los cierra: ldt cleanup"
    core.out({"sessions": items, "orphans": orphans}, a.json, text)


def cmd_close(a):
    names = [a.session]
    kept: list[str] = []
    if a.all:
        # Scopeado al proyecto, como `dev stop --all` y `cleanup`: los browsers de otros
        # repos pueden ser de otra sesion de agente a mitad de una prueba.
        root = core.find_root(Path(a.cwd))
        names = []
        for s in client.sessions():
            if a.glob_all or core.belongs((s.get("opts") or {}).get("project"), root):
                names.append(s["name"])
            else:
                kept.append(s["name"])
    closed = [n for n in names if client.stop(n)]
    reaped = client.reap() if a.all else []
    text = f"cerradas: {', '.join(closed) or '(ninguna)'}"
    if reaped:
        text += f"\nhuerfanos cerrados: {', '.join(str(o['pid']) for o in reaped)}"
    if kept:
        text += f"\nde otros proyectos, sin tocar: {', '.join(kept)}  (`--all --global` las cierra tambien)"
    core.out({"closed": closed, "reaped": reaped, "kept": kept}, a.json, text)


def cmd_tabs(a):
    _need(a)
    res = client.call(a.session, "tabs", {"index": a.index, "url": a.url, "close": a.close})
    rows = [{"tab": p["index"], "url": p["url"], "title": p["title"]} for p in res["pages"]]
    core.out(res, a.json, core.table(rows) + f"\nactiva: {res['current']}")


def cmd_check(a):
    """Chequeo de una URL de punta a punta: carga, errores y screenshot en un solo paso."""
    started = _need(a)
    client.call(a.session, "clear")
    nav = client.call(a.session, "goto", {"url": a.url, "wait": "load"})
    try:
        client.call(a.session, "wait", {"state": "networkidle", "timeout": a.settle})
    except client.BrowserError:
        pass  # networkidle no llega nunca en apps con polling o websockets: no es un fallo
    err = client.call(a.session, "errors")
    shot = client.call(a.session, "screenshot", {"full_page": a.full})
    title = client.call(a.session, "status")["pages"]
    result = {"nav": nav, "errors": err, "screenshot": shot["path"], "pages": title}
    problems = err["count"] or not nav["ok"]
    text = (
        f"HTTP {nav['status']} {nav['url']}\n"
        f"titulo: {nav['title']}\n"
        f"problemas: {err['count']}\n"
        f"{_errors_text(err)}\n"
        f"screenshot: {shot['path']}"
    )
    core.out(result, a.json, text + (CLEANUP_HINT if started else ""))
    if _login_guard(a):
        sys.exit(2)
    if problems and a.strict:
        sys.exit(1)


# -------------------------------------------------------------------------- parser


def register(sub):
    p = sub.add_parser("browser", help="navegador controlable (Playwright) con sesion persistente")
    p.add_argument("-s", "--session", help="nombre de sesion (default: el del proyecto)")
    p.add_argument("--headed", action="store_true", help="mostrar la ventana del navegador")
    p.add_argument("--profile", help="perfil persistente: reusa el login entre corridas")
    p.add_argument("--width", type=int, default=1440)
    p.add_argument("--height", type=int, default=900)
    p.add_argument("--device", help='emular un dispositivo, p.ej. "iPhone 15"')
    p.add_argument("--timeout", type=float, default=15, help="timeout por accion, en segundos")
    p.add_argument("--idle-timeout", type=float, default=1800, help="cerrar la sesion tras N segundos sin uso")
    p.add_argument(
        "--no-auto-login",
        action="store_true",
        help="no frenar ni abrir la ventana aunque la pagina parezca pedir login",
    )
    bs = p.add_subparsers(dest="browser_cmd", required=True)

    def add(name, fn, help_):
        sp = bs.add_parser(name, help=help_)
        sp.set_defaults(func=_in_session(fn))
        return sp

    sp = add("open", cmd_open, "abrir la sesion (y opcionalmente una URL)")
    sp.add_argument("url", nargs="?")
    sp.add_argument("--wait", default="load", choices=["load", "domcontentloaded", "networkidle", "commit"])
    sp.add_argument("--fresh", action="store_true", help="cerrar la sesion previa y arrancar limpia")

    sp = add("goto", cmd_goto, "navegar a una URL")
    sp.add_argument("url")
    sp.add_argument("--wait", default="load", choices=["load", "domcontentloaded", "networkidle", "commit"])

    sp = add("check", cmd_check, "cargar una URL y reportar errores + screenshot (chequeo completo)")
    sp.add_argument("url")
    sp.add_argument("--full", action="store_true", help="screenshot de la pagina entera")
    sp.add_argument("--settle", type=float, default=5, help="segundos a esperar a que la red se calme")
    sp.add_argument("--strict", action="store_true", help="exit code 1 si hay errores")

    sp = add("login", cmd_login, "abrir el Chromium VISIBLE para loguearse a mano")
    sp.add_argument("url", nargs="?")
    sp.add_argument("--wait-for", type=float, default=300, help="segundos a esperar el login sin terminal")
    sp.add_argument("--until-url", help="patron de URL que confirma que el login termino")
    sp.add_argument("--keepalive", type=float, default=7200, help="subir el idle timeout de la sesion")

    sp = add("click", cmd_simple("click", ("target", "count", "force")), "click en @ref o selector")
    sp.add_argument("target")
    sp.add_argument("--count", type=int, default=1)
    sp.add_argument("--force", action="store_true")

    sp = add("fill", cmd_simple("fill", ("target", "value")), "escribir en un input (lo vacia primero)")
    sp.add_argument("target")
    sp.add_argument("value")

    sp = add("type", cmd_simple("type", ("target", "value", "delay")), "tipear tecla por tecla (dispara autocompletes)")
    sp.add_argument("target")
    sp.add_argument("value")
    sp.add_argument("--delay", type=int, default=30)

    sp = add("press", cmd_simple("press", ("key", "target")), "apretar una tecla (Enter, Escape, Control+a)")
    sp.add_argument("key")
    sp.add_argument("--target")

    sp = add("hover", cmd_simple("hover", ("target",)), "hover sobre un elemento")
    sp.add_argument("target")

    sp = add("select", cmd_simple("select", ("target", "value")), "elegir opcion de un <select>")
    sp.add_argument("target")
    sp.add_argument("value")

    sp = add("tick", cmd_simple("check", ("target", "checked")), "marcar/desmarcar un checkbox")
    sp.add_argument("target")
    sp.add_argument("--off", dest="checked", action="store_false", default=True)

    sp = add("upload", cmd_simple("upload", ("target", "files")), "subir archivos a un input file")
    sp.add_argument("target")
    sp.add_argument("files", nargs="+")

    sp = add("scroll", cmd_simple("scroll", ("to",)), "scrollear (bottom | top | selector)")
    sp.add_argument("to", nargs="?", default="bottom")

    sp = add("text", cmd_text, "texto visible de la pagina o de un selector")
    sp.add_argument("target", nargs="?", default="body")
    sp.add_argument("--limit", type=int, default=20000)

    sp = add("html", cmd_html, "HTML de la pagina o de un selector")
    sp.add_argument("target", nargs="?", default="html")
    sp.add_argument("--limit", type=int, default=20000)

    sp = add("snapshot", cmd_snapshot, "arbol ARIA (barato en tokens, mejor que el HTML)")
    sp.add_argument("target", nargs="?", default="body")
    sp.add_argument("--limit", type=int, default=20000)

    sp = add("elements", cmd_elements, "listar elementos interactivos con refs @N")
    sp.add_argument("--viewport", action="store_true", help="solo los visibles en pantalla")

    sp = add("eval", cmd_simple("eval", ("expression",)), "evaluar JS en la pagina")
    sp.add_argument("expression")

    sp = add("shot", cmd_shot, "screenshot (devuelve el path para leerlo con Read)")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--full", action="store_true")
    sp.add_argument("--target")

    sp = add("pdf", cmd_simple("pdf", ("path",)), "imprimir la pagina a PDF")
    sp.add_argument("path", nargs="?")

    sp = add("console", cmd_console, "mensajes de consola capturados")
    sp.add_argument("--level", choices=["error", "warn", "info", "log", "debug"])
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--clear", action="store_true")

    sp = add("network", cmd_network, "requests fallidos o con status >= 400")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--clear", action="store_true")

    sp = add("errors", cmd_errors, "todo lo que fallo en la pagina, junto")
    sp.add_argument("--strict", action="store_true", help="exit code 1 si hay errores")

    add("clear", cmd_simple("clear"), "vaciar los buffers de consola y red")

    sp = add("wait", cmd_wait, "esperar por selector, texto, URL o estado de carga")
    sp.add_argument("--target")
    sp.add_argument("--text")
    sp.add_argument("--url")
    sp.add_argument("--state", choices=["load", "domcontentloaded", "networkidle"])
    sp.add_argument("--ms", type=int)

    sp = add("tabs", cmd_tabs, "listar / cambiar / abrir / cerrar pestanas")
    sp.add_argument("index", nargs="?", type=int)
    sp.add_argument("--url", help="abrir una pestana nueva en esta URL")
    sp.add_argument("--close", action="store_true")

    add("cookies", cmd_simple("cookies"), "cookies del contexto")
    add("storage", cmd_simple("storage"), "localStorage y sessionStorage")
    add("reload", cmd_simple("reload"), "recargar")
    add("back", cmd_simple("back"), "atras")
    add("forward", cmd_simple("forward"), "adelante")
    add("status", cmd_status, "estado de la sesion")
    add("sessions", cmd_sessions, "sesiones de browser abiertas")

    sp = add("close", cmd_close, "cerrar la sesion (y el Chromium)")
    sp.add_argument("--all", action="store_true", help="todas las de este proyecto")
    sp.add_argument(
        "--global", dest="glob_all", action="store_true", help="con --all, tambien las de otros proyectos"
    )
