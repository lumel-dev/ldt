"""Daemon del browser: mantiene un Chromium vivo entre invocaciones del CLI.

Por que un daemon y no abrir el browser en cada comando: un agente trabaja en varios
pasos (abrir, loguearse, hacer click, mirar la consola). Si cada comando levantara su
propio navegador se perderia la sesion, las cookies y el estado del SPA entre pasos, y
cada paso pagaria ~2s de arranque.

El daemon corre en background, escucha JSON-RPC sobre HTTP en 127.0.0.1 y guarda su
puerto en un archivo de sesion (`~/.ldt/sessions/<nombre>.json`). El CLI lo
levanta solo la primera vez.

La API sync de Playwright no es thread-safe: todo lo que la toca corre en el thread
principal. El server HTTP vive en threads aparte y encola callables en `self.jobs`.

Unica excepcion a la cola: `ping`, que se contesta en el thread HTTP porque no toca
Playwright. Es lo que permite distinguir "la sesion murio" de "la sesion esta ocupada":
si `ping` tuviera que hacer fila detras de un `goto` lento, el cliente la daria por muerta
y levantaria un segundo Chromium, dejando este huerfano.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ldt.core import DAEMONS, PROFILES, SESSIONS, SHOTS, ensure_dirs  # noqa: E402

BUFFER_CAP = 500  # consola / red: se guardan los ultimos N eventos por pagina

# Marca los elementos interactivos visibles con data-ldt-ref para poder referenciarlos
# despues como @1, @2, ... sin inventar selectores fragiles.
JS_ELEMENTS = """
() => {
  document.querySelectorAll('[data-ldt-ref]').forEach(e => e.removeAttribute('data-ldt-ref'));
  const sel = 'a,button,input,select,textarea,summary,[role=button],[role=link],[role=tab],'
    + '[role=checkbox],[role=switch],[role=menuitem],[role=option],[onclick],[contenteditable=true]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') continue;
    i += 1;
    el.setAttribute('data-ldt-ref', String(i));
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder
      || el.title || el.getAttribute('name') || '').toString().trim().replace(/\\s+/g, ' ').slice(0, 90);
    out.push({
      ref: i,
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      label,
      href: (el.getAttribute('href') || '').slice(0, 120),
      disabled: !!el.disabled,
      in_viewport: r.top < innerHeight && r.bottom > 0,
    });
  }
  return out;
}
"""


def session_file(name: str) -> Path:
    return SESSIONS / f"{name}.json"


def daemon_file(pid: int) -> Path:
    return DAEMONS / f"{pid}.json"


class Daemon:
    def __init__(self, opts: dict):
        self.opts = opts
        self.name = opts["session"]
        self.jobs: queue.Queue = queue.Queue()
        self.stopping = False
        self.last_used = time.time()
        self.idle_timeout = float(opts.get("idle_timeout") or 1800)
        self.token = secrets.token_hex(16)
        self.pages: list = []
        self.current = 0
        self.buffers: dict[int, dict] = {}
        self.default_timeout = float(opts.get("timeout") or 15) * 1000

    # ------------------------------------------------------------------ playwright

    def start_browser(self):
        from playwright.sync_api import sync_playwright

        self.pw = sync_playwright().start()
        headless = self.opts.get("headless", True)
        args = ["--disable-blink-features=AutomationControlled"]
        viewport = {"width": self.opts.get("width", 1440), "height": self.opts.get("height", 900)}
        ctx_kwargs: dict = {"viewport": viewport, "ignore_https_errors": True}
        if self.opts.get("device"):
            ctx_kwargs = {**self.pw.devices[self.opts["device"]], "ignore_https_errors": True}

        profile = self.opts.get("profile")
        if profile:
            # Contexto persistente: el login que hace una persona a mano queda guardado y
            # las corridas siguientes del agente arrancan ya autenticadas.
            user_dir = PROFILES / profile
            user_dir.mkdir(parents=True, exist_ok=True)
            self.browser = None
            self.context = self.pw.chromium.launch_persistent_context(
                str(user_dir), headless=headless, args=args, **ctx_kwargs
            )
        else:
            self.browser = self.pw.chromium.launch(headless=headless, args=args)
            self.context = self.browser.new_context(**ctx_kwargs)

        self.context.set_default_timeout(self.default_timeout)
        self.context.on("page", self._wire_page)
        page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self._wire_page(page)

    def _wire_page(self, page):
        if page in self.pages:
            return
        self.pages.append(page)
        buf = {"console": [], "errors": [], "requests": [], "downloads": []}
        self.buffers[id(page)] = buf

        def keep(lst, item):
            lst.append(item)
            if len(lst) > BUFFER_CAP:
                del lst[: len(lst) - BUFFER_CAP]

        def on_console(msg):
            loc = msg.location or {}
            keep(
                buf["console"],
                {
                    "type": msg.type,
                    "text": msg.text[:2000],
                    "at": f"{loc.get('url', '')}:{loc.get('lineNumber', '')}",
                    "ts": time.time(),
                },
            )

        def on_error(err):
            keep(buf["errors"], {"text": str(err)[:4000], "ts": time.time()})

        def on_failed(req):
            keep(
                buf["requests"],
                {
                    "method": req.method,
                    "url": req.url[:300],
                    "status": None,
                    "failure": (req.failure or "")[:200],
                    "ts": time.time(),
                },
            )

        def on_response(resp):
            if resp.status >= 400:
                keep(
                    buf["requests"],
                    {
                        "method": resp.request.method,
                        "url": resp.url[:300],
                        "status": resp.status,
                        "failure": None,
                        "ts": time.time(),
                    },
                )

        page.on("console", on_console)
        page.on("pageerror", on_error)
        page.on("requestfailed", on_failed)
        page.on("response", on_response)
        page.on("download", lambda d: keep(buf["downloads"], {"url": d.url, "name": d.suggested_filename}))
        page.on("close", lambda p=page: self._drop_page(p))

    def _drop_page(self, page):
        if page in self.pages:
            idx = self.pages.index(page)
            self.pages.remove(page)
            self.buffers.pop(id(page), None)
            if self.current >= len(self.pages):
                self.current = max(0, len(self.pages) - 1)
            elif idx < self.current:
                self.current -= 1

    @property
    def page(self):
        self.pages = [p for p in self.pages if not p.is_closed()]
        if not self.pages:
            page = self.context.new_page()
            self._wire_page(page)
            self.current = 0
        self.current = min(self.current, len(self.pages) - 1)
        return self.pages[self.current]

    @property
    def buf(self):
        return self.buffers.setdefault(
            id(self.page), {"console": [], "errors": [], "requests": [], "downloads": []}
        )

    def locator(self, target: str):
        """Resuelve un target: @N (ref de `elements`) o cualquier selector de Playwright.

        Playwright ya entiende `text=`, `role=`, `xpath=` y CSS pelado, asi que lo unico
        que agrega esto es el atajo @N.
        """
        if target.startswith("@"):
            return self.page.locator(f'[data-ldt-ref="{target[1:]}"]')
        return self.page.locator(target)

    # ------------------------------------------------------------------------- ops

    def op_ping(self, **_):
        """Prueba de vida barata. No toca Playwright, asi que el Handler la contesta sin
        pasar por la cola; aca esta igual por si alguien la pide por la via normal."""
        return {
            "session": self.name,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "busy": not self.jobs.empty(),
            "headless": self.opts.get("headless", True),
            "profile": self.opts.get("profile"),
        }

    def op_status(self, **_):
        return {
            "session": self.name,
            "pages": [{"index": i, "url": p.url, "title": p.title()} for i, p in enumerate(self.pages)],
            "current": self.current,
            "headless": self.opts.get("headless", True),
            "profile": self.opts.get("profile"),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def op_goto(self, url: str, wait: str = "load", **_):
        if "://" not in url:
            url = f"http://{url}"
        resp = self.page.goto(url, wait_until=wait)
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "status": resp.status if resp else None,
            "ok": bool(resp is None or resp.ok),
        }

    def op_reload(self, **_):
        self.page.reload()
        return {"url": self.page.url, "title": self.page.title()}

    def op_back(self, **_):
        self.page.go_back()
        return {"url": self.page.url, "title": self.page.title()}

    def op_forward(self, **_):
        self.page.go_forward()
        return {"url": self.page.url, "title": self.page.title()}

    def op_click(self, target: str, count: int = 1, force: bool = False, **_):
        self.locator(target).first.click(click_count=count, force=force)
        self.page.wait_for_timeout(250)
        return {"clicked": target, "url": self.page.url}

    def op_fill(self, target: str, value: str, **_):
        self.locator(target).first.fill(value)
        return {"filled": target}

    def op_type(self, target: str, value: str, delay: int = 30, **_):
        loc = self.locator(target).first
        loc.click()
        loc.type(value, delay=delay)
        return {"typed": target}

    def op_press(self, key: str, target: str | None = None, **_):
        if target:
            self.locator(target).first.press(key)
        else:
            self.page.keyboard.press(key)
        self.page.wait_for_timeout(200)
        return {"pressed": key, "url": self.page.url}

    def op_hover(self, target: str, **_):
        self.locator(target).first.hover()
        return {"hovered": target}

    def op_select(self, target: str, value: str, **_):
        self.locator(target).first.select_option(value)
        return {"selected": value}

    def op_check(self, target: str, checked: bool = True, **_):
        loc = self.locator(target).first
        loc.check() if checked else loc.uncheck()
        return {"target": target, "checked": checked}

    def op_upload(self, target: str, files: list, **_):
        self.locator(target).first.set_input_files(files)
        return {"uploaded": files}

    def op_scroll(self, to: str = "bottom", **_):
        if to == "bottom":
            self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        elif to == "top":
            self.page.evaluate("window.scrollTo(0, 0)")
        else:
            self.locator(to).first.scroll_into_view_if_needed()
        self.page.wait_for_timeout(200)
        return {"scrolled": to}

    def op_text(self, target: str = "body", limit: int = 20000, **_):
        txt = self.locator(target).first.inner_text()
        return {"text": txt[:limit], "truncated": len(txt) > limit, "length": len(txt)}

    def op_html(self, target: str = "html", limit: int = 20000, **_):
        html = self.locator(target).first.evaluate("el => el.outerHTML")
        return {"html": html[:limit], "truncated": len(html) > limit, "length": len(html)}

    def op_snapshot(self, target: str = "body", limit: int = 20000, **_):
        """Arbol ARIA de la pagina: mucho mas barato en tokens que el HTML crudo."""
        snap = self.locator(target).first.aria_snapshot()
        return {"snapshot": snap[:limit], "truncated": len(snap) > limit}

    def op_elements(self, **_):
        items = self.page.evaluate(JS_ELEMENTS)
        return {"elements": items, "count": len(items)}

    def op_eval(self, expression: str, **_):
        return {"result": self.page.evaluate(expression)}

    def op_screenshot(self, path: str | None = None, full_page: bool = False, target: str | None = None, **_):
        if not path:
            path = str(SHOTS / f"{self.name}-{int(time.time())}.png")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        if target:
            self.locator(target).first.screenshot(path=path)
        else:
            self.page.screenshot(path=path, full_page=full_page)
        return {"path": path, "bytes": Path(path).stat().st_size}

    def op_pdf(self, path: str | None = None, **_):
        if not path:
            path = str(SHOTS / f"{self.name}-{int(time.time())}.pdf")
        self.page.pdf(path=path)
        return {"path": path}

    def op_console(self, level: str | None = None, limit: int = 100, clear: bool = False, **_):
        items = self.buf["console"]
        if level:
            wanted = {"error": {"error"}, "warn": {"warning", "warn"}}.get(level, {level})
            items = [c for c in items if c["type"] in wanted]
        result = {"console": items[-limit:], "total": len(items)}
        if clear:
            self.buf["console"].clear()
        return result

    def op_network(self, limit: int = 100, clear: bool = False, **_):
        items = self.buf["requests"]
        result = {"requests": items[-limit:], "total": len(items)}
        if clear:
            self.buf["requests"].clear()
        return result

    def op_errors(self, **_):
        """Todo lo que salio mal en la pagina, en una sola respuesta."""
        buf = self.buf
        console_errors = [c for c in buf["console"] if c["type"] == "error"]
        return {
            "url": self.page.url,
            "page_errors": buf["errors"][-50:],
            "console_errors": console_errors[-50:],
            "failed_requests": buf["requests"][-50:],
            "count": len(buf["errors"]) + len(console_errors) + len(buf["requests"]),
        }

    def op_clear(self, **_):
        for key in ("console", "errors", "requests", "downloads"):
            self.buf[key].clear()
        return {"cleared": True}

    def op_wait(self, target: str | None = None, text: str | None = None, url: str | None = None,
                state: str | None = None, ms: int | None = None, timeout: float = 15, **_):
        t = timeout * 1000
        if target:
            self.locator(target).first.wait_for(state="visible", timeout=t)
        if text:
            self.page.get_by_text(text).first.wait_for(state="visible", timeout=t)
        if url:
            self.page.wait_for_url(url, timeout=t)
        if state:
            self.page.wait_for_load_state(state, timeout=t)
        if ms:
            self.page.wait_for_timeout(ms)
        return {"url": self.page.url, "title": self.page.title()}

    def op_tabs(self, index: int | None = None, url: str | None = None, close: bool = False, **_):
        if url is not None:
            page = self.context.new_page()
            self._wire_page(page)
            self.current = self.pages.index(page)
            page.goto(url if "://" in url else f"http://{url}")
        elif index is not None:
            if close:
                self.pages[index].close()
            else:
                self.current = index
                self.pages[index].bring_to_front()
        return self.op_status()

    def op_login_hint(self, **_):
        """Si la pagina parece estar pidiendo un login.

        Un solo round-trip con todas las senales juntas: preguntarlas por separado desde el
        CLI serian cuatro viajes y cuatro turnos de la cola.
        """
        url = self.page.url
        try:
            title = self.page.title()
        except Exception:
            title = ""
        reasons = []
        if self.page.locator("input[type=password]").count():
            reasons.append("hay un input de password")
        if re.search(r"/(log[-_]?in|sign[-_]?in|signin|login|auth|sso|accounts)(/|\?|$)", url, re.I):
            reasons.append("la URL parece de login")
        if re.search(r"(iniciar sesi|ingres|log ?in|sign ?in|autentic)", title, re.I):
            reasons.append(f"el titulo dice {title!r}")
        last = next((r for r in reversed(self.buf["requests"]) if r.get("status") in (401, 403)), None)
        if last:
            reasons.append(f"HTTP {last['status']} en {last['url']}")
        return {"likely": bool(reasons), "reasons": reasons, "url": url, "title": title}

    def op_cookies(self, **_):
        return {"cookies": self.context.cookies()}

    def op_storage(self, **_):
        local = self.page.evaluate("() => ({...localStorage})")
        session = self.page.evaluate("() => ({...sessionStorage})")
        return {"localStorage": local, "sessionStorage": session}

    def op_set_storage(self, key: str, value: str, kind: str = "local", **_):
        store = "localStorage" if kind == "local" else "sessionStorage"
        self.page.evaluate(f"([k, v]) => {store}.setItem(k, v)", [key, value])
        return {"set": key}

    def op_keepalive(self, seconds: float = 7200, **_):
        """Sube el tiempo de inactividad tolerado para esta sesion."""
        self.idle_timeout = max(self.idle_timeout, float(seconds))
        self.opts["idle_timeout"] = self.idle_timeout
        self.write_session()  # que `ldt status` muestre el valor real, no el de arranque
        return {"idle_timeout": self.idle_timeout}

    def op_shutdown(self, **_):
        self.stopping = True
        return {"stopped": True}

    # --------------------------------------------------------------- infraestructura

    def dispatch(self, op: str, args: dict):
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            raise ValueError(f"operacion desconocida: {op}")
        self.last_used = time.time()
        return fn(**args)

    def submit(self, op: str, args: dict, timeout: float = 180):
        box: queue.Queue = queue.Queue(1)
        self.jobs.put((op, args, box))
        try:
            return box.get(timeout=timeout)
        except queue.Empty:
            return (False, f"timeout esperando la operacion {op}")

    def serve(self):
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except Exception:
                    payload = {}
                if payload.get("token") != daemon.token:
                    self.send_error(403)
                    return
                op = payload.get("op", "")
                if op == "ping":
                    # Fuera de la cola a proposito: tiene que contestar aunque el thread
                    # principal este 3 minutos adentro de un goto. Si no, el cliente cree
                    # que la sesion murio y abre otro Chromium.
                    ok, value = True, daemon.op_ping()
                else:
                    ok, value = daemon.submit(op, payload.get("args") or {})
                body = json.dumps({"ok": ok, "result" if ok else "error": value}, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def write_daemon(self):
        """Registro por pid, aparte del archivo de sesion.

        La sesion se reescribe por nombre en cada arranque; esto no, asi que un Chromium
        nunca puede quedar corriendo sin que `ldt` pueda encontrarlo.
        """
        DAEMONS.mkdir(parents=True, exist_ok=True)
        daemon_file(os.getpid()).write_text(
            json.dumps(
                {"name": self.name, "pid": os.getpid(), "port": self.port, "started_at": self.started_at},
                indent=2,
            ),
            encoding="utf-8",
        )

    def write_session(self):
        session_file(self.name).write_text(
            json.dumps(
                {
                    "name": self.name,
                    "port": self.port,
                    "pid": os.getpid(),
                    "token": self.token,
                    "started_at": self.started_at,
                    "opts": {k: v for k, v in self.opts.items() if k != "token"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def run(self):
        ensure_dirs()
        self.started_at = time.time()
        self.start_browser()
        self.serve()
        self.write_session()
        self.write_daemon()
        while not self.stopping:
            try:
                op, args, box = self.jobs.get(timeout=5)
            except queue.Empty:
                if time.time() - self.last_used > self.idle_timeout:
                    break
                continue
            try:
                box.put((True, self.dispatch(op, args)))
            except Exception as exc:  # el CLI muestra el error, el daemon sigue vivo
                box.put((False, f"{type(exc).__name__}: {exc}"))
        self.cleanup()

    def cleanup(self):
        for closer in (
            lambda: self.server.shutdown(),
            lambda: self.context.close(),
            lambda: self.browser and self.browser.close(),
            lambda: self.pw.stop(),
        ):
            try:
                closer()
            except Exception:
                pass
        # Solo si la sesion registrada sigue siendo la nuestra. Un daemon viejo que expira
        # no tiene por que desregistrar al que lo reemplazo: el path sale del nombre, y
        # borrarlo hacia que el comando siguiente abriera un Chromium mas.
        try:
            current = json.loads(session_file(self.name).read_text(encoding="utf-8"))
        except Exception:
            current = None
        if current is None or current.get("pid") == os.getpid():
            session_file(self.name).unlink(missing_ok=True)
        daemon_file(os.getpid()).unlink(missing_ok=True)


def main():
    opts = json.loads(base64.b64decode(sys.argv[1]).decode())
    Daemon(opts).run()


if __name__ == "__main__":
    main()
