"""`ldt scan` y `ldt doctor` — orientarse en un repo, y verificar que ldt puede trabajar.

`scan` es lo primero que conviene correr al entrar a un proyecto desconocido: en una
salida dice que stack es, como se levanta, en que puerto, que variables necesita y si ya
hay algo corriendo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ldt import core, ui
from ldt.browser import client
from ldt.cmds import dev
from ldt.cmds.ports import listeners


# Carpetas que nunca son un proyecto propio aunque tengan un package.json adentro.
SKIP = {"node_modules", "vendor", "venv", "__pycache__", "dist", "build", "target"}


def is_project(path: Path) -> bool:
    if any((path / m).exists() for m in core.MARKERS):
        return True
    return any((path / f).exists() for f in (*core.COMPOSE_FILES, "index.php", "index.html"))


def projects_under(base: Path, depth: int) -> list[Path]:
    """Los proyectos de una carpeta de trabajo.

    Una carpeta que no es proyecto se abre un nivel mas (hasta `depth`): es el caso de
    las que agrupan varios repos de un mismo producto (`producto/api`, `producto/web`).
    Un proyecto no se abre: sus subcarpetas son parte de el.
    """
    found: list[Path] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return found
    for child in children:
        if not child.is_dir() or child.name.startswith((".", "_")) or child.name in SKIP:
            continue
        if is_project(child):
            found.append(child)
        elif depth > 1:
            found += projects_under(child, depth - 1)
    return found


def cmd_scan_all(a):
    """Todos los proyectos de una carpeta en una sola llamada.

    Es lo que usa una UI para armar la grilla: lanzar `ldt scan` una vez por proyecto
    cuesta un arranque de Python cada uno. `listeners()` y los registros de `dev` se leen
    una sola vez; git corre en paralelo porque son varios procesos por repo.
    """
    base = Path(a.path or a.cwd).resolve()
    if not base.is_dir():
        core.die(f"no existe la carpeta {base}")
    roots = projects_under(base, a.depth)
    ports = listeners()
    procs = dev.all_live()

    git = {}
    if not a.no_git:
        with ThreadPoolExecutor(max_workers=8) as pool:
            git = dict(zip(roots, pool.map(core.git_info, roots)))

    projects = []
    for root in roots:
        info = core.detect(root)
        info.pop("scripts", None)
        mine = [p for p in procs if core.belongs(p.get("cwd"), root)]
        taken = next((r for r in ports if r["port"] == info["port"]), None) if info["port"] else None
        if taken:
            ours = any(taken["pid"] in (p["pid"], p["port_pid"]) for p in procs)
            taken = {**taken, "ldt": ours}
        g = git.get(root) or {}
        projects.append(
            {
                "name": root.name,
                "slug": core.project_slug(root),
                "rel": root.relative_to(base).as_posix(),
                **info,
                "port_owner": taken,
                "dev": mine,
                "git": {"branch": g.get("branch"), "dirty_files": g.get("dirty_files")} if g else None,
            }
        )

    rows = []
    for p in projects:
        state = ""
        if any(d["ready"] for d in p["dev"]):
            state = "corriendo"
        elif any(d["alive"] for d in p["dev"]):
            state = "arrancando"
        elif p["port_owner"]:
            state = f"puerto tomado ({p['port_owner']['process']})"
        rows.append(
            {
                "proyecto": p["rel"],
                "stack": p["framework"] or p["kind"],
                "puerto": p["port"] or "",
                "dev": p["dev_cmd"] or "-",
                "estado": state,
            }
        )
    text = core.table(rows, max_width=48) + f"\n\n{len(projects)} proyectos en {base}"
    core.out({"base": str(base), "projects": projects}, a.json, text)


def cmd_scan(a):
    if a.all:
        return cmd_scan_all(a)
    root = core.find_root(Path(a.cwd))
    info = core.detect(root)
    git = core.git_info(root)
    live = [r for r in listeners() if r["port"] == info["port"]] if info["port"] else []
    interesting = {k: v for k, v in info["scripts"].items() if k in ("dev", "build", "start", "test", "lint", "typecheck", "db:push", "db:studio")}

    result = {**info, "git": git, "port_in_use": bool(live), "listeners": live, "key_scripts": interesting}
    lines = [
        f"{root}",
        f"stack:      {info['kind']} / {info['framework'] or '-'}  ({info['package_manager']})",
        f"dev:        {info['dev_cmd'] or '(no detectado)'}",
        f"puerto:     {info['port'] or '-'}" + ("  [OCUPADO: " + str(live[0]["process"]) + " pid " + str(live[0]["pid"]) + "]" if live else ""),
        f"entorno:    {', '.join(info['env_files']) or '(sin archivos de entorno)'}",
        f"docker:     {'si' if info['has_docker'] else 'no'}",
    ]
    if git:
        lines.append(f"git:        {git.get('branch')} ({git.get('dirty_files')} archivos sucios) - {git.get('last_commit') or ''}")
    if interesting:
        lines.append("scripts:    " + ", ".join(f"{k}" for k in interesting))
    core.out(result, a.json, "\n".join(lines))


def cmd_doctor(a):
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    check("python", sys.version_info >= (3, 10), sys.version.split()[0])

    try:
        import playwright

        from playwright.sync_api import sync_playwright

        check("playwright", True, playwright.__version__ if hasattr(playwright, "__version__") else "instalado")
        try:
            with sync_playwright() as pw:
                path = pw.chromium.executable_path
                check("chromium", Path(path).exists(), path)
        except Exception as exc:
            check("chromium", False, f"{exc}")
    except ImportError:
        check("playwright", False, "pip install playwright && python -m playwright install chromium")

    try:
        import psycopg2  # noqa: F401

        check("psycopg2", True, "")
    except ImportError:
        check("psycopg2", False, "pip install psycopg2-binary  (necesario para `ldt db`)")

    for tool in ("node", "pnpm", "git", "docker"):
        path = shutil.which(tool)
        check(tool, bool(path), path or "no esta en el PATH")

    # --- la instalacion: que `ldt` se pueda invocar y que los agentes lo conozcan
    repo = Path(__file__).resolve().parents[2]
    check("PATH", bool(shutil.which("ldt")), shutil.which("ldt") or f"agregar {repo / 'bin'} (install.ps1)")

    # La regla global es lo que hace que un agente sepa que `ldt` existe sin que haya que
    # decirselo en cada sesion. Se instala con install.ps1, o a mano copiando rules/ldt.md.
    agent_files = [
        Path.home() / ".claude" / "CLAUDE.md",
        Path.home() / ".codex" / "AGENTS.md",
        Path.home() / "AGENTS.md",
    ]
    installed = next(
        (
            f
            for f in agent_files
            if f.exists() and "ldt:start" in f.read_text(encoding="utf-8", errors="replace")
        ),
        None,
    )
    check("regla para agentes", bool(installed), str(installed) if installed else "ver rules/ldt.md")

    # --- basura: lo unico que se acumula solo
    orphans = client.orphans()
    check(
        "browsers huerfanos",
        not orphans,
        "ninguno" if not orphans else f"{len(orphans)} vivo(s) sin sesion: pid {', '.join(str(o['pid']) for o in orphans)}",
    )

    dead = []
    for f in core.PROCS.glob("*.json"):
        meta = core.read_json(f)
        if not core.running(meta.get("pid")):
            dead.append(meta.get("name") or f.stem)
    for f in core.SESSIONS.glob("*.json"):
        info = core.read_json(f)
        if not core.running(info.get("pid")):
            dead.append(f"browser {f.stem}")
    check("registros vencidos", not dead, "ninguno" if not dead else ", ".join(dead))

    size = sum(f.stat().st_size for f in core.HOME.rglob("*") if f.is_file())
    check("estado en disco", size < 512 * 1024 * 1024, f"{size / 1024 / 1024:.0f} MB en {core.HOME}")

    if a.fix:
        killed = client.reap()
        for name in list(dead):
            (core.PROCS / f"{name}.json").unlink(missing_ok=True)
            (core.SESSIONS / f"{name.removeprefix('browser ')}.json").unlink(missing_ok=True)
        print(f"limpiados: {len(killed)} huerfano(s), {len(dead)} registro(s) vencido(s)")
        a.fix = False
        return cmd_doctor(a)

    if a.install:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "playwright", "psycopg2-binary"], check=False)
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        a.install = False
        return cmd_doctor(a)

    # Los primeros cuatro son los que bloquean: sin ellos `ldt` no puede trabajar. El
    # resto (herramientas externas, instalacion, basura) se reporta pero no es fatal.
    essential = checks[:4]
    rows = [
        {"": ui.sym("ok") if c["ok"] else ui.warn("FALTA"), "check": c["check"], "detalle": c["detail"]}
        for c in checks
    ]
    text = core.table(rows, max_width=80)
    pending = [c["check"] for c in checks if not c["ok"]]
    if pending:
        text += "\n\n" + ui.dim("pendiente: " + ", ".join(pending))
        if any(c["check"] in ("browsers huerfanos", "registros vencidos") for c in checks if not c["ok"]):
            text += "\n" + ui.dim("limpiar lo que quedo colgado: ldt doctor --fix")
    core.out({"checks": checks, "ok": all(c["ok"] for c in essential)}, a.json, text)


def register(sub):
    p = sub.add_parser("scan", help="resumen del proyecto: stack, como levantarlo, puerto, entorno, git")
    p.add_argument("--all", action="store_true", help="todos los proyectos de una carpeta de trabajo")
    p.add_argument("path", nargs="?", help="con --all, la carpeta a recorrer (default: la actual)")
    p.add_argument("--depth", type=int, default=2, help="con --all, niveles de carpetas a abrir")
    p.add_argument("--no-git", action="store_true", help="con --all, sin estado de git (mas rapido)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("doctor", help="verificar que ldt tiene todo lo que necesita")
    p.add_argument("--install", action="store_true", help="instalar lo que falte (playwright + chromium + psycopg2)")
    p.add_argument("--fix", action="store_true", help="cerrar browsers huerfanos y limpiar registros vencidos")
    p.set_defaults(func=cmd_doctor)
