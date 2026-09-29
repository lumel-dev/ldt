"""`ldt ports` — que esta escuchando, y matarlo.

En Windows el ciclo "levanto el dev server, se queda colgado, el puerto queda tomado"
es constante y `lsof`/`fuser` no existen. Esto lo resuelve en un comando.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from ldt import core


def _win_listeners() -> list[dict]:
    # Sin `-p tcp`: eso filtra a IPv4, y desde Node 17 Vite y compania escuchan en `[::1]`.
    # Con el filtro, un server que escucha solo en IPv6 era invisible: `dev start` daba el
    # puerto por libre y `dev list` nunca lo marcaba listo. Las filas v6 tambien dicen "TCP".
    net = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
    names: dict[int, str] = {}
    tl = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    for line in tl.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].strip('"').isdigit():
            names[int(parts[1].strip('"'))] = parts[0].strip('"')

    rows = []
    for line in net.splitlines():
        m = re.match(r"\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
        if not m:
            continue
        addr, port, pid = m.group(1), int(m.group(2)), int(m.group(3))
        rows.append({"port": port, "addr": addr, "pid": pid, "process": names.get(pid, "?")})
    return _dedupe(rows)


def _dedupe(rows: list[dict]) -> list[dict]:
    """Una misma escucha aparece repetida (IPv4 + IPv6, o un fd por conexion)."""
    seen, uniq = set(), []
    for r in sorted(rows, key=lambda r: r["port"]):
        key = (r["port"], r["pid"])
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def _ss_listeners() -> list[dict]:
    out = subprocess.run(["ss", "-ltnp"], capture_output=True, text=True).stdout
    rows = []
    for line in out.splitlines()[1:]:
        m = re.search(r"(\S+):(\d+)\s", line)
        if not m:
            continue
        # El pid solo viene para los procesos propios: sin root, `ss` no lo muestra. La
        # fila va igual aunque no se sepa quien es. Descartarla es peor: `free_port` da
        # el puerto por libre y `dev start` arranca encima de lo que ya estaba ahi.
        who = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
        rows.append(
            {
                "port": int(m.group(2)),
                "addr": m.group(1),
                "pid": int(who.group(2)) if who else None,
                "process": who.group(1) if who else "?",
            }
        )
    return _dedupe(rows)


def _lsof_listeners() -> list[dict]:
    out = subprocess.run(
        ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines()[1:]:
        # COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME, con NAME = addr:puerto.
        m = re.match(r"(\S+)\s+(\d+)\s+.*?(\S+):(\d+)\s+\(LISTEN\)", line)
        if m:
            rows.append(
                {"port": int(m.group(4)), "addr": m.group(3), "pid": int(m.group(2)), "process": m.group(1)}
            )
    return _dedupe(rows)


def _posix_listeners() -> list[dict]:
    """`ss` en Linux, `lsof` en macOS: `ss` es de iproute2 y en BSD no existe."""
    if shutil.which("ss"):
        return _ss_listeners()
    if shutil.which("lsof"):
        return _lsof_listeners()
    core.die("para ver los puertos hace falta `ss` (iproute2) o `lsof`")


def listeners() -> list[dict]:
    return _win_listeners() if core.IS_WIN else _posix_listeners()


# La implementacion vive en core para que la compartan `dev`, `cleanup` y el browser sin
# que ninguno tenga que importar un modulo de `cmds`.
kill_pid = core.kill_tree


def owner(port: int) -> dict | None:
    """Quien escucha en ese puerto, si hay alguien."""
    for r in listeners():
        if r["port"] == port:
            return r
    return None


def free_port(start: int, span: int = 20) -> int | None:
    """El primer puerto libre desde `start`. Para no pisar el server de otro."""
    taken = {r["port"] for r in listeners()}
    for port in range(start, start + span + 1):
        if port not in taken:
            return port
    return None


def cmd_ports(a):
    rows = listeners()
    if a.ports:
        rows = [r for r in rows if r["port"] in a.ports]
    if a.filter:
        rows = [r for r in rows if a.filter.lower() in r["process"].lower()]

    if not a.kill:
        core.out({"listening": rows}, a.json, core.table(rows, ["port", "addr", "pid", "process"]))
        return

    if not a.ports and not a.filter:
        core.die("--kill necesita un puerto o --filter (no mata todo lo que escucha)")
    if not rows:
        core.out({"killed": []}, a.json, "no hay nada escuchando ahi")
        return
    killed = [{**r, "ok": kill_pid(r["pid"])} for r in rows]
    core.out({"killed": killed}, a.json, core.table(killed, ["port", "pid", "process", "ok"]))


def register(sub):
    p = sub.add_parser("ports", help="puertos escuchando, y matar al que molesta")
    p.add_argument("ports", nargs="*", type=int, help="filtrar por puerto(s)")
    p.add_argument("--filter", help="filtrar por nombre de proceso")
    p.add_argument("--kill", action="store_true", help="matar los procesos que quedaron listados")
    p.set_defaults(func=cmd_ports)
