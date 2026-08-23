"""Raw keyboard input and escape-sequence decoding."""
from __future__ import annotations
import os
import sys
import time
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


# Global state for escape sequence buffering
_pending_escape = None
_escape_start_time = None


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


def clear_escape_buffer() -> None:
    """Discard any partially-buffered escape sequence."""
    global _pending_escape, _escape_start_time
    _pending_escape = None
    _escape_start_time = None


def get_key_non_blocking() -> str | None:
    """Return one decoded key or escape sequence if available, without
    blocking; buffers partial CSI sequences across calls."""
    global _pending_escape, _escape_start_time

    # Flush a stale escape buffer that was never completed (e.g. from a focus
    # event like \033[O whose terminator isn't in 'ABCD').  Without this the
    # buffer poisons every subsequent keypress indefinitely.
    if _pending_escape and _escape_start_time is not None:
        if time.time() - _escape_start_time > 0.1:
            _pending_escape = None
            _escape_start_time = None

    # If we have a complete escape sequence, return it
    if _pending_escape and len(_pending_escape) >= 3 and _pending_escape[-1] in 'ABCD':
        result = _pending_escape
        _pending_escape = None
        return result

    if _IS_WINDOWS:
        assert msvcrt is not None
        if not msvcrt.kbhit():
            return None
        c = msvcrt.getwch()
        if c in ('\x00', '\xe0'):
            ext = msvcrt.getwch()
            return {
                'H': '\x1b[A', 'P': '\x1b[B', 'K': '\x1b[D', 'M': '\x1b[C',
                'G': '\x1b[H', 'O': '\x1b[F', 'I': '\x1b[5~', 'Q': '\x1b[6~', 'S': '\x1b[3~'
            }.get(ext, None)
        if c == '\r':
            return '\n'
        if c == '\x08':
            return '\x7f'
        return c

    assert select is not None
    # Read from the raw fd via os.read, NOT sys.stdin.read: the buffered reader
    # can slurp the rest of an escape sequence into Python's buffer where select
    # (which polls the OS fd) can't see it — making the greedy loop below wait
    # out its timeout on every arrow. os.read keeps select and reads consistent.
    fd = sys.stdin.fileno()
    if not select.select([fd], [], [], 0)[0]:
        return None

    c = os.read(fd, 1).decode('utf-8', 'replace')
    full = (_pending_escape or "") + c

    if full.startswith('\x1b'):
        if _escape_start_time is None:
            _escape_start_time = time.time()
        # The bytes of an arrow/CSI key arrive together, so finish the sequence
        # in THIS call rather than one byte per loop iteration — otherwise a
        # single tap is slow and can be dropped by the stale-flush, forcing you
        # to hold the key. A short per-byte wait keeps it non-blocking.
        # SGR mouse reports (\x1b[<btn;col;row{M|m}) run longer than arrows and
        # end in 'M'/'m', so they get their own terminator + larger cap.
        is_sgr = full.startswith('\x1b[<')
        while True:
            if is_sgr:
                if full[-1] in 'Mm':
                    break
            elif len(full) >= 3 and full[-1] in 'ABCD~':
                break
            if full in ('\x1b[I', '\x1b[O'):
                break
            if len(full) >= (32 if is_sgr else 8):    # safety cap
                break
            if not select.select([fd], [], [], 0.02)[0]:
                break
            full += os.read(fd, 1).decode('utf-8', 'replace')
            if full.startswith('\x1b[<'):
                is_sgr = True
        if is_sgr and full[-1] in 'Mm':
            _pending_escape = None
            _escape_start_time = None
            body, term = full[3:-1], full[-1]
            parts = body.split(';')
            if len(parts) == 3:
                try:
                    btn, col, row = int(parts[0]), int(parts[1]), int(parts[2])
                    if btn == 64: return 'SCROLL_UP'
                    if btn == 65: return 'SCROLL_DOWN'
                    if term == 'm': return f'MOUSE_RELEASE:{btn}:{row}:{col}'
                    if btn in (0, 1, 2): return f'MOUSE_CLICK:{btn}:{row}:{col}'
                except ValueError:
                    pass
            return None
        if len(full) >= 3 and full[-1] in 'ABCD':
            _pending_escape = None
            _escape_start_time = None
            return full
        # Complete focus-reporting sequences (\033[I / \033[O) shouldn't linger.
        if full in ('\x1b[I', '\x1b[O'):
            _pending_escape = None
            _escape_start_time = None
            return 'FOCUS_IN' if full == '\x1b[I' else 'FOCUS_OUT'
        # A lone Esc (no continuation arrived) is a real keypress — emit it rather
        # than buffering it, where the stale-flush would silently drop it. Callers
        # match the raw '\x1b'; is_arrow_key ignores it (length 1).
        if full == '\x1b':
            _pending_escape = None
            _escape_start_time = None
            return '\x1b'
        _pending_escape = full
        return None

    _pending_escape = None
    return full


def is_arrow_key(key: str | None) -> str | None:
    """Return the arrow letter ('A'/'B'/'C'/'D') if `key` is an arrow escape
    sequence, else None."""
    if key and len(key) >= 3 and key[-1] in 'ABCD':
        return key[-1]
    return None
