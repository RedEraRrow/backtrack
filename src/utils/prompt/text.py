"""One-line text and path entry, and editing a longer text in the system editor."""
from __future__ import annotations
import sys
import os
import tempfile
import subprocess
from src.utils.prompt_core import (
    _get_term_attrs, _set_raw, _restore_term_attrs, _wait_for_keypress, _render_status_bar,
    block_cursor, block_cursor_width, _read_key, _cols, _wrap_bordered_input_lines,
    screen_paint, screen_invalidate, screen_takeover_next,
)
from src.utils import ui_utils
from src.utils.prompt import chrome
from src.utils.prompt.chrome import CHROME_HANDLED, CHROME_REDRAW, MODE_TOGGLE, _MODE_TOGGLE_KEY, append_chrome, consume_chrome, disable_mouse, enable_mouse
from src.utils.prompt_core import C


def text(message: str, default: str = "") -> str | None:
    """Free-text line editor with cursor movement and wrapping; Enter returns the buffer, Ctrl-C cancels."""
    buf    = list(default)
    pos    = len(buf)
    fd     = sys.stdin.fileno()
    old    = _get_term_attrs(fd)
    result = None
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    # Track how many physical lines were drawn to clear them later
    prev_lines = 0

    def _render():
        nonlocal prev_lines
        cols = _cols()
        content = "".join(buf)
        # Frame = '  '(mh) + '│ ' + content + ' │'; content = cols-4 makes the
        # frame span exactly the inter-margin width for an even 2/2 margin.
        content_width = max(1, cols - 4)

        wrapped_lines = _wrap_bordered_input_lines(content, content_width)
        pre_lines = _wrap_bordered_input_lines(content[:pos], content_width)
        cursor_row = max(0, len(pre_lines) - 1)
        cursor_col = len(pre_lines[-1]) if pre_lines else 0

        # This prompt owns the screen (it full-clears on entry), so it is laid
        # out absolutely like every other widget: message, input frame, then the
        # hint bar pinned above the miniplayer. The caret is drawn as a block on
        # the character, like every other field — a real terminal caret is at the
        # mercy of the terminal's own cursor style, and could be a thin bar or
        # invisible where the block always reads.
        # No "(^t widget)" suffix on the message: ^t is in the hint bar below,
        # like every other key. The title says what the screen is, not how to
        # leave it.
        out = [f"  {C.DIM}{message}{C.RESET}"]
        rows = (wrapped_lines if cursor_row < len(wrapped_lines)
                else wrapped_lines + [""])          # caret parked on a fresh wrap
        for i, line in enumerate(rows):
            shown, used = ((block_cursor(line, cursor_col), block_cursor_width(line, cursor_col))
                           if i == cursor_row else (line, len(line)))
            out.append(f"  {C.DIM}│{C.RESET} {shown}{' ' * max(0, content_width - used)} "
                       f"{C.DIM}│{C.RESET}")

        pairs = [("↵", "save"), ("esc", "back")]
        if chrome._value_toggle_enabled:
            pairs.append(("^t", chrome._toggle_hint_label))
        append_chrome(out, pairs, _hint_cells)

        # One diffed frame (no full erase, no newlines): only the rows that
        # actually changed are written, so typing doesn't repaint the screen.
        frame = {i + 1: line for i, line in enumerate([""] * ui_utils.MARGIN_V + out)}
        for r in range(len(frame) + 1, prev_lines + ui_utils.MARGIN_V + 1):
            frame[r] = ""
        screen_paint(frame)          # the caret is drawn, not the terminal's own

        prev_lines = len(out)
        _render_status_bar()

    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _render()
        while True:
            if ui_utils.consume_resize():
                ui_utils.clear_screen()
                _render()
                continue
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch

            if chrome._value_toggle_enabled and key == _MODE_TOGGLE_KEY:
                chrome._toggle_carry = "".join(buf)
                return MODE_TOGGLE  # type: ignore[return-value]
            if   key == 'CTRL_C':             result = None;         break
            elif key == 'ESC':                result = None;         break
            elif key == 'ENTER':              result = "".join(buf); break
            elif key == 'BACKSPACE' and pos > 0:
                buf.pop(pos - 1); pos -= 1; _render()
            elif key == 'LEFT' and pos > 0:
                pos -= 1; _render()
            elif key == 'RIGHT' and pos < len(buf):
                pos += 1; _render()
            elif key == 'UP':
                pos = max(0, pos - _cols()); _render()
            elif key == 'DOWN':
                pos = min(len(buf), pos + _cols()); _render()
            elif key == 'HOME':
                pos = 0; _render()
            elif key == 'END':
                pos = len(buf); _render()
            elif key == 'SPACE':
                buf.insert(pos, ' '); pos += 1; _render()
            elif len(key) == 1 and key.isprintable():
                buf.insert(pos, key); pos += 1; _render()
    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui_utils.clear_screen()
        sys.stdout.write(C.HIDE)   # caret was ours; don't leave it blinking
        sys.stdout.flush()

    return result


def path(message: str, default: str = "") -> str | None:
    """Filesystem-path editor with Tab-cycling autocomplete against the current directory listing."""
    buf          = list(default)
    pos          = len(buf)
    fd           = sys.stdin.fileno()
    old          = _get_term_attrs(fd)
    result       = None
    _tab_matches : list = []
    _tab_index   = 0

    # Tracks the exact number of rows written in the previous render cycle
    # to roll back cleanly without scrolling or flickering the viewport.
    _last_rendered_lines = 1
    _hint_cells: dict = {}   # clickable hint keys, filled by append_chrome

    def _completions(current: str) -> list:
        """List non-hidden entries in `current`'s directory matching its basename stub, for Tab completion."""
        try:
            expanded = os.path.expanduser(current)
            # Find the root lookup folder depending on whether the path target is a valid directory
            base     = expanded if os.path.isdir(expanded) else os.path.dirname(expanded) or "."
            stub     = "" if os.path.isdir(expanded) else os.path.basename(expanded)
            return sorted(
                os.path.join(base, e)
                for e in os.listdir(base)
                if e.startswith(stub) and not e.startswith('.')
            )
        except OSError:
            return []

    def _render():
        nonlocal _last_rendered_lines
        cols    = _cols()
        content = "".join(buf)
        prefix  = "  │ "
        # cols-4 makes the '  │ … │' frame span the inter-margin width (even 2/2).
        max_w   = max(1, cols - 4)

        if pos >= max_w:
            # Scrolled: keep one column spare so the block has somewhere to sit
            # rather than straddling the frame's right border.
            display  = content[pos - max_w + 1: pos]
            disp_pos = len(display)
        else:
            display  = content[:max_w]
            disp_pos = pos

        cursor_col = len(prefix) + disp_pos
        shown = block_cursor(display, disp_pos)
        pad   = max(0, max_w - block_cursor_width(display, disp_pos))

        render_stream = [
            f"  {C.DIM}{message}{C.RESET}\r\n",
            f"  {C.DIM}│{C.RESET} {shown}{' ' * pad} {C.DIM}│{C.RESET}"
        ]

        lines_count = 2

        # Do not output autocomplete options when at a subdirectory juncture or when empty
        should_show_hints = content and not content.endswith('/') and not content.endswith(os.path.sep)

        visible_matches = []
        if should_show_hints and _tab_matches:
            stub = os.path.basename(content)
            for m in _tab_matches:
                name = os.path.basename(m.rstrip('/'))
                if name.startswith(stub):
                    visible_matches.append(m)

        if visible_matches:
            render_stream.append("\r\n\033[K")
            lines_count += 1

            start_pad = min(cursor_col, max(0, cols - 35))
            render_stream.append(" " * start_pad)

            tooltip_parts = []
            current_len = start_pad

            for idx, match in enumerate(visible_matches[:5]):
                name = os.path.basename(match.rstrip('/'))
                if os.path.isdir(match):
                    name += "/"

                if idx == (_tab_index % len(visible_matches)):
                    item_str = f"{C.INVERT}{C.BOLD}{name}{C.RESET}"
                    visible_len = len(name)
                else:
                    item_str = f"{C.DIM}{name}{C.RESET}"
                    visible_len = len(name)

                if current_len + visible_len + 2 > cols:
                    render_stream.append("  ".join(tooltip_parts) + "\r\n\033[K" + " " * start_pad)
                    lines_count += 1
                    tooltip_parts = [item_str]
                    current_len = start_pad + visible_len
                else:
                    tooltip_parts.append(item_str)
                    current_len += visible_len + 2

            if tooltip_parts:
                render_stream.append("  ".join(tooltip_parts))

            if len(visible_matches) > 5:
                render_stream.append(f" {C.DIM}(+{len(visible_matches)-5}){C.RESET}")

        _prev_rendered = _last_rendered_lines
        _last_rendered_lines = lines_count

        # Laid out absolutely, like every other screen: the completion tooltip
        # still rides just under the frame, but the hint bar is pinned above the
        # miniplayer instead of trailing whatever the tooltip left behind.
        out = "".join(render_stream).split("\r\n")
        pairs = [("↵", "save"), ("tab/⇧tab", "complete"), ("esc", "back")]
        append_chrome(out, pairs, _hint_cells)

        # One diffed frame — see text() above.
        frame = {i + 1: line for i, line in enumerate([""] * ui_utils.MARGIN_V + out)}
        for r in range(len(frame) + 1, _prev_rendered + ui_utils.MARGIN_V + 2):
            frame[r] = ""
        screen_paint(frame)          # the caret is drawn, not the terminal's own
        _render_status_bar()

    try:
        _set_raw(fd)
        enable_mouse()          # so the hint keys below can be clicked
        screen_takeover_next()   # paint over the previous screen, no flash
        _tab_matches = _completions("".join(buf))
        _render()

        while True:
            if ui_utils.consume_resize(): _render()
            if not _wait_for_keypress(0.05): continue
            key = _read_key(fd)

            _ch = consume_chrome(key, _hint_cells)
            if _ch is CHROME_HANDLED:
                continue
            if _ch is CHROME_REDRAW:
                _render(); continue
            if _ch is not None:
                key = _ch

            if key == 'CTRL_C':
                result = None; break
            elif key == 'ESC':
                result = None; break
            elif key == 'ENTER':
                result = "".join(buf); break

            elif key in ('TAB', 'BACKTAB'):
                current_text = "".join(buf)
                stub = os.path.basename(current_text) if (current_text and not current_text.endswith('/')) else ""

                visible_matches = [m for m in _tab_matches if os.path.basename(m.rstrip('/')).startswith(stub)] if stub else _tab_matches

                if visible_matches:
                    if key == 'BACKTAB':
                        _tab_index -= 2      # step back past the one just offered
                    completed = visible_matches[_tab_index % len(visible_matches)]
                    if os.path.isdir(completed) and not completed.endswith("/"):
                        completed += "/"

                    buf[:] = list(completed)
                    pos = len(buf)
                    _tab_index += 1

                    _tab_matches = _completions("".join(buf))
                _render()
                continue

            elif key == 'BACKSPACE' and pos > 0:
                buf.pop(pos - 1); pos -= 1
                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()
            elif key == 'SPACE':
                buf.insert(pos, ' '); pos += 1
                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()
            elif key == 'LEFT'  and pos > 0:
                pos -= 1; _render()
            elif key == 'RIGHT' and pos < len(buf):
                pos += 1; _render()
            elif key == 'HOME':
                pos = 0; _render()
            elif key == 'END':
                pos = len(buf); _render()
            elif len(key) == 1 and key.isprintable():
                buf.insert(pos, key); pos += 1

                _tab_matches = _completions("".join(buf))
                _tab_index = 0
                _render()

    finally:
        disable_mouse()
        _restore_term_attrs(fd, old)
        ui_utils.clear_screen()
        sys.stdout.write(C.HIDE)   # caret was ours; don't leave it blinking
        sys.stdout.flush()

    return result


_EDITOR_FALLBACKS = ['micro', 'nano', 'vim', 'vi', 'emacs']


def _find_editor() -> str | None:
    """Return the editor to use: $EDITOR if set and found, else first available fallback."""
    import shutil
    env_editor = os.environ.get('EDITOR', '').strip()
    if env_editor:
        cmd = env_editor.split()[0]
        if shutil.which(cmd):
            return env_editor
    for ed in _EDITOR_FALLBACKS:
        if shutil.which(ed):
            return ed
    return None


def system_editor_edit(initial_text: str) -> str | None:
    """Open system editor for long text."""
    with tempfile.NamedTemporaryFile(suffix=".txt", mode='w+', encoding='utf-8', delete=False) as tf:
        tf.write(initial_text)
        temp_path = tf.name
    try:
        editor = _find_editor() or 'nano'
        sys.stdout.write("\033[?7h"); sys.stdout.flush()     # the editor expects wrapping on
        try:
            subprocess.run(editor.split() + [temp_path], check=True)
        finally:
            sys.stdout.write("\033[?7l")                     # ours again (see enter_alt_screen)
        # The editor owned the screen and left its own cursor visible: forget what
        # we thought was on screen and hide the cursor again before the caller
        # repaints, so no caret is left blinking over our frame.
        screen_invalidate()
        sys.stdout.write(C.HIDE)
        sys.stdout.flush()
        with open(temp_path, 'r', encoding='utf-8') as f:
            result = f.read().strip()
        return result if result else None
    except (OSError, subprocess.CalledProcessError) as e:
        ui_utils.show_status(f"Error launching editor: {e}")
        return None
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
