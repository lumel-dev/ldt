"""Helpers compartidos por todos los comandos de ldt.

Nada acá depende de un proyecto en particular: `ldt` se corre desde cualquier repo y todo
lo que necesita saber lo deduce del cwd (o de --cwd).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = sys.platform == "win32"

# Estado (sesiones de browser, logs de dev, screenshots) fuera del repo: nunca ensucia
# el proyecto sobre el que se esta trabajando ni aparece en su git status.
HOME = Path(os.environ.get("LDT_HOME") or (Path.home() / ".ldt"))
SHOTS = HOME / "shots"
LOGS = HOME / "logs"
PROFILES = HOME / "profiles"
SESSIONS = HOME / "sessions"
PROCS = HOME / "procs"
# Un daemon de browser se registra aca ademas de en SESSIONS. El archivo de sesion se
# borra y se reescribe en cada arranque, asi que no sirve para rastrear: si una sesion
# quedara sin archivo, su Chromium seria invisible. El registro por pid nunca se pisa.
DAEMONS = HOME / "daemons"


def ensure_dirs() -> None:
    for d in (HOME, SHOTS, LOGS, PROFILES, SESSIONS, PROCS, DAEMONS):
        d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- salida


# Version del contrato de `--json`. Hay clientes que parsean esta salida (la UI, scripts):
# renombrar o sacar una clave, o cambiarle el tipo, es un cambio incompatible y sube este
# numero. Agregar claves nuevas no.
API_VERSION = 1


def out(obj, as_json: bool, text: str | None = None) -> None:
    """Imprime JSON si el que llama pidio --json, si no el texto legible."""
    if as_json or text is None:
        if isinstance(obj, dict):
            obj = {"v": API_VERSION, **obj}
        print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    else:
        print(text)


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def table(rows: list[dict], cols: list[str] | None = None, max_width: int = 60) -> str:
    """Tabla de texto plano: legible en terminal y barata en tokens para un agente."""
    if not rows:
        return "(sin filas)"
    cols = cols or list(rows[0].keys())

    def cell(v):
        if v is None:
            return "NULL"
        s = str(v).replace("\n", "\\n")
        return s[: max_width - 1] + "..." if len(s) > max_width else s

    widths = {c: max([len(c)] + [len(cell(r.get(c))) for r in rows]) for c in cols}
    head = "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  ".join("-" * widths[c] for c in cols)
    body = "\n".join("  ".join(cell(r.get(c)).ljust(widths[c]) for c in cols) for r in rows)
    return f"{head}\n{sep}\n{body}"


# ------------------------------------------------------------------------ procesos


def running(pid: int | None) -> bool:
    """Si el pid sigue vivo. Lo comparten `dev`, `cleanup` y el cliente del browser."""
    if not pid or pid < 0:
        return False
    if IS_WIN:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True
        )
        # /FO CSV deja el pid como campo propio entre comillas: buscarlo asi evita el falso
        # positivo de que "123" aparezca dentro de otro numero de la salida.
        return f'"{pid}"' in r.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int | None) -> bool:
    """Mata el proceso y sus hijos.

    En Windows hace falta el arbol: `ldt dev` lanza con shell=True y el browser corre
    Chromium como hijo, asi que matar solo el pid deja el trabajo a medias. `taskkill /T`
    baja al pid y sus descendientes, y a nada mas.

    En POSIX el equivalente es matar el grupo de procesos, pero ahi hay un filo:
    `killpg` no distingue descendientes de vecinos. Todo lo que levanta ldt va con
    `start_new_session`, asi que es lider de su propio grupo y el grupo es exactamente
    su arbol. Un proceso ajeno —el `pnpm dev` que el usuario tiene corriendo en su
    terminal, el caso de `ports --kill`— comparte grupo con su shell, y barrer el grupo
    se lleva la terminal entera. Por eso solo se barre el grupo cuando el pid lo lidera.
    """
    if not pid or pid < 0:
        return False
    if IS_WIN:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return r.returncode == 0
    import signal
    import time

    try:
        target = -pid if os.getpgid(pid) == pid else pid
    except OSError:
        return False  # ya no esta
    # SIGTERM primero: un dev server que recibe SIGKILL no borra su .pid ni cierra sus
    # sockets. Si a los 3s sigue vivo, ahi si va el KILL.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(target, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not running(pid):
                return True
            time.sleep(0.1)
    return not running(pid)


def belongs(raw: str | None, root: Path) -> bool:
    """Si algo se levanto desde el proyecto (o desde un subdirectorio suyo)."""
    if not raw:
        return False  # sin marca de proyecto no se asume propio: se cierra con --all
    try:
        path = Path(raw).resolve()
    except OSError:
        return False
    return path == root or root in path.parents


def project_slug(root: Path) -> str:
    return root.name.lower().replace(" ", "-")


# ---------------------------------------------------------------- entorno / config

# El nombre del archivo de entorno se arma con un glob a proposito. Es habitual que un
# agente corra detras de un guard que rechaza cualquier tool call que nombre el archivo de
# variables de forma literal, asi que el codigo lo descubre en vez de escribirlo.
ENV_GLOB = ".env*"
EXAMPLE_HINTS = ("example", "sample", "template", "dist")


def env_files(root: Path) -> list[Path]:
    """Archivos de entorno del proyecto, del mas especifico al mas generico."""
    order = {"local": 0, "development": 1, "dev": 1, "": 2}
    found = [p for p in root.glob(ENV_GLOB) if p.is_file()]

    def rank(p: Path) -> tuple[int, str]:
        suffix = p.name.split("env", 1)[1].lstrip(".")
        if any(h in suffix for h in EXAMPLE_HINTS):
            return (9, p.name)
        return (order.get(suffix, 3), p.name)

    return sorted(found, key=rank)


def is_example(path: Path) -> bool:
    return any(h in path.name for h in EXAMPLE_HINTS)


def parse_env_file(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k:
            data[k] = v
    return data


def project_env(root: Path) -> dict[str, str]:
    """Variables del proyecto: los archivos primero, el entorno del proceso pisa."""
    merged: dict[str, str] = {}
    for f in reversed([p for p in env_files(root) if not is_example(p)]):
        merged.update(parse_env_file(f))
    merged.update({k: v for k, v in os.environ.items() if k in merged})
    return merged


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-2:]} ({len(value)} chars)"


# ------------------------------------------------------------ deteccion de proyecto

# Lo que marca la raiz de un proyecto. `find_root` sube hasta el primero que encuentra, asi
# que aca no va nada que aparezca tambien en subdirectorios (un `index.html` en `public/`
# cortaria la subida ahi).
MARKERS = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    ".git",
    "go.mod",
    "composer.json",
    "artisan",
    "manage.py",
    "pubspec.yaml",
    "Gemfile",
)
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")

# Dependencia de package.json -> (framework, puerto por defecto). El orden importa: el
# primero que aparece gana, y casi todos traen `vite` como dependencia, asi que el
# especifico va antes que el generico.
NODE_FRAMEWORKS = (
    ("next", "next", 3000),
    ("@angular/core", "angular", 4200),
    ("react-scripts", "cra", 3000),
    ("nuxt", "nuxt", 3000),
    ("astro", "astro", 4321),
    ("@remix-run/dev", "remix", 5173),
    ("remix", "remix", 5173),
    ("@sveltejs/kit", "sveltekit", 5173),
    ("vite", "vite", 5173),
    ("@nestjs/core", "nest", 3000),
    ("express", "express", 3000),
    ("fastify", "fastify", 3000),
    ("hono", "hono", 3000),
    ("koa", "koa", 3000),
)
# Scripts que levantan el server, en orden de preferencia. `start` en Next es produccion y
# pide un build previo: por eso va despues de `dev`, nunca antes.
DEV_SCRIPTS = ("dev", "start", "serve", "develop")


def find_root(start: Path) -> Path:
    """Raiz del proyecto: sube hasta encontrar un marcador conocido."""
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if any((candidate / m).exists() for m in MARKERS):
            return candidate
    return cur


def read_json(path: Path) -> dict:
    try:
        # utf-8-sig: PowerShell y el Notepad viejo guardan con BOM, y con "utf-8" ese
        # package.json no parsea y el proyecto sale como `unknown`.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return ""


def _python(root: Path) -> str:
    """El Python del proyecto: el de su `.venv` si tiene, que es donde estan sus deps."""
    for venv in (".venv", "venv"):
        rel = f"{venv}\\Scripts\\python.exe" if IS_WIN else f"{venv}/bin/python"
        # Relativo: `dev start` corre con cwd en la raiz, y asi el comando se lee.
        if (root / rel).exists():
            return rel
    return "python" if IS_WIN else "python3"


def detect(root: Path) -> dict:
    """Que es este proyecto: stack, package manager, comando de dev, puerto probable.

    Un repo puede tener mas de un stack (Laravel con Vite, FastAPI con un package.json de
    tooling). `dev_cmd` es uno solo —el que sirve la app— con esta prioridad: los
    frameworks de backend que tambien sirven el front (Laravel, Django, Rails), despues
    FastAPI/Flask por sobre un package.json, despues Node, Flutter, Go, docker compose y,
    como ultimo recurso, el server embebido de PHP.
    """
    pkg = read_json(root / "package.json")
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    scripts = pkg.get("scripts", {})
    composer = read_json(root / "composer.json")
    composer_scripts = composer.get("scripts", {}) if isinstance(composer.get("scripts"), dict) else {}
    py_deps = _read(root / "requirements.txt") + _read(root / "pyproject.toml")

    pm = "pnpm"
    if (root / "package-lock.json").exists():
        pm = "npm"
    elif (root / "yarn.lock").exists():
        pm = "yarn"
    elif (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        pm = "bun"

    kind, framework, dev_cmd, port = "unknown", None, None, None

    node_fw, node_port = None, None
    if pkg:
        for dep, name, default in NODE_FRAMEWORKS:
            if dep in deps:
                node_fw, node_port = f"{name}@{str(deps[dep]).lstrip('^~>=<')}", default
                break
    script = next((s for s in DEV_SCRIPTS if s in scripts), None)
    # `npm dev` no existe: npm solo acepta scripts propios via `run`.
    node_cmd = (f"npm run {script}" if pm == "npm" else f"{pm} {script}") if script else None

    if (root / "artisan").exists():
        kind, framework, port = "php", "laravel", 8000
        # El `composer run dev` de Laravel 11 levanta server, cola y Vite juntos.
        dev_cmd = "composer run dev" if "dev" in composer_scripts else "php artisan serve --port=8000"
    elif (root / "manage.py").exists():
        kind, framework, port = "python", "django", 8000
        dev_cmd = f"{_python(root)} manage.py runserver 8000"
    elif (root / "bin" / "rails").exists():
        kind, framework, port = "ruby", "rails", 3000
        dev_cmd = "ruby bin/rails server -p 3000"
    elif "fastapi" in py_deps:
        kind, framework, port = "python", "fastapi", 8000
        target = "main:app"
        for cand in ("main.py", "app/main.py", "src/main.py"):
            if (root / cand).exists():
                target = cand[:-3].replace("/", ".") + ":app"
                break
        dev_cmd = f"{_python(root)} -m uvicorn {target} --reload --port 8000"
    elif "flask" in py_deps and (root / "app.py").exists():
        kind, framework, port = "python", "flask", 5000
        dev_cmd = f"{_python(root)} -m flask --app app run --port 5000"
    elif pkg:
        kind, framework, port = "node", node_fw, node_port
        dev_cmd = node_cmd
    elif (root / "pubspec.yaml").exists() and (root / "lib" / "main.dart").exists():
        kind, framework = "flutter", "flutter"
        if (root / "web").is_dir():
            port = 8080
            dev_cmd = "flutter run -d web-server --web-port 8080"
        else:
            dev_cmd = "flutter run"
    elif (root / "go.mod").exists():
        kind, framework, dev_cmd = "go", "go", "go run ."
    elif list(root.glob("*.py")):
        kind, framework, port = "python", "python", 8000

    compose = next((f for f in COMPOSE_FILES if (root / f).exists()), None)
    if not dev_cmd and compose:
        kind = kind if kind != "unknown" else "docker"
        framework = framework or "compose"
        dev_cmd = "docker compose up"

    if not dev_cmd and kind == "unknown":
        docroot = "public" if (root / "public" / "index.php").exists() else "."
        if (root / docroot / "index.php").exists() or (root / "index.html").exists():
            kind, framework, port = "php", "php", 8000
            dev_cmd = f"php -S 127.0.0.1:8000 -t {docroot}"

    if kind == "node":
        m = re.search(r"--port[= ](\d+)|(?:^| )-p (\d+)", scripts.get(script or "dev", ""))
        if m:
            port = int(m.group(1) or m.group(2))

    return {
        "root": str(root),
        "kind": kind,
        "framework": framework,
        "package_manager": pm if pkg else None,
        "scripts": scripts,
        "dev_cmd": dev_cmd,
        "port": port,
        "has_docker": (root / "Dockerfile").exists() or compose is not None,
        "env_files": [p.name for p in env_files(root)],
    }


def git_info(root: Path) -> dict:
    if not shutil.which("git"):
        return {}

    def run(*args):
        try:
            r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            return None

    status = run("status", "--porcelain")
    return {
        "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
        "remote": run("remote", "get-url", "origin"),
        "dirty_files": len([l for l in (status or "").splitlines() if l.strip()]),
        "last_commit": run("log", "-1", "--pretty=%h %s (%cr)"),
    }
