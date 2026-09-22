"""`ldt env` — que variables define el proyecto y cuales le faltan, sin mostrar secretos.

Los valores nunca se imprimen enteros (hay que pedir --reveal explicitamente). Asi un
agente puede diagnosticar "falta STRIPE_KEY" sin que la clave termine en el transcript.
"""

from __future__ import annotations

import re
from pathlib import Path

from ldt import core

# Referencias a variables en el codigo: process.env.X / os.environ["X"] / os.getenv("X")
USAGE_RE = re.compile(
    r"process\.env\.([A-Z0-9_]+)|process\.env\[['\"]([A-Z0-9_]+)|os\.environ(?:\.get)?[\[(]['\"]([A-Z0-9_]+)"
    r"|os\.getenv\(['\"]([A-Z0-9_]+)"
)
# Las pone el runtime (Node, Next, Vercel, CI), no el proyecto: pedirlas seria ruido.
RUNTIME_KEYS = {
    "NODE_ENV", "PORT", "CI", "PATH", "HOME", "PWD", "TZ", "NEXT_RUNTIME", "NEXT_PUBLIC_VERCEL_URL",
    "VERCEL", "VERCEL_ENV", "VERCEL_URL", "VERCEL_REGION", "PYTHONPATH", "npm_lifecycle_event",
}
SKIP_DIRS = {"node_modules", ".next", ".git", "dist", "build", "out", "__pycache__", ".venv", "venv", ".turbo"}
CODE_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".py"}


def scan_usage(root: Path, limit_files: int = 4000) -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    count = 0
    for path in root.rglob("*"):
        if count >= limit_files:
            break
        if path.is_dir() or path.suffix not in CODE_EXT:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        count += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in USAGE_RE.finditer(text):
            key = next(g for g in match.groups() if g)
            used.setdefault(key, [])
            rel = str(path.relative_to(root))
            if rel not in used[key]:
                used[key].append(rel)
    return used


def cmd_check(a):
    root = core.find_root(Path(a.cwd))
    files = core.env_files(root)
    if not files:
        core.die(f"no hay archivos de entorno en {root}")
    actual: dict[str, str] = {}
    example: dict[str, str] = {}
    for f in files:
        (example if core.is_example(f) else actual).update(core.parse_env_file(f))

    used = scan_usage(root) if a.scan else {}
    expected = (set(example) | set(used)) - RUNTIME_KEYS
    missing = sorted(k for k in expected if k not in actual)
    empty = sorted(k for k, v in actual.items() if not v)
    unused = sorted(k for k in actual if used and k not in used and k not in example)

    result = {
        "files": [f.name for f in files],
        "defined": len(actual),
        "missing": [{"key": k, "used_in": used.get(k, [])[:3]} for k in missing],
        "empty": empty,
        "possibly_unused": unused,
    }
    lines = [f"archivos: {', '.join(result['files'])}", f"definidas: {len(actual)}"]
    if missing:
        lines.append("\nFALTAN:")
        lines += [f"  {k}" + (f"  (usada en {', '.join(used.get(k, [])[:2])})" if used.get(k) else "") for k in missing]
    if empty:
        lines.append("\nvacias: " + ", ".join(empty))
    if unused:
        lines.append("\nposiblemente sin uso: " + ", ".join(unused))
    if not missing and not empty:
        lines.append("\ntodo lo esperado esta definido")
    core.out(result, a.json, "\n".join(lines))
    if missing and a.strict:
        raise SystemExit(1)


def cmd_keys(a):
    root = core.find_root(Path(a.cwd))
    rows = []
    for f in core.env_files(root):
        for k, v in core.parse_env_file(f).items():
            rows.append({"key": k, "file": f.name, "value": v if a.reveal else core.mask(v)})
    core.out({"keys": rows}, a.json, core.table(sorted(rows, key=lambda r: r["key"])))


def register(sub):
    p = sub.add_parser("env", help="variables de entorno del proyecto (valores enmascarados)")
    es = p.add_subparsers(dest="env_cmd", required=True)

    sp = es.add_parser("check", help="comparar lo definido contra el ejemplo y el uso en el codigo")
    sp.add_argument("--scan", action="store_true", default=True, help="buscar usos en el codigo (default)")
    sp.add_argument("--no-scan", dest="scan", action="store_false")
    sp.add_argument("--strict", action="store_true", help="exit code 1 si falta alguna")
    sp.set_defaults(func=cmd_check)

    sp = es.add_parser("keys", help="listar las variables definidas")
    sp.add_argument("--reveal", action="store_true", help="mostrar los valores (usar con cuidado)")
    sp.set_defaults(func=cmd_keys)
