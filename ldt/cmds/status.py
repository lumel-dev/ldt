"""`ldt status` — que dejo ldt corriendo, de un vistazo.

Existe porque todo lo que levanta `ldt` es detached: sobrevive al comando, al turno del
agente y a la sesion entera. Sin un lugar donde mirar, la unica forma de saber que quedo
vivo es el administrador de tareas.

No le habla al daemon del browser a proposito: un `status` que cuente como uso le
reiniciaria el contador de inactividad, y entonces mirar el estado alargaria la vida de lo
que estamos mirando.
"""

from __future__ import annotations

import time
from pathlib import Path

from ldt import core, ui
from ldt.browser import client
from ldt.cmds import dev
from ldt.cmds.ports import listeners


def _uptime(started: float | None) -> str:
    if not started:
        return "?"
    secs = int(time.time() - started)
    if secs < 90:
        return f"{secs}s"
    if secs < 5400:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}"


def _procs(root: Path, every: bool) -> list[dict]:
    rows = []
    for f in sorted(core.PROCS.glob("*.json")):
        meta = core.read_json(f)
        if not every and not core.belongs(meta.get("cwd"), root):
            continue
        rows.append(
            {
                "name": meta.get("name") or f.stem,
                "pid": meta.get("pid"),
                "port": meta.get("port"),
                "alive": core.running(meta.get("pid")),
                "uptime": _uptime(meta.get("started_at")),
                "cmd": meta.get("cmd"),
                "log": str(dev.log_file(meta.get("name") or f.stem)),
                "cwd": meta.get("cwd"),
            }
        )
    return rows


def _browsers(root: Path, every: bool) -> list[dict]:
    rows = []
    for s in client.sessions():
        opts = s.get("opts") or {}
        if not every and not core.belongs(opts.get("project"), root):
            continue
        rows.append(
            {
                "name": s.get("name"),
                "pid": s.get("pid"),
                "alive": core.running(s.get("pid")),
                "modo": "headed" if not opts.get("headless", True) else "headless",
                "perfil": opts.get("profile") or "efimero",
                "uptime": _uptime(s.get("started_at")),
                "project": opts.get("project"),
            }
        )
    return rows


def cmd_status(a):
    root = core.find_root(Path(a.cwd))
    info = core.detect(root)
    git = core.git_info(root)
    procs = _procs(root, a.all)
    browsers = _browsers(root, a.all)
    orphans = client.orphans()
    ours = {p["port"] for p in procs if p.get("port")}
    ports = [r for r in listeners() if r["port"] in ours or r["port"] == info.get("port")]

    payload = {
        "project": {**info, "git": git},
        "dev": procs,
        "browsers": browsers,
        "orphans": orphans,
        "ports": ports,
    }

    sep = " " + ui.sym("sep", colored=False) + " "
    bits = [root.name] + [b for b in (info.get("framework"), f":{info['port']}" if info.get("port") else None) if b]
    lines = [ui.kv("proyecto", sep.join(bits) + ui.dim(f"  {root}"))]
    if git.get("branch"):
        dirty = f"{git['dirty_files']} sin commitear" if git.get("dirty_files") else "limpio"
        lines.append(ui.kv("git", sep.join([git["branch"], dirty, git.get("last_commit") or ""])))

    if procs:
        for p in procs:
            mark = ui.sym("ok") if p["alive"] else ui.sym("err")
            state = "" if p["alive"] else ui.dim(" (muerto, registro viejo)")
            lines.append(
                ui.kv("dev", f"{mark} {p['name']}  pid {p['pid']}  :{p['port'] or '?'}  {p['uptime']}{state}")
            )
            lines.append(ui.kv("", ui.dim(f"  {p['log']}")))
    else:
        lines.append(ui.kv("dev", ui.dim("nada levantado por ldt")))

    if browsers:
        for b in browsers:
            mark = ui.sym("ok") if b["alive"] else ui.sym("err")
            lines.append(
                ui.kv("browser", f"{mark} {b['name']}  pid {b['pid']}  {b['modo']}{sep}{b['perfil']}  {b['uptime']}")
            )
    else:
        lines.append(ui.kv("browser", ui.dim("ninguno abierto")))

    if orphans:
        pids = ", ".join(str(o["pid"]) for o in orphans)
        lines.append(ui.kv("huerfanos", ui.warn(f"{len(orphans)} browser(s) sin sesion (pid {pids})")))
        lines.append(ui.kv("", ui.dim("  los cierra: ldt cleanup")))

    if ports:
        shown = []
        for r in ports:
            who = "de ldt" if r["port"] in ours else "no es de ldt"
            shown.append(f":{r['port']} {r['process']} ({who})")
        lines.append(ui.kv("puertos", sep.join(shown)))

    core.out(payload, a.json, "\n".join(lines))


def register(sub):
    p = sub.add_parser("status", help="que dejo ldt corriendo: dev servers, browsers, puertos")
    p.add_argument("--all", action="store_true", help="incluir lo de otros proyectos")
    p.set_defaults(func=cmd_status)
