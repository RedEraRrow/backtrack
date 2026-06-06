"""
Lyric processing helpers for the playback engine.
Supports SYLT/USLT ID3 tags and markdown dialogue scripts.
"""
from __future__ import annotations
import bisect
import re
import textwrap
import sys
from pathlib import Path

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C


def normalize_lyric_newlines(text: str) -> str:
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
_EXPAND_CACHE_MAX = 10  # Max number of cached expansions


def expand_uslt_lines(
    lines: list[str],
    line_times: list[tuple],
    wrap_w: int,
    max_lines_per_chunk: int = 6,
) -> tuple[list[str], list[tuple]]:
    """Split long USLT lines into wrapped chunks and adjust their time windows."""
    global _expand_cache
    
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

    # Limit cache size to prevent unbounded growth
    if len(_expand_cache) >= _EXPAND_CACHE_MAX:
        oldest_key = next(iter(_expand_cache))
        del _expand_cache[oldest_key]
    
    _expand_cache[cache_key] = (exp_lines, exp_times)
    return exp_lines, exp_times


def build_uslt_line_times(lines: list, track_duration: float, words_per_second: float = 2.2) -> list[tuple[float, float]]:
    """Pre-calculate start/end times for each USLT line by word count."""
    times = []
    t = 0.0
    total_words = 0

    for line in lines:
        text = line
        if ':' in text:
            text = text.split(':', 1)[1]
        text = re.sub(r'\([^)]*\)', '', text)
        text = re.sub(r'\[[^\]]*\]', '', text)
        n = len(text.split())
        total_words += n

    words_per_second = track_duration / total_words if total_words > 0 else 20

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


# ============================================================================
# MARKDOWN DIALOGUE SUPPORT
# ============================================================================

class DialogueLine:
    """Represents a single dialogue line: speaker, stage directions, text."""
    
    def __init__(self, speaker: str = "", stage_dir: str = "", text: str = ""):
        self.speaker = speaker.strip()
        self.stage_dir = stage_dir.strip()
        self.text = text.strip()
    
    def is_stage_direction(self) -> bool:
        """True if this is a pure stage direction (no speaker, no dialogue)."""
        return not self.speaker and not self.text and self.stage_dir is not None
    
    def is_empty(self) -> bool:
        """True if completely empty."""
        return not self.speaker and not self.text and not self.stage_dir


def _parse_markdown_dialogue(text: str) -> list[DialogueLine]:
    """Parse markdown dialogue into speaker/stage/text tuples.
    
    Format:
      **SPEAKER** *(stage dir)*: dialogue text
      *(stage dir)*
      continued dialogue (inherits speaker)
      
    Returns list of DialogueLine objects.
    """
    lines_raw = normalize_lyric_newlines(text).split('\n')
    dialogue_lines = []
    current_speaker = ""
    
    for line in lines_raw:
        stripped = line.strip()
        
        # Skip empty lines
        if not stripped:
            if dialogue_lines and not dialogue_lines[-1].is_empty():
                dialogue_lines.append(DialogueLine())
            continue
        
        # Pure stage direction: *(text)*
        if stripped.startswith('*(') and stripped.endswith(')*'):
            stage = stripped[2:-2].strip()
            if current_speaker:
                # Append to last speaker's dialogue if it exists
                if dialogue_lines and dialogue_lines[-1].speaker == current_speaker:
                    if dialogue_lines[-1].text:
                        dialogue_lines[-1].text += f"\n{stage}"
                    else:
                        dialogue_lines.append(DialogueLine(speaker=current_speaker, text=stage))
                else:
                    dialogue_lines.append(DialogueLine(speaker=current_speaker, text=stage))
            else:
                dialogue_lines.append(DialogueLine(stage_dir=stage))
            continue
        
        # Speaker line: **SPEAKER** *(stage)?: dialogue
        speaker_match = re.match(r'\*\*([^*]+)\*\*\s*(.*)', stripped)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            rest = speaker_match.group(2).strip()
            current_speaker = speaker
            
            stage_dir = ""
            dialogue = ""
            
            # Extract stage direction if present: *(text)*: dialogue
            stage_match = re.match(r'\*\(([^)]*)\)\*:\s*(.*)', rest)
            if stage_match:
                stage_dir = stage_match.group(1).strip()
                dialogue = stage_match.group(2).strip()
            else:
                # Just dialogue, no stage direction
                dialogue = rest
                if dialogue.startswith(':'):
                    dialogue = dialogue[1:].strip()
            
            dialogue_lines.append(DialogueLine(speaker=speaker, stage_dir=stage_dir, text=dialogue))
            continue
        
        # Continuation line (inherits current speaker)
        if current_speaker:
            if dialogue_lines and dialogue_lines[-1].speaker == current_speaker and not dialogue_lines[-1].is_empty():
                dialogue_lines[-1].text += f"\n{stripped}"
            else:
                dialogue_lines.append(DialogueLine(speaker=current_speaker, text=stripped))
        else:
            # Orphaned line with no speaker context
            dialogue_lines.append(DialogueLine(text=stripped))
    
    return dialogue_lines


def _apply_markdown_formatting(text: str) -> str:
    """Convert markdown formatting to ANSI codes.
    
    Supports:
      **bold**
      *italic*
      [link text](url) -> link text (url shown)
    """
    # Bold: **text**
    text = re.sub(r'\*\*([^*]+)\*\*', f'{C.BOLD}\\1{C.RESET}', text)
    
    # Italic: *text* (but not *italic)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', f'{C.DIM}\\1{C.RESET}', text)
    
    # Links: [text](url) -> text (url)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', f'\\1 ({C.DIM}\\2{C.RESET})', text)
    
    return text


def parse_markdown_file(file_path: str) -> list[DialogueLine] | None:
    """Parse a markdown file as dialogue.
    
    Returns None if file doesn't exist, list of DialogueLine otherwise.
    """
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8')
        return _parse_markdown_dialogue(content)
    except Exception:
        return None


def build_dialogue_line_times(
    dialogue_lines: list[DialogueLine],
    words_per_second: float = 2.2
) -> list[tuple[float, float]]:
    """Calculate time windows for dialogue lines based on word count."""
    times = []
    t = 0.0
    
    for line in dialogue_lines:
        # Count words in dialogue text only
        word_count = max(1, len(line.text.split()))
        duration = word_count / words_per_second
        times.append((t, t + duration))
        t += duration
    
    return times


def find_current_dialogue_line(
    line_times: list[tuple[float, float]],
    elapsed: float
) -> int:
    """Find current dialogue line index."""
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def draw_dialogue_window(
    row: int,
    dialogue_lines: list[DialogueLine],
    current_idx: int,
    width: int | None = None,
    max_row: int | None = None,
    col: int | None = None,
    bottom_row: int | None = None,
    speaker_width: int | None = None
) -> None:
    """Display dialogue with speaker column on left, text on right.
    
    Uses actual pane geometry from playback_ui if available.
    Stage directions are dimmed. Formatting applied.
    """
    import src.playback.playback_ui as pui
    
    # Use actual rendered geometry if available
    if col is None:
        col = pui._last_right_left or 1
    if width is None:
        width = pui._last_right_width or ui_utils.get_terminal_width()
    
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    
    # Adaptive speaker width: use 1/3 of pane, but cap at 22 and min 10
    if speaker_width is None:
        speaker_width = max(10, min(width // 3, 22))
    
    # Speaker column on left, text flows on right
    text_col = speaker_width + 3  # speaker + " | "
    text_width = max(20, width - text_col - 2)
    
    clear_end = bottom_row if bottom_row is not None else max_row
    for i in range(max(0, clear_end - row + 1)):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")
    
    out_row = row
    
    # Divider
    rule = "─" * (width + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 1
    
    # Blank padding
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    
    # Previous speaker/line (context)
    if current_idx > 0:
        prev = dialogue_lines[current_idx - 1]
        prev_speaker = f"{C.DIM}{prev.speaker}{C.RESET}" if prev.speaker else ""
        prev_text = _apply_markdown_formatting(prev.text) if prev.text else ""
        prev_wrapped = textwrap.wrap(prev_text, width=text_width) if prev_text else []
        
        if prev_speaker or prev_wrapped:
            sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{prev_speaker:<{speaker_width}}{C.RESET} {C.DIM}│{C.RESET} ")
            if prev_wrapped:
                sys.stdout.write(f"{C.DIM}{prev_wrapped[0]}{C.RESET}")
            sys.stdout.write("\033[K")
            out_row += 1
            
            for line_text in prev_wrapped[1:]:
                sys.stdout.write(f"\033[{out_row};{col}H{' ' * speaker_width} {C.DIM}│{C.RESET} {C.DIM}{line_text}{C.RESET}\033[K")
                out_row += 1
    
    # Blank line
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    
    # Current line (highlighted)
    if 0 <= current_idx < len(dialogue_lines):
        curr = dialogue_lines[current_idx]
        
        # Speaker (bold, full width)
        speaker_display = curr.speaker if curr.speaker else "(stage)"
        if curr.stage_dir:
            speaker_display = f"{speaker_display} {C.DIM}({curr.stage_dir}){C.RESET}"
        
        sys.stdout.write(f"\033[{out_row};{col}H{C.BOLD}{speaker_display:<{speaker_width}}{C.RESET} {C.ACCENT}│{C.RESET} ")
        out_row += 1
        
        # Text lines
        curr_text = _apply_markdown_formatting(curr.text)
        curr_wrapped = textwrap.wrap(curr_text, width=text_width) or [""]
        
        for i, text_line in enumerate(curr_wrapped[:6]):  # Limit to 6 lines
            pfx = "▶ " if i == 0 else "  "
            sys.stdout.write(f"\033[{out_row};{text_col}H{C.BOLD}{pfx}{text_line}{C.RESET}\033[K")
            out_row += 1
    
    # Blank line
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    
    # Next speaker/line (context)
    if current_idx < len(dialogue_lines) - 1:
        nxt = dialogue_lines[current_idx + 1]
        nxt_speaker = f"{C.DIM}{nxt.speaker}{C.RESET}" if nxt.speaker else ""
        nxt_text = _apply_markdown_formatting(nxt.text) if nxt.text else ""
        nxt_wrapped = textwrap.wrap(nxt_text, width=text_width) if nxt_text else []
        
        if nxt_speaker or nxt_wrapped:
            sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{nxt_speaker:<{speaker_width}}{C.RESET} {C.DIM}│{C.RESET} ")
            if nxt_wrapped:
                sys.stdout.write(f"{C.DIM}{nxt_wrapped[0]}{C.RESET}")
            sys.stdout.write("\033[K")
            out_row += 1
            
            for line_text in nxt_wrapped[1:]:
                sys.stdout.write(f"\033[{out_row};{col}H{' ' * speaker_width} {C.DIM}│{C.RESET} {C.DIM}{line_text}{C.RESET}\033[K")
                out_row += 1
    
    sys.stdout.flush()


def draw_dialogue_initial(
    row: int,
    first_dialogue: DialogueLine,
    width: int | None = None,
    max_row: int | None = None,
    col: int | None = None,
    bottom_row: int | None = None,
    speaker_width: int | None = None
) -> None:
    """Draw dialogue region before playback: divider + first line greyed."""
    import src.playback.playback_ui as pui
    
    if col is None:
        col = pui._last_right_left or 1
    if width is None:
        width = pui._last_right_width or ui_utils.get_terminal_width()
    
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    
    # Adaptive speaker width
    if speaker_width is None:
        speaker_width = max(10, min(width // 3, 22))
    
    text_col = speaker_width + 3
    text_width = max(20, width - text_col - 2)
    
    clear_end = bottom_row if bottom_row is not None else max_row
    for i in range(max(0, clear_end - row + 1)):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")
    
    out_row = row
    rule = "─" * (width + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 1
    
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    
    # First line as preview (greyed)
    if first_dialogue.speaker:
        speaker_display = first_dialogue.speaker
        if first_dialogue.stage_dir:
            speaker_display = f"{speaker_display} ({first_dialogue.stage_dir})"
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{speaker_display:<{speaker_width}}{C.RESET} {C.DIM}│{C.RESET} ")
        out_row += 1
        
        text = _apply_markdown_formatting(first_dialogue.text)
        wrapped = textwrap.wrap(text, width=text_width) or [""]
        for i, line in enumerate(wrapped[:3]):
            if i > 0:
                sys.stdout.write(f"\033[{out_row};{col}H")
                out_row += 1
            sys.stdout.write(f"{C.DIM}  {line}{C.RESET}")
    
    sys.stdout.flush()


# ============================================================================
# SYLT/USLT SUPPORT (existing functions below)
# ============================================================================

def draw_lyric_window(row: int, sylt_data: list, current_idx: int,
                      width: int | None = None, max_row: int | None = None,
                      col: int = 1, bottom_row: int | None = None) -> None:
    """Display previous, current, and next lyrics for SYLT."""
    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    budget = max(4, max_row - row - 1)
    wrap_w = max(20, width - 8)

    p_raw = sylt_data[current_idx - 1][0] if current_idx > 0 else ""
    c_raw = sylt_data[current_idx][0] if 0 <= current_idx < len(sylt_data) else ""
    n_raw = sylt_data[current_idx + 1][0] if 0 <= current_idx < len(sylt_data) - 1 else ""

    p_flat = normalize_lyric_newlines(p_raw).replace('\n', ' ')
    p_wrapped = textwrap.wrap(p_flat, width=wrap_w - 1) if p_flat else []
    p_line = p_wrapped[-1] if p_wrapped else ""

    n_flat = normalize_lyric_newlines(n_raw).replace('\n', ' ')
    n_wrapped = textwrap.wrap(n_flat, width=wrap_w - 1) if n_flat else []
    n_line = n_wrapped[0] if n_wrapped else ""

    c_flat = normalize_lyric_newlines(c_raw).replace('\n', ' ')
    c_wrap = textwrap.wrap(c_flat, width=wrap_w - 4)[:max(1, budget - 4)]

    clear_end = bottom_row if bottom_row is not None else max_row
    for i in range(max(0, clear_end - row + 1)):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")

    out_row = row
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    rule = "─" * (width + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if p_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {p_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    for i, seg in enumerate(c_wrap or [""]):
        pfx = "▶ " if i == 0 else "  "
        if seg:
            sys.stdout.write(f"\033[{out_row};{col}H  {C.BOLD}{pfx}{seg}{C.RESET}")
        else:
            sys.stdout.write(f"\033[{out_row};{col}H\033[K")
        out_row += 1
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
                     col: int = 1, bottom_row: int | None = None) -> None:
    """Render prev/current/next USLT lines from an expanded line list."""
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

    prev_wrapped = textwrap.wrap(prev_text.replace('\n', ' '), width=wrap_w - 1) if prev_text else []
    prev_line = prev_wrapped[-1] if prev_wrapped else ""

    next_wrapped = textwrap.wrap(next_text.replace('\n', ' '), width=wrap_w - 1) if next_text else []
    next_line = next_wrapped[0] if next_wrapped else ""

    scroll_hint = f" {C.DIM}↕ scroll{C.RESET}"
    hl = C.SUCCESS if manual_idx is not None else C.ACCENT
    pfx = "● " if manual_idx is not None else "▶ "

    clear_end = bottom_row if bottom_row is not None else max_row
    for i in range(max(0, clear_end - row + 1)):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")

    out_row = row
    rule = "─" * (wrap_w - 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if prev_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {prev_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1

    for i, seg in enumerate(curr_wrapped):
        if i == 0:
            sys.stdout.write(f"\033[{out_row};{col}H  {hl}{pfx}{seg}{C.RESET}{scroll_hint}")
        else:
            sys.stdout.write(f"\033[{out_row};{col}H    {hl}{seg}{C.RESET}")
        out_row += 1

    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if next_line:
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {next_line}{C.RESET}")
    else:
        sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    sys.stdout.flush()


def draw_lyric_initial(row: int, first_line: str, width: int | None = None,
                       max_row: int | None = None, col: int = 1,
                       bottom_row: int | None = None) -> None:
    """Draw the lyric region before sync starts: divider + first line greyed out."""
    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    wrap_w = max(20, width - 8)

    clear_end = bottom_row if bottom_row is not None else max_row
    for i in range(max(0, clear_end - row + 1)):
        sys.stdout.write(f"\033[{row + i};{col}H\033[K")

    out_row = row
    rule = "─" * (width + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 1
    if first_line:
        flat = normalize_lyric_newlines(first_line).replace('\n', ' ')
        preview = textwrap.wrap(flat, width=wrap_w - 4)
        preview_line = preview[0] if preview else flat
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {preview_line}{C.RESET}")
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

    text = normalize_lyric_newlines(text)
    return [(line.strip(), 0) for line in text.split('\n') if line.strip()]


def estimate_sylt_last_line_end(sylt_data: list[tuple[str, int]],
                                duration_ms: float,
                                words_per_second: float = 2.2) -> float:
    """Estimate the end time (seconds) of the last SYLT line via word count."""
    if not sylt_data:
        return 0.0
    last_text, last_ts_ms = sylt_data[-1]
    last_ts = last_ts_ms / 1000.0
    word_count = max(1, len(last_text.split()))
    estimated_end = last_ts + word_count / words_per_second
    return min(estimated_end, duration_ms / 1000.0)


def find_uslt_handoff_index(uslt_lines: list[str], last_sylt_text: str) -> int:
    """Find the USLT line index that follows the last SYLT line.

    Tries exact match then normalized substring match. Returns the index
    after the matched line so USLT continues from where SYLT left off.
    Falls back to 0 if nothing matches.
    """
    def _norm(s: str) -> str:
        return re.sub(r'\s+', ' ', s.strip().lower())

    target = _norm(last_sylt_text)
    for i, line in enumerate(uslt_lines):
        if _norm(line) == target:
            return i + 1
    for i, line in enumerate(uslt_lines):
        n = _norm(line)
        if target in n or n in target:
            return i + 1
    return 0