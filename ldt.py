#!/usr/bin/env python
"""Entrada directa: `python <ruta>/ldt.py ...` desde cualquier directorio."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ldt.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
