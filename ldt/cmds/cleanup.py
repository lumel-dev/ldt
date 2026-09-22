"""`ldt cleanup` — cerrar de una vez lo que quedo vivo: dev servers y navegadores.

Todo lo que `ldt` levanta es detached a proposito (sobrevive al comando y al turno del
agente), asi que nada lo apaga cuando la tarea termina: queda un `pnpm dev` tomando el
puerto y un Chromium comiendo RAM hasta que alguien se da cuenta. Esto es el "cerrar todo"
de un solo paso, y va **al final de la sesion**, no entre pasos: el browser de desarrollo
es efimero, cerrarlo borra cookies, login y estado de la pagina.

Por defecto toca solo lo que se levanto para *este* proyecto: pueden convivir varias
sesiones de agente en repos distintos y ninguna tiene por que apagarle el server a la otra.
Y solo lo que levanto `ldt`: un dev server que abrio el usuario a mano no se toca nunca,
ni con `--all`.
"""

from __future__ import annotations

from pathlib import Path

from ldt import core
from ldt.browser import client
from ldt.cmds import dev


def cmd_cleanup(a):
    dev.PROCS.mkdir(parents=True, exist_ok=True)
    root = core.find_root(Path(a.cwd))
    want_dev = a.dev or not a.browser
    want_browser = a.browser or not a.dev
    closed: list[str] = []
    stale: list[str] = []
    kept: list[str] = []

    if want_dev:
        for f in sorted(dev.PROCS.glob("*.json")):
            meta = core.read_json(f)
            name = meta.get("name") or f.stem
            if not (a.all or core.belongs(meta.get("cwd"), root)):
                kept.append(f"dev {name}")
                continue
            was_alive = dev.running(meta.get("pid", -1))
            dev.stop_proc(name)
            (closed if was_alive else stale).append(f"dev {name}")

    if want_browser:
        for s in client.sessions():
            name = s.get("name") or "default"
            if not (a.all or core.belongs((s.get("opts") or {}).get("project"), root)):
                kept.append(f"browser {name}")
                continue
            (closed if client.stop(name) else stale).append(f"browser {name}")

    # Los huerfanos se cierran siempre: son Chromiums vivos que perdieron su archivo de
    # sesion, o sea que nadie puede usarlos ni sabe de que proyecto eran. No hay caso en
    # que alguien los quiera.
    reaped = [f"browser huerfano (pid {o['pid']})" for o in client.reap()]
    closed += reaped

    text = f"cerrados: {', '.join(closed)}" if closed else "no habia nada corriendo de este proyecto"
    if stale:
        text += f"\nya estaban muertos (limpiados): {', '.join(stale)}"
    if kept:
        text += f"\nde otros proyectos, sin tocar: {', '.join(kept)}  (`ldt cleanup --all` los cierra tambien)"
    core.out(
        {"closed": closed, "stale": stale, "kept": kept, "reaped": reaped, "root": str(root)},
        a.json,
        text,
    )


def register(sub):
    p = sub.add_parser("cleanup", help="cerrar los dev servers y navegadores que quedaron abiertos")
    p.add_argument("--all", action="store_true", help="cerrar tambien lo de otros proyectos")
    p.add_argument("--dev", action="store_true", help="solo los dev servers")
    p.add_argument("--browser", action="store_true", help="solo los navegadores")
    p.set_defaults(func=cmd_cleanup)
