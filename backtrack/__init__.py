"""
Backtrack package entry point.

Windows ANSI support is initialised once in ``backtrack.main.main()`` via
``colorama.just_fix_windows_console()``; no package-import-time init here.
"""

__all__ = []
