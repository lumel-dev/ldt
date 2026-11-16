"""`ldt docker check` — buildear la imagen del proyecto en local antes de mergear.

Es comun que la CI corra lint y tests directo en el runner y no construya la imagen: eso
lo hace recien la plataforma de deploy (Coolify, Render, Railway...) al desplegar. Un
cambio que rompe el Dockerfile, o una dependencia que necesita algo que la imagen slim no
trae, pasa la CI en verde y explota en el deploy. Ejemplo real: playwright 1.65 trae un
Node que necesita libatomic; el runner de GitHub la tiene y `python:*-slim` no.

Correr el build en la CI gasta minutos, asi que esto lo hace en la maquina local: buildea, si la imagen trae playwright levanta Chromium adentro, y borra
la imagen al terminar. Con `--pr N` buildea la rama de un PR en un worktree aparte, sin
tocar el arbol de trabajo del usuario (los de Dependabot o Renovate nunca pasan por la maquina).
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ldt import core, ui

WORKTREES = core.HOME / "worktrees"

# Si la imagen trae playwright para Python, que Chromium levante adentro es la prueba que
# importa: el build puede pasar y el browser no arrancar por una libreria del sistema.
PY_PLAYWRIGHT_SMOKE = (
    "from playwright.sync_api import sync_playwright\n"
    "with sync_playwright() as p:\n"
    "    b = p.chromium.launch()\n"
    "    print('chromium', b.version)\n"
    "    b.close()\n"
)

ERROR_LINE = re.compile(r"\berror\b|exit code|not found|cannot|failed", re.IGNORECASE)


def _run(cmd: list[str], cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, check=False,
    )


def docker_up() -> bool:
    try:
        return _run(["docker", "info"], timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def image_tag(root: Path, pr: int | None) -> str:
    # Los tags de Docker son [a-z0-9._-]; el nombre de la carpeta puede traer cualquier cosa.
    slug = re.sub(r"[^a-z0-9._-]+", "-", core.project_slug(root)).strip("-.") or "proyecto"
    return f"ldt-check/{slug}:{f'pr{pr}' if pr else 'local'}"


def checkout_pr(root: Path, pr: int, remote: str) -> tuple[Path, str]:
    """Baja la cabeza del PR a un worktree propio. Devuelve (ruta, sha)."""
    fetch = _run(["git", "fetch", "--quiet", remote, f"refs/pull/{pr}/head"], cwd=root, timeout=180)
    if fetch.returncode:
        core.die(f"no pude bajar el PR #{pr} de '{remote}': {fetch.stderr.strip()[:300]}")
    sha = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=root).stdout.strip()
    WORKTREES.mkdir(parents=True, exist_ok=True)
    path = WORKTREES / f"{core.project_slug(root)}-pr{pr}"
    remove_worktree(root, path)
    add = _run(["git", "worktree", "add", "--detach", "--force", str(path), sha], cwd=root, timeout=120)
    if add.returncode:
        core.die(f"no pude crear el worktree del PR #{pr}: {add.stderr.strip()[:300]}")
    return path, sha


def remove_worktree(root: Path, path: Path) -> None:
    if path.exists():
        _run(["git", "worktree", "remove", "--force", str(path)], cwd=root)
    _run(["git", "worktree", "prune"], cwd=root)


def placeholder(key: str) -> str:
    """Un valor que pasa la validacion de forma sin apuntar a nada real."""
    k = key.upper()
    if "DATABASE" in k or "POSTGRES" in k:
        return "postgresql://ldt:ldt@localhost:5432/ldt"
    if k.endswith("URL"):
        return "http://localhost"
    return "ldt-placeholder"


def parse_build_env(items: list[str]) -> dict[str, str]:
    """`KEY=VAL` tal cual; `KEY` solo, con un placeholder.

    No se leen del archivo de entorno del proyecto a proposito: ahi suele estar la URL de
    produccion, y un valor usado en un RUN queda en la cache de build de la maquina.
    """
    env: dict[str, str] = {}
    for item in items:
        key, sep, val = item.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            core.die(f"--build-env invalida: {item!r} (formato: KEY o KEY=valor)")
        env[key] = val if sep else placeholder(key)
    return env


def with_args(dockerfile: Path, env: dict[str, str], dest: Path) -> Path:
    """Copia del Dockerfile con un `ARG` por variable despues de cada FROM.

    Es lo que hacen las plataformas de deploy con las variables de build: sin el ARG, un
    `--build-arg` no llega a los RUN. La copia va fuera del repo (y del worktree).
    """
    out = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        out.append(line)
        if re.match(r"\s*FROM\s", line, re.IGNORECASE):
            out += [f"ARG {k}" for k in env]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    return dest


def uses_py_playwright(ctx: Path) -> bool:
    text = ""
    for name in ("pyproject.toml", "requirements.txt"):
        f = ctx / name
        if f.exists():
            text += f.read_text(encoding="utf-8", errors="replace").lower()
    return re.search(r"^\s*[\"']?playwright\b", text, re.MULTILINE) is not None


def cmd_check(a):
    root = core.find_root(Path(a.cwd))
    if not docker_up():
        core.die(
            "Docker no responde. Abri Docker Desktop y espera a que `docker info` conteste "
            '(Windows: Start-Process "C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe")'
        )

    ctx, sha, worktree, tmp_dockerfile = root, None, None, None
    if a.pr:
        worktree, sha = checkout_pr(root, a.pr, a.remote)
        ctx = worktree
    try:
        dockerfile = ctx / a.file
        if not dockerfile.exists():
            core.die(f"no hay {a.file} en {ctx}" + (f" (PR #{a.pr})" if a.pr else ""))

        tag = image_tag(root, a.pr)
        suffix = f"-pr{a.pr}" if a.pr else ""
        log = core.LOGS / f"{core.project_slug(root)}-docker{suffix}.log"
        build_env = parse_build_env(a.build_env or [])
        cmd = ["docker", "build", "--progress=plain", "-t", tag]
        if build_env:
            tmp_dockerfile = core.HOME / "tmp" / f"{core.project_slug(root)}{suffix}.Dockerfile"
            dockerfile = with_args(dockerfile, build_env, tmp_dockerfile)
            for k, v in build_env.items():
                cmd += ["--build-arg", f"{k}={v}"]
        t0 = time.monotonic()
        build = _run(cmd + ["-f", str(dockerfile), str(ctx)], timeout=a.timeout)
        secs = round(time.monotonic() - t0)
        output = (build.stdout or "") + (build.stderr or "")
        log.write_text(output, encoding="utf-8")

        smoke = None
        if build.returncode == 0:
            smoke = run_smoke(tag, ctx, a.smoke)

        ok = build.returncode == 0 and (smoke is None or smoke["ok"])
        if not a.keep:
            _run(["docker", "rmi", "-f", tag])
    finally:
        if worktree:
            remove_worktree(root, worktree)
        if tmp_dockerfile:
            tmp_dockerfile.unlink(missing_ok=True)

    lines = output.splitlines()
    errors = [l.strip() for l in lines if ERROR_LINE.search(l)][-8:] if build.returncode else []
    payload = {
        "ok": ok,
        "pr": a.pr,
        "sha": sha,
        "image": tag if a.keep else None,
        "build": {"exit_code": build.returncode, "seconds": secs, "errors": errors},
        "smoke": smoke,
        "build_env": list(build_env),  # solo las claves: un valor explicito puede ser secreto
        "log": str(log),
    }

    what = f"PR #{a.pr} ({sha[:7]})" if a.pr else "arbol local"
    text = [f"{ui.sym('ok' if ok else 'err')} docker check {'ok' if ok else 'fallo'} - {what}"]
    if build.returncode:
        text.append(f"build fallo (exit {build.returncode}, {secs}s)")
        text += [f"  {l}" for l in errors] or [f"  {l}" for l in lines[-15:]]
        if re.search(r"\b(falta|missing|undefined|required)\b.*\b[A-Z][A-Z0-9_]{3,}\b", output, re.IGNORECASE):
            text.append(ui.dim(
                "si falta una variable que el deploy le pasa al build, repetir con --build-env KEY "
                "(va un placeholder) o --build-env KEY=valor"
            ))
    else:
        text.append(f"build ok ({secs}s)")
        if smoke:
            mark = ui.sym("ok" if smoke["ok"] else "err")
            text.append(f"{mark} smoke: {smoke['label']}")
            text += [f"  {l}" for l in smoke["output"].splitlines()[-10:]]
        else:
            text.append(ui.dim("smoke: no hay (pasalo con --smoke \"<cmd>\")"))
    text.append(ui.dim(f"imagen: {tag}" if a.keep else "imagen borrada"))
    text.append(ui.dim(f"log completo: {log}"))
    core.out(payload, a.json, "\n".join(text))
    if not ok:
        raise SystemExit(1)


def run_smoke(tag: str, ctx: Path, custom: str | None) -> dict | None:
    if custom:
        cmd = ["docker", "run", "--rm", "--entrypoint", "sh", tag, "-c", custom]
        label = custom
    elif uses_py_playwright(ctx):
        cmd = ["docker", "run", "--rm", "--entrypoint", "python", tag, "-c", PY_PLAYWRIGHT_SMOKE]
        label = "Chromium de playwright levanta en la imagen"
    else:
        return None
    try:
        r = _run(cmd, timeout=180)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return {"label": label, "ok": r.returncode == 0, "exit_code": r.returncode, "output": out[-2000:]}
    except subprocess.TimeoutExpired:
        return {"label": label, "ok": False, "exit_code": None, "output": "timeout (180s)"}


def register(sub):
    p = sub.add_parser("docker", help="buildear la imagen Docker del proyecto en local")
    s = p.add_subparsers(dest="cmd", required=True)
    c = s.add_parser(
        "check",
        help="build + smoke test de la imagen (el arbol local o un PR), y la borra al terminar",
    )
    c.add_argument("--pr", type=int, help="buildear la rama de este PR (en un worktree aparte)")
    c.add_argument("--remote", default="origin", help="remoto de donde bajar el PR (default: origin)")
    c.add_argument("-f", "--file", default="Dockerfile", help="Dockerfile a usar (default: Dockerfile)")
    c.add_argument("--smoke", help='comando a correr dentro de la imagen (sh -c), p.ej. "node -v"')
    c.add_argument(
        "--build-env", action="append", metavar="KEY[=VAL]",
        help="variable de build (las que el deploy le pasa al build); sin valor va un placeholder. Repetible",
    )
    c.add_argument("--keep", action="store_true", help="no borrar la imagen al terminar")
    c.add_argument("--timeout", type=int, default=1800, help="tope del build en segundos (default: 1800)")
    c.set_defaults(func=cmd_check)
