"""`ldt test` — correr la suite del proyecto sin tener que averiguar cual es.

Cada repo usa lo suyo (vitest, jest, pytest) y el comando exacto vive en un
`package.json` distinto cada vez. Esto lo deduce igual que `ldt dev` deduce el server, y
resume la salida: el detalle completo esta en el archivo de log, no en la terminal.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ldt import core, ui

# Lineas que resumen el resultado en vitest, jest y pytest. Lo demas suele ser ruido.
SUMMARY = re.compile(
    r"(tests?|test files|suites?)\s+\d|passed|failed|\d+\s+(passed|failed|skipped|error)|"
    r"=+\s*\d+\s+(passed|failed)|no tests? (ran|found)",
    re.IGNORECASE,
)


def detect_cmd(root: Path, info: dict) -> str | None:
    scripts = info.get("scripts") or {}
    if "test" in scripts:
        pm = info.get("package_manager") or "pnpm"
        return f"{pm} test"
    pyproject = root / "pyproject.toml"
    markers = [root / "pytest.ini", root / "tests", root / "test"]
    has_pytest = any(m.exists() for m in markers)
    if pyproject.exists() and "pytest" in pyproject.read_text(encoding="utf-8", errors="replace").lower():
        has_pytest = True
    if has_pytest:
        return "python -m pytest -q"
    return None


def cmd_test(a):
    root = core.find_root(Path(a.cwd))
    info = core.detect(root)
    cmd = a.cmd or detect_cmd(root, info)
    if not cmd:
        core.die(f'no encontre suite de tests en {root}. Pasala con --cmd "pnpm test"')
    if a.target:
        cmd = f"{cmd} {a.target}"

    log = core.LOGS / f"{core.project_slug(root)}-test.log"
    proc = subprocess.run(
        cmd,
        cwd=str(root),
        shell=True,  # hace falta para resolver pnpm.cmd / npm.cmd en Windows
        capture_output=True,
        text=True,
        errors="replace",
        env={**os.environ, "FORCE_COLOR": "0", "NO_COLOR": "1", "CI": "1"},
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    log.write_text(output, encoding="utf-8")

    lines = output.splitlines()
    summary = [l.strip() for l in lines if SUMMARY.search(l)][-8:]
    tail = lines[-a.tail :]
    mark = ui.sym("ok") if proc.returncode == 0 else ui.sym("err")
    verdict = "tests ok" if proc.returncode == 0 else f"tests fallaron (exit {proc.returncode})"

    text_lines = [f"{mark} {verdict}", ui.dim(f"$ {cmd}"), ui.dim(f"log completo: {log}"), ""]
    text_lines += summary or tail
    if proc.returncode and summary:
        text_lines += ["", ui.dim("ultimas lineas:"), *tail]

    core.out(
        {"cmd": cmd, "exit_code": proc.returncode, "log": str(log), "summary": summary, "tail": tail},
        a.json,
        "\n".join(text_lines),
    )
    if proc.returncode:
        raise SystemExit(proc.returncode)


def register(sub):
    p = sub.add_parser("test", help="correr la suite de tests del proyecto")
    p.add_argument("target", nargs="?", help="archivo o patron a pasarle al runner")
    p.add_argument("--cmd", help='comando a correr (default: el detectado, p.ej. "pnpm test")')
    p.add_argument("-n", "--tail", type=int, default=25, help="lineas de salida a mostrar")
    p.set_defaults(func=cmd_test)
