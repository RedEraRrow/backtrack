"""Raw terminal mode and a non-blocking key read, for the loops that poll for keys
between redraws (the player, the activity centre, the trim conveyor)."""
from __future__ import annotations
import os
import sys
from contextlib import contextmanager
from typing import Any

_IS_WINDOWS = os.name == 'nt'

# Expose module-level names so static type checkers don't consider them
# possibly unbound when imports are platform-gated.
msvcrt: Any | None = None
select: Any | None = None
termios: Any | None = None

if _IS_WINDOWS:
    import msvcrt as _msvcrt
    msvcrt = _msvcrt
else:
    import select as _select
    import termios as _termios
    select = _select
    termios = _termios


@contextmanager
def raw_mode(file):
    """Raw terminal mode: no echo, no canonical input."""
    if _IS_WINDOWS:
        yield
        return

    # At runtime termios is available on non-Windows platforms; assert
    # so static analyzers know the name is defined.
    assert termios is not None
    old_attrs = termios.tcgetattr(file.fileno())
    new_attrs = termios.tcgetattr(file.fileno())
    new_attrs[3] &= ~(termios.ECHO | termios.ICANON)
    try:
        termios.tcsetattr(file.fileno(), termios.TCSADRAIN, new_attrs)
        yield
    finally:
        termios.tcsetattr(file.fileno(), termios.TCSADRAIN, old_attrs)


def get_key_non_blocking() -> str | None:
    """The next key if one is waiting, else None, without blocking. Decoded by
    prompt_core's reader, the one every screen uses, so keys arrive by the same
    names everywhere ('UP', 'ESC', 'SPACE', 'MOUSE_CLICK:…')."""
    if _IS_WINDOWS:
        assert msvcrt is not None
        if not msvcrt.kbhit():
            return None
    else:
        assert select is not None
        if not select.select([sys.stdin.fileno()], [], [], 0)[0]:
            return None
    from src.utils.prompt_core import _read_key_raw
    return _read_key_raw(sys.stdin.fileno()) or None
