"""Presentacion: color, simbolos y marcos. Stdlib puro, como todo el resto.

Regla de oro: **nada de esto aparece cuando la salida no es una terminal**. El consumidor
principal de `ldt` es un agente que captura stdout, y un escape ANSI ahi es ruido que
ademas cuesta tokens. Con `isatty()` en False, todo lo de aca devuelve texto pelado
identico al de siempre.
"""

from __future__ import annotations

import os
import sys

RESET = "\x1b[0m"
CODES = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "ok": "\x1b[32m",
    "warn": "\x1b[33m",
    "err": "\x1b[31m",
    "accent": "\x1b[36m",
    "invert": "\x1b[7m",
}

_color: bool | None = None


def _enable_vt() -> bool:
    """En Windows la consola no interpreta ANSI hasta que se lo pedis.

    Windows Terminal ya viene con esto puesto, pero conhost (el de siempre) no: sin esto
    los codigos se imprimen crudos.
    """
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def supports_color() -> bool:
    global _color
    if _color is None:
        _color = bool(
            not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
            and getattr(sys.stdout, "isatty", lambda: False)()
            and _enable_vt()
        )
    return _color


def can_redraw() -> bool:
    """Si se puede mover el cursor para repintar en el lugar (menu interactivo).

    Separado de `supports_color` a proposito: NO_COLOR apaga el color, no la navegacion.
    """
    return bool(getattr(sys.stdout, "isatty", lambda: False)() and _enable_vt())


def paint(text: str, *styles: str) -> str:
    if not text or not supports_color():
        return text
    prefix = "".join(CODES[s] for s in styles if s in CODES)
    return f"{prefix}{text}{RESET}" if prefix else text


def dim(t: str) -> str:
    return paint(t, "dim")


def bold(t: str) -> str:
    return paint(t, "bold")


def ok(t: str) -> str:
    return paint(t, "ok")


def warn(t: str) -> str:
    return paint(t, "warn")


def err(t: str) -> str:
    return paint(t, "err")


def accent(t: str) -> str:
    return paint(t, "accent")


# ------------------------------------------------------------------------ simbolos


def ascii_only() -> bool:
    """La consola de Windows a veces no puede con los bordes lindos; ahi va ASCII."""
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "╭─╯│✓❯".encode(enc)
    except (LookupError, UnicodeEncodeError):
        return True
    return False


_SYMBOLS = {
    "ok": ("✓", "ok"),
    "warn": ("!", "warn"),
    "err": ("×", "err"),
    "arrow": ("❯", "accent"),
    "bullet": ("·", "dim"),
    "sub": ("▸", "dim"),
    "sep": ("·", "dim"),
}
_ASCII = {"ok": "ok", "warn": "!", "err": "x", "arrow": ">", "bullet": "-", "sub": ">", "sep": "-"}


def sym(kind: str, colored: bool = True) -> str:
    glyph = _ASCII[kind] if ascii_only() else _SYMBOLS[kind][0]
    return paint(glyph, _SYMBOLS[kind][1]) if colored else glyph


def status(kind: str, text: str) -> str:
    return f"{sym(kind)} {text}"


# --------------------------------------------------------------------------- marcos


def box(title: str, subtitle: str = "") -> str:
    """El marco del cartel de bienvenida."""
    if ascii_only():
        subtitle = subtitle.replace(" · ", " - ")
        tl, tr, bl, br, h, v = "+", "+", "+", "+", "-", "|"
    else:
        tl, tr, bl, br, h, v = "╭", "╮", "╰", "╯", "─", "│"
    inner = max(len(title), len(subtitle))
    width = inner + 4
    lines = [f"{tl}{h * width}{tr}"]
    for text, style in ((title, "bold"), (subtitle, "dim")):
        if not text:
            continue
        # El padding se calcula sobre el texto sin pintar: los escapes no ocupan ancho.
        lines.append(f"{v}  {paint(text, style)}{' ' * (inner - len(text))}  {v}")
    lines.append(f"{bl}{h * width}{br}")
    return "\n".join(lines)


def rule(width: int = 42) -> str:
    return dim(("-" if ascii_only() else "─") * width)


def section(title: str) -> str:
    return bold(title)


def kv(label: str, value: str, pad: int = 9) -> str:
    return f"{dim(label.ljust(pad))} {value}"
