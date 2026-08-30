"""Compatibility alias for :mod:`src.eisv.formatting`.

The legacy module name remains importable and resolves to the canonical module
object so exported identities and any module-level state stay shared.
"""

import importlib
import runpy
import sys


_CANONICAL_MODULE = "src.eisv.formatting"

if __name__ == "__main__":
    runpy.run_module(_CANONICAL_MODULE, run_name="__main__")
else:
    sys.modules[__name__] = importlib.import_module(_CANONICAL_MODULE)
