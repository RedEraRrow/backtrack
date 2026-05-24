"""
Terminal input handling for raw mode keyboard input.

Provides non-blocking keyboard input, raw mode management, and escape sequence handling.
"""
from __future__ import annotations
import os
import sys
import select
import time
import termios
from contextlib import contextmanager


# Global state for escape sequence buffering
_pending_escape = None
_escape_start_time = None


@contextmanager
def raw_mode(file):
    """
    Context manager for raw terminal mode (no echo, no canonical input).
    
    Allows capturing keypresses without requiring Enter.
    Automatically restores terminal settings on exit.
    """
    old_attrs = termios.tcgetattr(file.fileno())
    new_attrs = termios.tcgetattr(file.fileno())
    new_attrs[3] &= ~(termios.ECHO | termios.ICANON)
    try:
        termios.tcsetattr(file.fileno(), termios.TCSADRAIN, new_attrs)
        yield
    finally:
        termios.tcsetattr(file.fileno(), termios.TCSADRAIN, old_attrs)


def is_data_available() -> bool:
    """Check if there is keyboard data waiting to be read."""
    return select.select([sys.stdin], [], [], 0) == ([sys.stdin], [], [])


def clear_escape_buffer() -> None:
    """Clear pending escape sequence buffer."""
    global _pending_escape, _escape_start_time
    _pending_escape = None
    _escape_start_time = None


def get_key_non_blocking() -> str | None:
    """
    Read a single key press without blocking.
    
    Properly handles multi-byte escape sequences (e.g., arrow keys).
    Returns the complete key sequence or None if no input available.
    """
    global _pending_escape, _escape_start_time

    # If we have a complete escape sequence, return it
    if _pending_escape and len(_pending_escape) >= 3 and _pending_escape[-1] in 'ABCD':
        result = _pending_escape
        _pending_escape = None
        return result

    # Check if input is available
    if not select.select([sys.stdin], [], [], 0)[0]:
        return None

    # Read one byte
    c = sys.stdin.read(1)
    full = (_pending_escape or "") + c

    # Handle escape sequences
    if full.startswith('\x1b'):
        if _escape_start_time is None:
            _escape_start_time = time.time()
        if len(full) >= 3 and full[-1] in 'ABCD':
            _pending_escape = None
            return full
        _pending_escape = full
        return None

    _pending_escape = None
    return full


def is_arrow_key(key: str | None) -> str | None:
    """
    Check if key is an arrow key sequence.
    
    Args:
        key: The key sequence to check
        
    Returns:
        The arrow key direction ('A'=up, 'B'=down, 'C'=right, 'D'=left) or None
    """
    if key and len(key) >= 3 and key[-1] in 'ABCD':
        return key[-1]
    return None
