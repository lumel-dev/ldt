"""`ldt http` — pegarle a una API con salida legible, y esperar a que un server levante.

`curl` tambien sirve, pero desde PowerShell esta aliaseado a Invoke-WebRequest y las
comillas de un body JSON se vuelven una pesadilla. Esto ademas resuelve rutas relativas
contra el puerto del proyecto: `ldt http get /api/health` sabe que es localhost:3000.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from ldt import core


def resolve_url(a, url: str) -> str:
    if url.startswith("/"):
        port = a.port or core.detect(core.find_root(Path(a.cwd)))["port"] or 3000
        return f"http://localhost:{port}{url}"
    if "://" not in url:
        return f"http://{url}"
    return url


def request(url: str, method: str, headers: dict, body: bytes | None, timeout: float) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status, resp_headers = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:  # 4xx/5xx son respuestas, no fallos del comando
        raw = exc.read()
        status, resp_headers = exc.code, dict(exc.headers)
    ms = round((time.time() - started) * 1000)
    text = raw.decode("utf-8", errors="replace")
    parsed = None
    if "json" in (resp_headers.get("Content-Type", "")).lower():
        try:
            parsed = json.loads(text)
        except ValueError:
            pass
    return {"status": status, "ms": ms, "headers": resp_headers, "json": parsed, "text": text, "bytes": len(raw)}


def cmd_http(a):
    url = resolve_url(a, a.url)
    headers = {"User-Agent": "ldt/1.0", "Accept": "application/json, */*"}
    for h in a.header or []:
        k, _, v = h.partition(":")
        headers[k.strip()] = v.strip()
    if a.token:
        headers["Authorization"] = f"Bearer {a.token}"

    body = None
    if a.data:
        body = a.data.encode()
        headers.setdefault("Content-Type", "application/json" if a.data.lstrip().startswith(("{", "[")) else "text/plain")
    method = a.method.upper() if a.method else ("POST" if body else "GET")

    try:
        res = request(url, method, headers, body, a.timeout)
    except Exception as exc:
        core.die(f"{type(exc).__name__}: {exc}  ({method} {url})")

    payload = json.dumps(res["json"], indent=2, ensure_ascii=False) if res["json"] is not None else res["text"]
    if len(payload) > a.limit:
        payload = payload[: a.limit] + f"\n... (truncado, {res['bytes']} bytes)"
    text = f"{method} {url}\nHTTP {res['status']}  {res['ms']}ms  {res['bytes']}b\n\n{payload}"
    core.out({**res, "url": url, "method": method}, a.json, text)
    if res["status"] >= 400 and a.strict:
        raise SystemExit(1)


def cmd_wait(a):
    url = resolve_url(a, a.url)
    deadline = time.time() + a.for_
    last = ""
    while time.time() < deadline:
        try:
            res = request(url, "GET", {"User-Agent": "ldt/1.0"}, None, 5)
            if res["status"] < 500:
                core.out(
                    {"url": url, "status": res["status"], "waited_s": round(a.for_ - (deadline - time.time()), 1)},
                    a.json,
                    f"listo: {url} responde HTTP {res['status']}",
                )
                return
            last = f"HTTP {res['status']}"
        except Exception as exc:
            last = f"{type(exc).__name__}"
        time.sleep(0.5)
    core.die(f"{url} no respondio en {a.for_}s (ultimo: {last})")


def register(sub):
    p = sub.add_parser("http", help="requests HTTP con salida legible")
    p.add_argument("--port", type=int, help="puerto para rutas relativas (default: el del proyecto)")
    hs = p.add_subparsers(dest="http_cmd", required=True)

    for verb in ("get", "post", "put", "patch", "delete"):
        sp = hs.add_parser(verb, help=f"{verb.upper()} a una URL")
        sp.add_argument("url", help="URL completa o ruta relativa (/api/...) contra el server local")
        sp.add_argument("-d", "--data", help="body; si empieza con { o [ se manda como JSON")
        sp.add_argument("-H", "--header", action="append", help='header "Clave: valor" (repetible)')
        sp.add_argument("--token", help="atajo para Authorization: Bearer <token>")
        sp.add_argument("--timeout", type=float, default=30)
        sp.add_argument("--limit", type=int, default=8000, help="maximo de caracteres del body a imprimir")
        sp.add_argument("--strict", action="store_true", help="exit code 1 si el status es >= 400")
        sp.set_defaults(func=cmd_http, method=verb)

    sp = hs.add_parser("wait", help="esperar a que una URL responda (post dev server start)")
    sp.add_argument("url")
    sp.add_argument("--for", dest="for_", type=float, default=60, help="segundos maximos de espera")
    sp.set_defaults(func=cmd_wait)
