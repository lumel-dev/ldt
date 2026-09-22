"""Cliente del daemon: lo levanta si hace falta y le manda operaciones.

La regla de oro aca es **no dejar nunca dos Chromiums para la misma sesion**. Levantar uno
nuevo sin cerrar el anterior deja un huerfano sin archivo de sesion: invisible, comiendo
RAM, y encima capaz de desregistrar al nuevo cuando expira. Por eso `alive` distingue
"ocupada" de "muerta", `start` mata lo que haya antes de arrancar, y todo daemon queda
registrado por pid en `DAEMONS` para que un huerfano siempre se pueda encontrar.
"""

from __future__ import annotations

import base64
import errno
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ldt import core
from ldt.core import DAEMONS, LOGS, SESSIONS, ensure_dirs

DAEMON_MODULE = "ldt.browser.daemon"
REPO_ROOT = Path(__file__).resolve().parents[2]


class BrowserError(RuntimeError):
    pass


def session_path(name: str) -> Path:
    return SESSIONS / f"{name}.json"


def read_session(name: str) -> dict | None:
    path = session_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def call(name: str, op: str, args: dict | None = None, timeout: float = 200) -> dict:
    info = read_session(name)
    if not info:
        raise BrowserError(f"no hay sesion de browser '{name}' (abrila con: ldt browser open <url>)")
    body = json.dumps({"op": op, "args": args or {}, "token": info["token"]}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/rpc", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        raise BrowserError(f"la sesion '{name}' no responde ({exc}). Probá: ldt browser close --all") from exc
    if not payload.get("ok"):
        raise BrowserError(payload.get("error", "error desconocido"))
    return payload.get("result") or {}


def state(name: str) -> str:
    """`none` | `dead` | `busy` | `alive`.

    La distincion entre `busy` y `dead` es el corazon de esto: `ping` se contesta fuera de
    la cola del daemon, asi que si no responde y ademas el pid esta muerto, la sesion se
    fue de verdad. Si el pid vive, esta ocupada o arrancando — y levantar otra seria
    justamente el bug.
    """
    info = read_session(name)
    if not info:
        return "none"
    try:
        call(name, "ping", timeout=3)
        return "alive"
    except BrowserError:
        pass
    return "busy" if core.running(info.get("pid")) else "dead"


def alive(name: str) -> bool:
    """Si hay una sesion con la que vale la pena contar (aunque este ocupada)."""
    return state(name) in ("alive", "busy")


# --------------------------------------------------------------------- huerfanos


def daemon_file(pid: int) -> Path:
    return DAEMONS / f"{pid}.json"


def daemons() -> list[dict]:
    """Todo daemon registrado cuyo proceso siga vivo. Limpia los registros vencidos."""
    ensure_dirs()
    out = []
    for f in DAEMONS.glob("*.json"):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            f.unlink(missing_ok=True)
            continue
        if core.running(info.get("pid")):
            out.append(info)
        else:
            f.unlink(missing_ok=True)
    return out


def orphans(name: str | None = None) -> list[dict]:
    """Daemons vivos que ya no son la sesion registrada: nadie puede hablarles."""
    out = []
    for info in daemons():
        if name and info.get("name") != name:
            continue
        session = read_session(info.get("name") or "")
        if not session or session.get("pid") != info.get("pid"):
            out.append(info)
    return out


def reap(name: str | None = None) -> list[dict]:
    """Mata los huerfanos. Son basura por definicion: no hay forma de usarlos."""
    killed = []
    for info in orphans(name):
        core.kill_tree(info.get("pid"))
        daemon_file(info["pid"]).unlink(missing_ok=True)
        killed.append(info)
    return killed


# ------------------------------------------------------------------------ lock


def _acquire(name: str, timeout: float = 90) -> int | None:
    """Lock exclusivo por sesion: dos comandos en paralelo no pueden abrir dos browsers."""
    ensure_dirs()
    path = SESSIONS / f"{name}.lock"
    deadline = time.time() + timeout
    while True:
        try:
            return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except OSError as exc:
            if exc.errno not in (errno.EEXIST, errno.EACCES):
                raise
            try:
                stale = time.time() - path.stat().st_mtime > timeout
            except OSError:
                stale = True
            if stale or time.time() > deadline:
                path.unlink(missing_ok=True)
                continue
            time.sleep(0.2)


def _release(name: str, fd: int | None) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    (SESSIONS / f"{name}.lock").unlink(missing_ok=True)


def _evict(name: str) -> None:
    """Deja el nombre libre sin dejar nada corriendo.

    Antes esto era un `unlink` pelado del archivo de sesion: si el daemon seguia vivo,
    quedaba un Chromium al que ya nadie podia hablarle ni cerrar. Ahora primero se mata.
    """
    info = read_session(name)
    if info and core.running(info.get("pid")):
        core.kill_tree(info["pid"])
        daemon_file(info["pid"]).unlink(missing_ok=True)
        for _ in range(25):
            if not core.running(info["pid"]):
                break
            time.sleep(0.2)
    session_path(name).unlink(missing_ok=True)
    reap(name)


def start(name: str, **opts) -> dict:
    """Levanta el daemon en background y espera a que publique su archivo de sesion."""
    ensure_dirs()
    _evict(name)
    payload = base64.b64encode(json.dumps({"session": name, **opts}).encode()).decode()
    log = open(LOGS / f"browser-{name}.log", "ab")
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "PYTHONIOENCODING": "utf-8"}
    kwargs: dict = {"stdout": log, "stderr": log, "stdin": subprocess.DEVNULL, "env": env, "cwd": str(REPO_ROOT)}
    if sys.platform == "win32":
        # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: el navegador sobrevive al comando que
        # lo abrio (que es todo el punto de tener sesion entre invocaciones) y ademas su
        # salida sigue yendo al log, que es lo unico que hay para diagnosticar si no arranca.
        kwargs["creationflags"] = 0x08000000 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([sys.executable, "-m", DAEMON_MODULE, payload], **kwargs)

    deadline = time.time() + 60
    while time.time() < deadline:
        if read_session(name) and alive(name):
            return read_session(name)
        if proc.poll() is not None:
            break  # murio al arrancar: no tiene sentido esperar el minuto entero
        time.sleep(0.3)
    # Que no quede a medio arrancar: aunque no haya publicado la sesion, ya puede tener un
    # Chromium abierto, y sin registro nadie lo encontraria despues.
    if core.running(proc.pid):
        core.kill_tree(proc.pid)
    daemon_file(proc.pid).unlink(missing_ok=True)
    tail = (LOGS / f"browser-{name}.log").read_text(encoding="utf-8", errors="replace")[-1500:]
    raise BrowserError(f"el daemon no arranco. Ultimas lineas del log:\n{tail}")


def ensure(name: str, **opts) -> tuple[dict, bool]:
    """Devuelve (sesion, recien_levantada): quien llama avisa que hay que cerrarla."""
    if alive(name):
        return read_session(name), False
    fd = _acquire(name)
    try:
        # Otro comando puede haberla levantado mientras esperabamos el lock.
        if alive(name):
            return read_session(name), False
        return start(name, **opts), True
    finally:
        _release(name, fd)


def sessions() -> list[dict]:
    ensure_dirs()
    out = []
    for path in SESSIONS.glob("*.json"):
        try:
            if path.stat().st_size:
                out.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def stop(name: str) -> bool:
    """Cierra la sesion. Devuelve si habia algo vivo que cerrar.

    Nunca se limita a borrar el archivo: si el daemon no contesta pero el proceso vive
    (estaba ocupado), borrar el registro y dejarlo correr es exactamente como se fabricaba
    un huerfano.
    """
    st = state(name)
    info = read_session(name)
    if st == "none":
        return bool(reap(name))
    if st == "dead":
        session_path(name).unlink(missing_ok=True)
        reap(name)
        return False

    if st == "alive":
        try:
            call(name, "shutdown", timeout=20)
        except BrowserError:
            pass
        for _ in range(25):
            if not session_path(name).exists():
                break
            time.sleep(0.2)

    # Ocupada, o no se cerro sola: al arbol. `os.kill` dejaba vivo el Chromium hijo.
    if info and core.running(info.get("pid")):
        core.kill_tree(info["pid"])
        daemon_file(info["pid"]).unlink(missing_ok=True)
    session_path(name).unlink(missing_ok=True)
    reap(name)
    return True
