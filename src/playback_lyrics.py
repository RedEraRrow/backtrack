"""
Lyric processing helpers for the playback engine.
"""
from __future__ import annotations
import bisect
import re
import textwrap
import sys

from src import ui_utils


def normalise_lyric_newlines(text: str) -> str:
    """Ensure consistent newline format for lyric processing."""
    if not text:
        return ""
    return text.replace('\r\n', '\n').replace('\r', '\n')


_ABBREV_RE = re.compile(
    r'\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|St|vs|etc|approx|govt|dept|'
    r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec|'
    r'Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.',
    re.IGNORECASE,
)


def _sentence_split(text: str, wrap_w: int, max_lines: int) -> list[str]:
    """Split text into chunks that each wrap to at most max_lines rows."""
    flat = text.replace('\n', ' ').strip()
    masked = _ABBREV_RE.sub(lambda m: m.group().replace('.', '\x00'), flat)
    boundaries = [m.end() for m in re.finditer(r'[.!?]\s+', masked)]

    def _fits(chunk: str) -> bool:
        return len(textwrap.wrap(chunk, width=wrap_w)) <= max_lines

    if not boundaries:
        chunks, result = [flat], []
        while chunks:
            chunk = chunks.pop(0)
            if _fits(chunk):
                result.append(chunk)
            else:
                words = chunk.split()
                mid = max(1, len(words) // 2)
                result.append(' '.join(words[:mid]))
                remainder = ' '.join(words[mid:])
                if remainder:
                    chunks.insert(0, remainder)
        return result

    prev = 0
    sentences = []
    for b in boundaries:
        sentences.append(flat[prev:b].strip())
        prev = b
    tail = flat[prev:].strip()
    if tail:
        sentences.append(tail)

    chunks, current = [], ''
    for sentence in sentences:
        candidate = (current + ' ' + sentence).strip() if current else sentence
        if _fits(candidate):
            current = candidate
        else:
            if current:
                chunks.append(current)
            if _fits(sentence):
                current = sentence
            else:
                sub = _sentence_split(sentence, wrap_w, max_lines)
                chunks.extend(sub[:-1])
                current = sub[-1]
    if current:
        chunks.append(current)
    return chunks


_expand_cache: dict = {}


def expand_uslt_lines(
    lines: list[str],
    line_times: list[tuple],
    wrap_w: int,
    max_lines_per_chunk: int = 6,
) -> tuple[list[str], list[tuple]]:
    """Split long USLT lines into wrapped chunks and adjust their time windows."""
    cache_key = (wrap_w, max_lines_per_chunk, id(lines))
    if cache_key in _expand_cache:
        return _expand_cache[cache_key]

    exp_lines: list[str] = []
    exp_times: list[tuple] = []

    for text, (t_start, t_end) in zip(lines, line_times):
        if len(textwrap.wrap(text, width=wrap_w)) <= max_lines_per_chunk:
            exp_lines.append(text)
            exp_times.append((t_start, t_end))
            continue

        chunks = _sentence_split(text, wrap_w, max_lines_per_chunk)
        word_counts = [max(1, len(c.split())) for c in chunks]
        total_words = sum(word_counts)
        duration = t_end - t_start
        t = t_start

        for chunk, wc in zip(chunks, word_counts):
            chunk_dur = duration * (wc / total_words)
            exp_lines.append(chunk)
            exp_times.append((t, t + chunk_dur))
            t += chunk_dur

    _expand_cache[cache_key] = (exp_lines, exp_times)
    return exp_lines, exp_times


def build_uslt_line_times(lines: list, words_per_second: float = 2.2) -> list[tuple[float, float]]:
    """Pre-calculate start/end times for each USLT line by word count."""
    times = []
    t = 0.0

    for line in lines:
        text = line
        if ':' in text:
            text = text.split(':', 1)[1]
        text = re.sub(r'\([^)]*\)', '', text)
        text = re.sub(r'\[[^\]]*\]', '', text)
        n = len(text.split())
        duration = max(0.5, n / words_per_second)
        times.append((t, t + duration))
        t += duration

    return times


def find_current_uslt_line(line_times: list[tuple[float, float]], elapsed: float) -> int:
    """Find the current USLT line index using binary search."""
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def draw_lyric_window(row: int, sylt_data: list, current_idx: int,
                      width: int | None = None, max_row: int | None = None,
                      col: int = 1) -> None:
    """Display previous, current, and next lyrics for SYLT."""
    C = ui_utils.Colours
    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    budget = max(4, max_row - row - 1)
    wrap_w = max(20, width - 8)

    p_raw = sylt_data[current_idx - 1][0] if current_idx > 0 else ""
    c_raw = sylt_data[current_idx][0] if 0 <= current_idx < len(sylt_data) else ""
    n_raw = sylt_data[current_idx + 1][0] if 0 <= current_idx < len(sylt_data) - 1 else ""

    # Wrap prev/next to show context: last line of prev, first line of next
    p_flat = normalise_lyric_newlines(p_raw).replace('\n', ' ')
    p_wrapped = textwrap.wrap(p_flat, width=wrap_w - 1) if p_flat else []
    p_line = p_wrapped[-1] if p_wrapped else ""

    n_flat = normalise_lyric_newlines(n_raw).replace('\n', ' ')
    n_wrapped = textwrap.wrap(n_flat, width=wrap_w - 1) if n_flat else []
    n_line = n_wrapped[0] if n_wrapped else ""

    c_flat = normalise_lyric_newlines(c_raw).replace('\n', ' ')
    c_wrap = textwrap.wrap(c_flat, width=wrap_w - 4)[:max(1, budget - 4)]

    # Clear only the right-pane rows, keeping the left-side UI intact.
    # Increased to clear lingering USLT scroll hints from previous tracks
    clear_rows = max(12, 2 + len(c_wrap) + 3)
    for i in range(clear_rows):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")

    out_row = row
    # Horizontal rule separator from credits
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    rule = "─" * (width + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{ui_utils.Colours.DIM}{rule}{ui_utils.Colours.RESET}")
    out_row += 1
    # Blank padding line before previous
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if p_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {p_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    # Blank line between previous and current
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    for i, seg in enumerate(c_wrap or [""]):
        pfx = "▶ " if i == 0 else "  "
        if seg:
            sys.stdout.write(f"\033[{out_row};{col}H  {C.BOLD}{pfx}{seg}{C.RESET}")
        else:
            sys.stdout.write(f"\033[{out_row};{col}H\033[K")
        out_row += 1
    # Blank line between current and next
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if n_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {n_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    sys.stdout.flush()


def draw_uslt_window(row: int, all_lines: list, line_times: list,
                     elapsed: float, width: int | None = None,
                     manual_idx: int | None = None,
                     max_row: int | None = None,
                     col: int = 1) -> None:
    """Render prev/current/next USLT lines from an expanded line list."""
    C = ui_utils.Colours
    width = width or ui_utils.get_terminal_width()
    wrap_w = max(20, width - 8)
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows

    auto_idx = find_current_uslt_line(line_times, elapsed)
    display_idx = manual_idx if manual_idx is not None else auto_idx

    prev_text = all_lines[display_idx - 1] if display_idx > 0 else ""
    curr_text = all_lines[display_idx] if 0 <= display_idx < len(all_lines) else ""
    next_text = all_lines[display_idx + 1] if display_idx < len(all_lines) - 1 else ""

    budget = max(3, max_row - row - 1)
    curr_wrapped = textwrap.wrap(curr_text.replace('\n', ' '), width=wrap_w - 4) or ['']
    curr_wrapped = curr_wrapped[:max(1, budget - 4)]

    # Show last line of prev and first line of next for context
    prev_wrapped = textwrap.wrap(prev_text.replace('\n', ' '), width=wrap_w - 1) if prev_text else []
    prev_line = prev_wrapped[-1] if prev_wrapped else ""

    next_wrapped = textwrap.wrap(next_text.replace('\n', ' '), width=wrap_w - 1) if next_text else []
    next_line = next_wrapped[0] if next_wrapped else ""

    scroll_hint = f" {C.DIM}↕ scroll{C.RESET}"
    hl = C.SUCCESS if manual_idx is not None else C.ACCENT
    pfx = "● " if manual_idx is not None else "▶ "

    # Clear only the right-pane rows, keeping the left-side UI intact.
    clear_rows = max(8, 2 + len(curr_wrapped) + 2)
    for i in range(clear_rows):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")

    out_row = row
    # Horizontal rule separator from credits
    rule = "─" * (wrap_w - 1)
    sys.stdout.write(f"\033[{out_row};{col}H{ui_utils.Colours.DIM}{rule}{ui_utils.Colours.RESET}")
    out_row += 1
    # Blank padding line before previous
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if prev_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {prev_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    # Blank line between previous and current
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1

    for i, seg in enumerate(curr_wrapped):
        if i == 0:
            sys.stdout.write(f"\033[{out_row};{col}H  {hl}{pfx}{seg}{C.RESET}{scroll_hint}")
        else:
            sys.stdout.write(f"\033[{out_row};{col}H    {hl}{seg}{C.RESET}")
        out_row += 1

    # Blank line between current and next
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if next_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {next_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    sys.stdout.flush()


def _parse_sylt(audio) -> list[tuple[str, int]]:
    """Parse SYLT (synced lyrics) from ID3 tags."""
    sylt_data = []
    for tag in audio.getall('SYLT'):
        sylt_data.extend(tag.text)
    sylt_data.sort(key=lambda x: x[1])
    return sylt_data


def _parse_uslt(audio) -> list[tuple[str, int]]:
    """Parse USLT (unsynced lyrics) from ID3 tags."""
    tags = audio.getall('USLT')
    if not tags:
        return []

    text = tags[0].text
    if isinstance(text, list):
        text = '\n'.join(text)

    text = normalise_lyric_newlines(text)
    return [(line.strip(), 0) for line in text.split('\n') if line.strip()]
