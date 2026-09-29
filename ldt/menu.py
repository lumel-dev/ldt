"""Menu interactivo de `ldt`: elegir con las flechas en vez de escribir el comando.

Se abre solo cuando `ldt` se corre a mano en una terminal. Si la salida esta capturada
(el caso del agente), si se paso `--json`, o si no hay TTY, no se abre nada: un menu que
espera una tecla en un proceso sin stdin cuelga el turno entero.

Los items son declarativos y terminan siempre en un `argv` que se pasa por el mismo parser
que la linea de comandos, asi que el menu no puede quedar haciendo algo distinto de lo que
hace el CLI.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ldt import core, ui


@dataclass
class Item:
    label: str
    hint: str = ""
    argv: list[str] | None = None
    prompts: tuple[tuple[str, str], ...] = ()  # (texto, default) -> se appendean al argv
    submenu: str | None = None
    action: str | None = None  # "back" | "quit"
    separator: bool = False


@dataclass
class Menu:
    title: str
    items: list[Item] = field(default_factory=list)


# ------------------------------------------------------------------------ teclado


def _read_key() -> str:
    """Una tecla. Devuelve 'up','down','enter','quit', o el caracter."""
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # prefijo de tecla especial
            code = msvcrt.getwch()
            return {"H": "up", "P": "down", "K": "back", "M": "enter"}.get(code, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch == "\x1b":
            return "quit"
        return ch
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            nxt = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[D": "back"}.get(nxt, "quit")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def interactive() -> bool:
    """Si tiene sentido dibujar un menu."""
    if os.environ.get("LDT_NO_MENU"):
        return False
    for stream in (sys.stdin, sys.stdout):
        if not getattr(stream, "isatty", lambda: False)():
            return False
    return True


# -------------------------------------------------------------------------- dibujo


def _choosable(items: list[Item]) -> list[int]:
    return [i for i, it in enumerate(items) if not it.separator]


def _render(menu: Menu, cursor: int, header: list[str]) -> list[str]:
    lines = list(header)
    if menu.title:
        lines += [ui.section(menu.title), ""]
    pad = max((len(i.label) for i in menu.items if not i.separator), default=0)
    for idx, item in enumerate(menu.items):
        if item.separator:
            lines.append("  " + ui.rule(pad + 20))
            continue
        hint = item.hint or (ui.sym("sub", colored=False) if item.submenu else "")
        body = f"{item.label.ljust(pad)}  {ui.dim(hint)}".rstrip()
        if idx == cursor:
            lines.append(f"{ui.sym('arrow')} {ui.bold(item.label.ljust(pad))}  {ui.dim(hint)}".rstrip())
        else:
            lines.append(f"  {body}")
    lines += ["", ui.dim("  up/down mover  ·  Enter elegir  ·  q volver")]
    # Aplanado: el cartel de bienvenida es un solo string con saltos adentro, y el repintado
    # cuenta renglones. Un item de lista != una fila de pantalla.
    return [physical for line in lines for physical in line.split("\n")]


def _select(menu: Menu, header: list[str]) -> Item | None:
    """Dibuja y devuelve el item elegido, o None si el usuario se volvio."""
    choosable = _choosable(menu.items)
    if not choosable:
        return None
    if not ui.can_redraw():
        # Sin control de cursor los escapes se imprimirian crudos: mejor la lista numerada.
        return _select_plain(menu, header)
    pos = 0
    drawn = 0
    while True:
        block = _render(menu, choosable[pos], header)
        if drawn:
            sys.stdout.write(f"\x1b[{drawn}A")  # volver arriba y repintar en el lugar
        for line in block:
            sys.stdout.write("\x1b[2K" + line + "\n")
        sys.stdout.write("\x1b[0J")  # borrar lo que haya quedado abajo de un bloque mas largo
        sys.stdout.flush()
        # Filas de pantalla, no items de la lista: un renglon mas largo que la terminal
        # envuelve y ocupa dos. Subir de menos deja el bloque anterior pegado arriba, y
        # asi es como el cartel de bienvenida se multiplicaba con cada flecha.
        drawn = ui.rows(block)
        try:
            key = _read_key()
        except KeyboardInterrupt:
            return None
        if key in ("up", "k"):
            pos = (pos - 1) % len(choosable)
        elif key in ("down", "j"):
            pos = (pos + 1) % len(choosable)
        elif key == "enter":
            return menu.items[choosable[pos]]
        elif key in ("q", "quit", "back"):
            return None
        elif key.isdigit() and key != "0":
            n = int(key) - 1
            if n < len(choosable):
                return menu.items[choosable[n]]


def _select_plain(menu: Menu, header: list[str]) -> Item | None:
    """Fallback numerado, para cuando no se puede leer tecla a tecla."""
    for line in header:
        print(line)
    if menu.title:
        print(ui.section(menu.title), end="\n\n")
    choosable = _choosable(menu.items)
    for n, idx in enumerate(choosable, 1):
        item = menu.items[idx]
        print(f"  {n}) {item.label}   {ui.dim(item.hint)}".rstrip())
    try:
        raw = input("\n  > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw or raw in ("q", "Q"):
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(choosable):
        return menu.items[choosable[int(raw) - 1]]
    return None


# ---------------------------------------------------------------------- contenido


def _menus(info: dict) -> dict[str, Menu]:
    port = info.get("port") or 3000
    url = f"http://localhost:{port}"
    return {
        "root": Menu(
            "",
            [
                Item("Ver el proyecto", "ldt scan", ["scan"]),
                Item("Server de desarrollo", submenu="dev"),
                Item("Navegador", submenu="browser"),
                Item("Base de datos", submenu="db"),
                Item("HTTP / API", submenu="http"),
                Item("Entorno", submenu="env"),
                Item("Puertos", submenu="ports"),
                Item("", separator=True),
                Item("Todos los comandos", "la lista completa", ["help"]),
                Item("Estado de lo que esta vivo", "ldt status", ["status"]),
                Item("Diagnostico de la instalacion", "ldt doctor", ["doctor"]),
                Item("Cerrar lo que abrio ldt", "ldt cleanup", ["cleanup"]),
                Item("Salir", action="quit"),
            ],
        ),
        "dev": Menu(
            "Server de desarrollo",
            [
                Item("Arrancar", "ldt dev start", ["dev", "start"]),
                Item("Ver los logs", "ultimas 60 lineas", ["dev", "logs"]),
                Item("Solo los errores", "ldt dev logs --errors", ["dev", "logs", "--errors"]),
                Item("Seguir el log en vivo", "Ctrl+C para cortar", ["dev", "logs", "--follow"]),
                Item("Que hay levantado", "ldt dev list", ["dev", "list"]),
                Item("Detener", "ldt dev stop", ["dev", "stop"]),
                Item("Volver", action="back"),
            ],
        ),
        "browser": Menu(
            "Navegador",
            [
                Item("Revisar una URL", "carga, errores y screenshot", ["browser", "check"], (("URL", url),)),
                Item("Loguearse a mano", "abre el Chromium visible", ["browser", "login"], (("URL", url),)),
                Item("Elementos clickeables", "refs @N", ["browser", "elements"]),
                Item("Click", "@N o selector", ["browser", "click"], (("target", "@1"),)),
                Item("Que fallo en la pagina", "ldt browser errors", ["browser", "errors"]),
                Item("Screenshot", "pagina entera", ["browser", "shot", "--full"]),
                Item("Estado de la sesion", "ldt browser status", ["browser", "status"]),
                Item("Sesiones abiertas", "incluye huerfanos", ["browser", "sessions"]),
                Item("Cerrar el navegador", "se pierde la sesion", ["browser", "close"]),
                Item("Volver", action="back"),
            ],
        ),
        "db": Menu(
            "Base de datos",
            [
                Item("Probar la conexion", "y a que base va: dev o produccion", ["db", "ping"]),
                Item("Tablas", "tamano y filas", ["db", "tables"]),
                Item("Esquema de una tabla", "columnas e indices", ["db", "schema"], (("tabla", ""),)),
                Item("Consulta", "solo lectura, en dev si hay", ["db", "q"], (("SQL", "select 1"),)),
                Item("Consulta en produccion", "solo lectura, --prod", ["db", "--prod", "q"], (("SQL", "select 1"),)),
                Item("Volver", action="back"),
            ],
        ),
        "http": Menu(
            "HTTP / API",
            [
                Item("GET", "ruta relativa o URL", ["http", "get"], (("ruta", "/"),)),
                Item("Esperar a que levante", "ldt http wait", ["http", "wait"], (("ruta", "/"),)),
                Item("Volver", action="back"),
            ],
        ),
        "env": Menu(
            "Entorno",
            [
                Item("Que variables faltan", "ldt env check", ["env", "check"]),
                Item("Nombres definidos", "valores enmascarados", ["env", "keys"]),
                Item("Volver", action="back"),
            ],
        ),
        "ports": Menu(
            "Puertos",
            [
                Item("Que esta escuchando", "ldt ports", ["ports"]),
                Item("Liberar un puerto", "mata al que lo tenga", ["ports"], (("puerto", str(port)),)),
                Item("Volver", action="back"),
            ],
        ),
    }


def _header(cwd: str) -> list[str]:
    from ldt import __main__ as main_mod

    root = core.find_root(Path(cwd))
    info = core.detect(root)
    git = core.git_info(root)
    sep = f" {ui.sym('sep', colored=False)} "
    bits = [root.name]
    if info.get("framework"):
        bits.append(info["framework"])
    if info.get("port"):
        bits.append(f":{info['port']}")
    if git.get("branch"):
        branch = git["branch"] + ("*" if git.get("dirty_files") else "")
        bits.append(branch)
    lines = [
        ui.box("ldt", f"herramientas de desarrollo · {main_mod.VERSION}"),
        "  " + ui.dim(sep.join(bits)),
    ]
    live = _live_line()
    if live:
        lines.append("  " + live)
    lines.append("")
    return lines


def _live_line() -> str:
    """Una linea con lo que ldt tiene corriendo ahora. Barata: no le habla al daemon."""
    from ldt.browser import client
    from ldt.cmds import dev

    bits = []
    for f in core.PROCS.glob("*.json"):
        meta = core.read_json(f)
        if core.running(meta.get("pid")):
            bits.append(f"{ui.sym('ok')} dev {meta.get('name')} :{meta.get('port') or '?'}")
    for s in client.sessions():
        if core.running(s.get("pid")):
            mode = "headed" if not (s.get("opts") or {}).get("headless", True) else "headless"
            bits.append(f"{ui.sym('ok')} browser {s.get('name')} ({mode})")
    huerfanos = client.orphans()
    if huerfanos:
        bits.append(ui.warn(f"{len(huerfanos)} browser(s) huerfano(s)"))
    return f" {ui.sym('sep', colored=False)} ".join(bits)


# ------------------------------------------------------------------------- correr


def _ask(prompts: tuple[tuple[str, str], ...]) -> list[str] | None:
    extra = []
    for text, default in prompts:
        shown = f"  {text}" + (f" [{default}]" if default else "") + ": "
        try:
            value = input(shown).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        value = value or default
        if not value:
            return None
        extra.append(value)
    return extra


def open_root(cwd: str) -> None:
    from ldt import __main__ as main_mod

    info = core.detect(core.find_root(Path(cwd)))
    menus = _menus(info)
    stack = ["root"]
    while stack:
        header = _header(cwd)
        menu = menus[stack[-1]]
        try:
            chosen = _select(menu, header)
        except Exception:
            # Terminal sin raw mode (algunos emuladores, algunos CI): modo numerado.
            chosen = _select_plain(menu, header)
        if chosen is None or chosen.action == "quit":
            return
        if chosen.action == "back":
            stack.pop()
            continue
        if chosen.submenu:
            stack.append(chosen.submenu)
            continue
        argv = list(chosen.argv or [])
        if chosen.prompts:
            extra = _ask(chosen.prompts)
            if extra is None:
                continue
            argv += extra
        print()
        main_mod.dispatch(["--cwd", cwd, *argv])
        try:
            input(ui.dim("\n  [Enter] para volver al menu "))
        except (EOFError, KeyboardInterrupt):
            return


def register(sub):
    p = sub.add_parser("menu", help="abrir el menu interactivo")
    p.set_defaults(func=lambda a: open_root(a.cwd))
