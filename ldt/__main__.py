"""Punto de entrada de `ldt`.

Uso:  ldt                       menu interactivo (o cartel corto si no hay terminal)
      ldt help                   la lista completa de comandos
      ldt <grupo> <comando> [opciones]
      python /ruta/a/ldt/ldt.py <grupo> <comando>

Todos los comandos trabajan sobre el directorio actual salvo que se pase --cwd.
"""

from __future__ import annotations

import argparse
import os
import sys

from ldt import core, menu, ui
from ldt.browser import cli as browser_cli
from ldt.cmds import cleanup, db, dev, envck, http, ports, scan, status, test

VERSION = "1.1.0"

EXAMPLES = [
    ("ldt scan", "que es este proyecto y como se levanta"),
    ("ldt dev start", "levantar el server de dev en background"),
    ("ldt browser check http://localhost:3000", "cargar la app y reportar errores + screenshot"),
    ("ldt browser elements", "elementos clickeables con refs @N"),
    ("ldt browser click @3", "click en el elemento @3"),
    ("ldt browser errors", "errores de consola, JS y requests fallidos"),
    ('ldt db q "select count(*) from users"', "consulta de solo lectura"),
    ("ldt ports 3000 --kill", "liberar un puerto colgado"),
    ("ldt env check", "variables que faltan (sin mostrar valores)"),
    ("ldt cleanup", "cerrar el dev server y el browser al terminar"),
]

EPILOG = "ejemplos:\n" + "\n".join(f"  {cmd:<42} {desc}" for cmd, desc in EXAMPLES) + "\n"


def _groups(parser: argparse.ArgumentParser) -> list[dict]:
    """Los grupos registrados, leidos del propio parser para que no se desincronicen."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return [{"name": c.dest, "help": c.help or ""} for c in action._choices_actions]
    return []


SUBTITLE = f"herramientas de desarrollo para agentes y humanos \u00b7 v{VERSION}"


def banner_short() -> str:
    """Lo que se ve cuando `ldt` corre sin terminal (o sin menu).

    Adrede no lista los comandos: el volcado completo esta a un `ldt help` de distancia y
    apenas se ejecuta el CLI no le sirve a nadie.
    """
    sep = " - " if ui.ascii_only() else " \u00b7 "
    return "\n".join(
        [
            ui.box("ldt", SUBTITLE),
            "",
            f"  {ui.bold('ldt menu')}              menu interactivo",
            f"  {ui.bold('ldt help')}              todos los comandos y ejemplos",
            f"  {ui.bold('ldt scan')}              que es este proyecto y como se levanta",
            f"  {ui.bold('ldt status')}            que dejo ldt corriendo",
            "",
            ui.dim(f"  ldt <grupo> --help{sep}detalle de cada grupo"),
        ]
    )


def banner_full(parser: argparse.ArgumentParser) -> str:
    lines = [
        ui.box("ldt", SUBTITLE),
        "",
        "uso:  ldt <grupo> <comando> [opciones]",
        "",
        ui.section("comandos:"),
    ]
    groups = _groups(parser)
    pad = max([len(g["name"]) for g in groups] or [0])
    lines += [f"  {ui.bold(g['name'].ljust(pad))}  {ui.dim(g['help'])}" for g in groups]
    lines += [
        "",
        ui.section("ejemplos:"),
        *[f"  {c:<42} {ui.dim(d)}" for c, d in EXAMPLES],
        "",
        ui.section("opciones globales:"),
        f"  --json            {ui.dim('salida estructurada en vez de texto')}",
        f"  --cwd <ruta>      {ui.dim('trabajar sobre otro directorio (default: el actual)')}",
        f"  -V, --version     {ui.dim('version de ldt')}",
        "",
    ]
    sep = " - " if ui.ascii_only() else " \u00b7 "
    lines.append(ui.dim(f"mas ayuda:  ldt <grupo> --help{sep}ldt menu{sep}ldt doctor"))
    return "\n".join(lines)


def cmd_help(a) -> None:
    parser = build_parser()
    payload = {
        "cli": "ldt",
        "version": VERSION,
        "usage": "ldt <grupo> <comando> [opciones]",
        "groups": _groups(parser),
        "examples": [{"cmd": c, "desc": d} for c, d in EXAMPLES],
    }
    core.out(payload, getattr(a, "json", False), banner_full(parser))


def cmd_welcome(a) -> None:
    """`ldt` pelado: menu si hay con quien hablar, cartel corto si no."""
    if getattr(a, "json", False):
        cmd_help(a)
        return
    if menu.interactive():
        menu.open_root(a.cwd)
        return
    print(banner_short())


def add_json(parser: argparse.ArgumentParser) -> None:
    """Pone `--json` en todos los subcomandos, no solo en la raiz.

    `ldt --json ports 3000` andaba, pero `ldt ports 3000 --json` rebotaba, y esa es la
    forma que sale natural de escribir (y la que escribe un agente, que es el consumidor
    principal de la salida estructurada).

    Se inyecta recorriendo el arbol de parsers en vez de repetirlo en cada `add_parser`:
    asi un comando nuevo no se lo puede olvidar. `default=SUPPRESS` es la parte que
    importa: sin eso el subparser dejaria `json=False` y pisaria el `--json` que venia de
    la raiz, rompiendo la forma que hoy si funciona.
    """
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for child in action.choices.values():
            # Los alias apuntan al mismo parser, y `--json` dos veces es un error.
            if not any("--json" in a.option_strings for a in child._actions):
                child.add_argument(
                    "--json", action="store_true", default=argparse.SUPPRESS,
                    help="salida estructurada en vez de texto",
                )
            add_json(child)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ldt",
        description="Herramientas de desarrollo compartidas entre repos",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--json", action="store_true", help="salida JSON en vez de texto")
    parser.add_argument("--cwd", default=os.getcwd(), help="directorio del proyecto (default: el actual)")
    parser.add_argument("-V", "--version", action="version", version=f"ldt {VERSION}")
    # Sin grupo no es un error: `ldt` solo muestra el cartel de presentacion.
    sub = parser.add_subparsers(dest="group")
    parser.set_defaults(func=cmd_welcome)

    for module in (browser_cli, dev, ports, db, http, envck, scan, test, status, cleanup, menu):
        module.register(sub)

    p = sub.add_parser("help", help="la lista completa de comandos, con ejemplos")
    p.set_defaults(func=cmd_help)

    add_json(parser)
    return parser


def dispatch(argv: list[str] | None = None) -> int:
    """Parsea y corre. Lo usa `main` y tambien el menu, para que no haya dos caminos."""
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        return 130
    except SystemExit as exc:  # core.die
        return int(exc.code or 0)
    except Exception as exc:
        # Los errores esperables (sesion caida, SQL invalido, host inalcanzable) se
        # reportan en una linea: el traceback no le agrega nada a quien lee la salida.
        if os.environ.get("LDT_DEBUG"):
            raise
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    # La consola de Windows no es UTF-8 por defecto y la salida lleva acentos y rutas
    # que vienen de la web; sin esto se imprimen mojibake o revientan.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    core.ensure_dirs()
    return dispatch(argv)


if __name__ == "__main__":
    sys.exit(main())
