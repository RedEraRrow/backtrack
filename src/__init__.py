"""Backtrack package entry point."""

import os

if os.name == "nt":
    try:
        import colorama

        colorama.init()
    except ImportError:
        pass

__all__ = []
