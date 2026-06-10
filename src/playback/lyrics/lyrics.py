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
from typing import Any  # Imported Any to resolve Pylance type issues

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
_EXPAND_CACHE_MAX = 10


def expand_uslt_lines(
    lines: list[str],
    line_times: list[tuple],
    wrap_w: int,
    max_lines_per_chunk: int = 6,
) -> tuple[list[str], list[tuple]]:
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

    if len(_expand_cache) >= _EXPAND_CACHE_MAX:
        oldest_key = next(iter(_expand_cache))
        del _expand_cache[oldest_key]
    
    _expand_cache[cache_key] = (exp_lines, exp_times)
    return exp_lines, exp_times


def build_uslt_line_times(lines: list, track_duration: float, words_per_second: float = 2.2) -> list[tuple[float, float]]:
    times = []
    t = 0.0
    total_words = 0

    for line in lines:
        text = line.text if hasattr(line, 'text') else str(line)
        if ':' in text:
            text = text.split(':', 1)[1]
        text = re.sub(r'\([^)]*\)', '', text)
        text = re.sub(r'\[[^\]]*\]', '', text)
        n = len(text.split())
        total_words += n

    words_per_second = track_duration / total_words if total_words > 0 else 20

    for line in lines:
        text = line.text if hasattr(line, 'text') else str(line)
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
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


# ============================================================================
# MARKDOWN DIALOGUE & FORMATTING SUPPORT
# ============================================================================


def _parse_markdown_dialogue(text: str) -> list[DialogueLine]:
    lines_raw = normalize_lyric_newlines(text).split('\n')
    dialogue_lines = []
    current_speaker = ""
    
    for line in lines_raw:
        stripped = line.strip()
        if not stripped:
            if dialogue_lines and not dialogue_lines[-1].is_empty():
                dialogue_lines.append(DialogueLine())
            continue
        
        # 1. Normalize formatting just for structural boundary checks
        match_normalized = stripped.replace('**', '').replace('*', '').strip()
        
        # 2. Match a true standalone stage direction line
        if match_normalized.startswith('(') and match_normalized.endswith(')'):
            stage = match_normalized[1:-1].strip()
            # FORCE it to be a standalone stage direction element 
            # rather than merging it into the current speaker's dialogue bubble
            dialogue_lines.append(DialogueLine(stage_dir=stage))
            continue 
        
        # 3. Match character speaker blocks
        speaker_match = re.match(r'\*+([^*]+)\*+\s*(.*)', stripped)
        if speaker_match:
            speaker = speaker_match.group(1).strip()
            rest = speaker_match.group(2).strip()
            current_speaker = speaker
            
            stage_dir, dialogue = "", ""
            stage_match = re.match(r'\*\(([^)]*)\)\*:\s*(.*)', rest)
            if stage_match:
                stage_dir = stage_match.group(1).strip()
                dialogue = stage_match.group(2).strip()
            else:
                dialogue = rest
                if dialogue.startswith(':'):
                    dialogue = dialogue[1:].strip()
            
            dialogue_lines.append(DialogueLine(speaker=speaker, stage_dir=stage_dir, text=dialogue))
            continue
        
        if current_speaker:
            if dialogue_lines and dialogue_lines[-1].speaker == current_speaker and not dialogue_lines[-1].is_empty():
                dialogue_lines[-1].text += f"\n{stripped}"
            else:
                dialogue_lines.append(DialogueLine(speaker=current_speaker, text=stripped))
        else:
            dialogue_lines.append(DialogueLine(text=stripped))
    
    return dialogue_lines

def _apply_markdown_formatting(text: str) -> str:
    """Convert markdown formatting patterns into dynamic ANSI rendering escapes."""
    if not text:
        return ""
    text = re.sub(r'\*+([^*]+)\*+', f'{C.BOLD}\\1{C.RESET}', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', f'{C.DIM}\\1{C.RESET}', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', f'\\1 ({C.DIM}\\2{C.RESET})', text)
    return text


def parse_markdown_file(file_path: str) -> list[DialogueLine] | None:
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8')
        return _parse_markdown_dialogue(content)
    except Exception:
        return None


class DialogueLine:
    def __init__(self, speaker: str = "", stage_dir: str = "", text: str = ""):
        self.speaker = speaker.strip()
        self.stage_dir = stage_dir.strip()
        self.text = text.strip()
    
    def is_stage_direction(self) -> bool:
        return not self.speaker and not self.text and self.stage_dir is not None
    
    def is_empty(self) -> bool:
        return not self.speaker and not self.text and not self.stage_dir


def clean_text_for_timing(text: str) -> str:
    """Strips inline stage directions from a dialogue block to isolate spoken words."""
    cleaned = re.sub(r'\s*\*[^(]*\([^)]*\)\*\s*|\s*\([^)]*\)\s*', ' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


def expand_dialogue_into_sentences(
    dialogue_lines: list[DialogueLine], 
    words_per_second: float = 2.2,
    wrap_w: int = 50,
    max_sentence_chars: int = 110
) -> tuple[list[dict], list[tuple[float, float]]]:
    """
    Slices paragraph scripts precisely by sentence markers (.!?).
    Preserves all inline punctuation and formatting completely.
    """
    expanded_chunks: list[dict] = []
    time_windows: list[tuple[float, float]] = []
    total_elapsed = 0.0

    # Change this line inside expand_dialogue_into_sentences:
    sentence_end_re = re.compile(r'(?<!\bMr)(?<!\bDr)(?<!\bMs)(?<!\bMrs)(?<!\.\.)(?<=[.!?])\s+(?=[A-Z"\*])')

    for idx, line in enumerate(dialogue_lines):
        if line.is_empty():
            continue
            
        if not line.speaker and not line.text and line.stage_dir:
            line_duration = max(1.5, len(line.stage_dir.split()) / words_per_second)
            expanded_chunks.append({
                'parent_idx': idx,
                'is_first_of_line': True,
                'is_standalone_stage': True,
                'speaker': '',
                'stage_dir': line.stage_dir,
                'text': f"({line.stage_dir})"
            })
            time_windows.append((total_elapsed, total_elapsed + line_duration))
            total_elapsed += line_duration
            continue

        spoken_text_only = clean_text_for_timing(line.text)
        spoken_word_count = max(1, len(spoken_text_only.split()))
        line_duration = spoken_word_count / words_per_second
        
        if line.text.strip():
            raw_sentences = [s.strip() for s in sentence_end_re.split(line.text) if s.strip()]
        else:
            raw_sentences = []

        if not raw_sentences:
            raw_sentences = [line.text.strip()] if line.text.strip() else ["..."]

        processed_sentences: list[str] = []
        for sentence in raw_sentences:
            if len(sentence) > max_sentence_chars and ',' in sentence:
                comma_indices = [m.start() for m in re.finditer(r',', sentence)]
                halfway = len(sentence) / 2.0
                if comma_indices:
                    best_comma_idx = min(comma_indices, key=lambda i: abs(i - halfway))
                    part1 = sentence[:best_comma_idx + 1].strip()
                    part2 = sentence[best_comma_idx + 1:].strip()
                    if part1 and part2:
                        processed_sentences.append(part1)
                        processed_sentences.append(part2)
                    else:
                        processed_sentences.append(sentence)
                else:
                    processed_sentences.append(sentence)
            else:
                processed_sentences.append(sentence)
        
        total_chunks_words = sum(max(1, len(clean_text_for_timing(s).split())) for s in processed_sentences)
        current_time = total_elapsed
        
        for s_idx, s in enumerate(processed_sentences):
            wc = max(1, len(clean_text_for_timing(s).split()))
            chunk_duration = line_duration * (wc / total_chunks_words) if total_chunks_words > 0 else line_duration
            
            expanded_chunks.append({
                'parent_idx': idx,
                'is_first_of_line': (s_idx == 0),
                'is_standalone_stage': False,
                'speaker': line.speaker,
                'stage_dir': line.stage_dir,
                'text': s
            })
            time_windows.append((current_time, current_time + chunk_duration))
            current_time += chunk_duration
            
        total_elapsed += line_duration

    return expanded_chunks, time_windows


def draw_dialogue_window(
    row: int,
    dialogue_lines: list[dict],
    current_idx: int,
    width: int,
    max_row: int,
    col: int = 1,
    bottom_row: int | None = None
) -> None:
    """
    Renders a clean 3-line scroll view layout (Previous, Current, Next).
    Current line gets speaker+stage_dir in left column (with wrapping if needed).
    Previous/next lines get speaker name only.
    """
    if not dialogue_lines or not (0 <= current_idx < len(dialogue_lines)):
        return

    speaker_width = max(12, min(width // 3, 26))
    text_col = col + speaker_width + 3
    text_width = max(20, width - speaker_width - 5)
    clear_limit = bottom_row if bottom_row is not None else max_row

    # Clear the entire region
    for i in range(row, clear_limit + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{'─' * width}{C.RESET}")
    out_row += 2

    # Collect window items: prev (if exists), current, next (if exists)
    window_items = []
    if current_idx - 1 >= 0:
        window_items.append((dialogue_lines[current_idx - 1], False))
    window_items.append((dialogue_lines[current_idx], True))
    if current_idx + 1 < len(dialogue_lines):
        window_items.append((dialogue_lines[current_idx + 1], False))

    current_speaker = dialogue_lines[current_idx].get('speaker', '').strip()

    for chunk, is_active in window_items:
        speaker = chunk.get('speaker', '').strip()
        stage_dir = chunk.get('stage_dir', '').strip()
        text = chunk.get('text', '').strip()

        # Build left column (speaker + optional stage direction)
        left_lines = []
        if speaker:
            if is_active and stage_dir:
                # Current line: try to fit speaker + stage on one line
                stage_text = f"({stage_dir})"
                combined = f"{speaker} {stage_text}"
                
                if len(combined) <= speaker_width:
                    # Fits on one line
                    left_lines.append(f"{C.BOLD}{speaker}{C.RESET} {C.DIM}{stage_text}{C.RESET}")
                else:
                    # Doesn't fit: speaker on first line, stage wraps below
                    left_lines.append(f"{C.BOLD}{speaker}{C.RESET}")
                    for stage_line in textwrap.wrap(stage_text, width=speaker_width):
                        left_lines.append(f"{C.DIM}{stage_line}{C.RESET}")
            elif is_active:
                # Current line, no stage direction
                left_lines.append(f"{C.BOLD}{speaker}{C.RESET}")
            else:
                # Previous/next: only show speaker if different from current
                if speaker != current_speaker:
                    left_lines.append(f"{C.DIM}{speaker}{C.RESET}")

        # Build right column (dialogue text)
        right_lines = []
        if text:
            if is_active:
                formatted_text = _apply_markdown_formatting(text)
                right_lines = textwrap.wrap(formatted_text, width=text_width) or [""]
            else:
                plain_text = _strip_markdown(text)
                right_lines = [f"{C.DIM}{line}{C.RESET}" for line in textwrap.wrap(plain_text, width=text_width)] or [""]

        # Calculate total rows needed for this block
        max_rows = max(len(left_lines), len(right_lines)) if (left_lines or right_lines) else 1
        if out_row + max_rows + 2 > clear_limit:
            break

        # Draw the block
        for i in range(max_rows):
            # Left column
            if i < len(left_lines):
                clean_len = len(re.sub(r'\033\[[0-9;]*m', '', left_lines[i]))
                padding = " " * (speaker_width - clean_len)
                left_cell = f"{left_lines[i]}{padding}"
            else:
                left_cell = " " * speaker_width

            # Right column
            if i < len(right_lines):
                right_cell = f"{right_lines[i]}"
            else:
                right_cell = ""

            # Draw the line
            if right_cell:
                sys.stdout.write(f"\033[{out_row};{col}H{left_cell} {C.DIM}│{C.RESET} {right_cell}")
            else:
                sys.stdout.write(f"\033[{out_row};{col}H{left_cell} {C.DIM}│{C.RESET}")
            out_row += 1

        # Padding between blocks
        out_row += 2

    sys.stdout.flush()


def find_current_dialogue_line(line_times: list[tuple[float, float]], elapsed: float) -> int:
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def _strip_markdown(text: str) -> str:
    """Removes basic inline markdown formatting like asterisks and underscores."""
    return re.sub(r'[*_`~]', '', text)

# Fixed type-hint signature from lowercase 'any' to capitalized 'Any' to resolve Pylance Issues
def draw_dialogue_initial(
    row: int,
    first_dialogue: Any,
    width: int | None = None,
    max_row: int | None = None,
    col: int | None = None,
    bottom_row: int | None = None,
    speaker_width: int | None = None
) -> None:
    import src.playback.playback_ui as pui
    if col is None: col = pui._last_right_left or 1
    if width is None: width = pui._last_right_width or ui_utils.get_terminal_width()
    
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    
    if speaker_width is None:
        speaker_width = max(10, min(width // 3, 22))
    
    text_width = max(20, width - speaker_width - 5)
    
    clear_end = bottom_row if bottom_row is not None else (row + max_row)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")
    
    out_row = row
    rule = "─" * (width - 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 1
    
    sys.stdout.write(f"\033[{out_row};{col}H\033[K")
    out_row += 2
    
    is_dict = isinstance(first_dialogue, dict)
    speaker = first_dialogue.get('speaker', '') if is_dict else getattr(first_dialogue, 'speaker', '')
    stage_dir = first_dialogue.get('stage_dir', '') if is_dict else getattr(first_dialogue, 'stage_dir', '')
    text_content = first_dialogue.get('text', '') if is_dict else getattr(first_dialogue, 'text', '')
    is_standalone = first_dialogue.get('is_standalone_stage', False) if is_dict else (not speaker and not text_content and stage_dir)

    if is_standalone:
        raw_text = f"({stage_dir})" if stage_dir else text_content
        wrapped = textwrap.wrap(raw_text, width=text_width) or [""]
        for line in wrapped[:3]:
            sys.stdout.write(f"\033[{out_row};{col + speaker_width + 3}H{C.DIM}  {line}{C.RESET}")
            out_row += 1
    elif speaker:
        speaker_display = speaker
        if stage_dir:
            speaker_display = f"{speaker_display} ({stage_dir})"
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{speaker_display:<{speaker_width}}{C.RESET} {C.DIM}│{C.RESET} ")
        out_row += 1
        
        formatted_text = _apply_markdown_formatting(text_content)
        wrapped = textwrap.wrap(formatted_text, width=text_width) or [""]
        for line in wrapped[:3]:
            sys.stdout.write(f"\033[{out_row};{col + speaker_width + 3}H{C.DIM}  {line}{C.RESET}")
            out_row += 1
    else:
        formatted_text = _apply_markdown_formatting(text_content)
        wrapped = textwrap.wrap(formatted_text, width=text_width) or [""]
        for line in wrapped[:3]:
            sys.stdout.write(f"\033[{out_row};{col + speaker_width + 3}H{C.DIM}  {line}{C.RESET}")
            out_row += 1
            
    sys.stdout.flush()


# ============================================================================
# SYLT/USLT CLEAN FORMATTING UPGRADE
# ============================================================================

def draw_lyric_window(row: int, sylt_data: list, current_idx: int,
                      width: int | None = None, max_row: int | None = None,
                      col: int = 1, bottom_row: int | None = None) -> None:
    import src.playback.playback_ui as pui
    if pui._layout_mode(ui_utils.get_terminal_width()) == 'wide':
        col = pui._last_right_left or 1
        width = pui._last_right_width or width

    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    budget = max(4, max_row - row - 1)
    wrap_w = max(20, width - 8)

    p_raw = sylt_data[current_idx - 1][0] if current_idx > 0 else ""
    c_raw = sylt_data[current_idx][0] if 0 <= current_idx < len(sylt_data) else ""
    n_raw = sylt_data[current_idx + 1][0] if 0 <= current_idx < len(sylt_data) - 1 else ""

    p_flat = _apply_markdown_formatting(normalize_lyric_newlines(p_raw).replace('\n', ' '))
    p_wrapped = textwrap.wrap(p_flat, width=wrap_w - 1) if p_flat else []
    p_line = p_wrapped[-1] if p_wrapped else ""

    n_flat = _apply_markdown_formatting(normalize_lyric_newlines(n_raw).replace('\n', ' '))
    n_wrapped = textwrap.wrap(n_flat, width=wrap_w - 1) if n_flat else []
    n_line = n_wrapped[0] if n_wrapped else ""

    c_flat = _apply_markdown_formatting(normalize_lyric_newlines(c_raw).replace('\n', ' '))
    c_wrap = textwrap.wrap(c_flat, width=wrap_w - 4)[:max(1, budget - 4)]

    clear_end = bottom_row if bottom_row is not None else (row + budget)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * (width - 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 2
    
    if p_line: sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {p_line}{C.RESET}")
    out_row += 2
    
    for i, seg in enumerate(c_wrap or [""]):
        pfx = "▶ " if i == 0 else "  "
        if seg: sys.stdout.write(f"\033[{out_row};{col}H  {C.BOLD}{pfx}{seg}{C.RESET}")
        out_row += 1
        
    out_row += 1
    if n_line: sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {n_line}{C.RESET}")
    sys.stdout.flush()


def draw_uslt_window(row: int, all_lines: list, line_times: list,
                     elapsed: float, width: int | None = None,
                     manual_idx: int | None = None,
                     max_row: int | None = None,
                     col: int = 1, bottom_row: int | None = None) -> None:
    import src.playback.playback_ui as pui
    if pui._layout_mode(ui_utils.get_terminal_width()) == 'wide':
        col = pui._last_right_left or 1
        width = pui._last_right_width or width

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
    
    prev_flat = _apply_markdown_formatting(prev_text.replace('\n', ' '))
    curr_flat = _apply_markdown_formatting(curr_text.replace('\n', ' '))
    next_flat = _apply_markdown_formatting(next_text.replace('\n', ' '))

    curr_wrapped = textwrap.wrap(curr_flat, width=wrap_w - 4) or ['']
    curr_wrapped = curr_wrapped[:max(1, budget - 4)]

    prev_wrapped = textwrap.wrap(prev_flat, width=wrap_w - 1) if prev_flat else []
    prev_line = prev_wrapped[-1] if prev_wrapped else ""

    next_wrapped = textwrap.wrap(next_flat, width=wrap_w - 1) if next_flat else []
    next_line = next_wrapped[0] if next_wrapped else ""

    scroll_hint = f" {C.DIM}↕ scroll{C.RESET}"
    hl = C.SUCCESS if manual_idx is not None else C.ACCENT
    pfx = "● " if manual_idx is not None else "▶ "

    clear_end = bottom_row if bottom_row is not None else (row + budget)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * (width - 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 2
    
    if prev_line: sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {prev_line}{C.RESET}")
    out_row += 2

    for i, seg in enumerate(curr_wrapped):
        if i == 0:
            sys.stdout.write(f"\033[{out_row};{col}H  {hl}{pfx}{seg}{C.RESET}{scroll_hint}")
        else:
            sys.stdout.write(f"\033[{out_row};{col}H    {hl}{seg}{C.RESET}")
        out_row += 1

    out_row += 1
    if next_line: sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {next_line}{C.RESET}")
    sys.stdout.flush()


# Fixed type-hint signature from lowercase 'any' to capitalized 'Any' to resolve Pylance Issues
def draw_lyric_initial(row: int, first_line: Any, width: int | None = None,
                       max_row: int | None = None, col: int = 1,
                       bottom_row: int | None = None) -> None:
    import src.playback.playback_ui as pui
    if pui._layout_mode(ui_utils.get_terminal_width()) == 'wide':
        col = pui._last_right_left or 1
        width = pui._last_right_width or width

    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    wrap_w = max(20, width - 8)

    clear_end = bottom_row if bottom_row is not None else (row + max_row)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * (width - 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 3
    
    if first_line:
        if isinstance(first_line, dict):
            raw_str = first_line.get('text', '')
        elif hasattr(first_line, 'text'):
            raw_str = first_line.text
        else:
            raw_str = str(first_line)
            
        flat = _apply_markdown_formatting(normalize_lyric_newlines(raw_str).replace('\n', ' '))
        preview = textwrap.wrap(flat, width=wrap_w - 4)
        preview_line = preview[0] if preview else flat
        sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {preview_line}{C.RESET}")
    sys.stdout.flush()


def _parse_sylt(audio) -> list[tuple[str, int]]:
    sylt_data = []
    for tag in audio.getall('SYLT'):
        sylt_data.extend(tag.text)
    sylt_data.sort(key=lambda x: x[1])
    return sylt_data


def _parse_uslt(audio) -> list[tuple[str, int]]:
    tags = audio.getall('USLT')
    if not tags:
        return []
    text = tags[0].text
    if isinstance(text, list):
        text = '\n'.join(text)
    text = normalize_lyric_newlines(text)
    return [(line.strip(), 0) for line in text.split('\n') if line.strip()]


def estimate_sylt_last_line_end(sylt_data: list[tuple[str, int]], duration_ms: float, words_per_second: float = 2.2) -> float:
    if not sylt_data: return 0.0
    last_text, last_ts_ms = sylt_data[-1]
    last_ts = last_ts_ms / 1000.0
    word_count = max(1, len(last_text.split()))
    estimated_end = last_ts + word_count / words_per_second
    return min(estimated_end, duration_ms / 1000.0)


def find_uslt_handoff_index(uslt_lines: list[str], last_sylt_text: str) -> int:
    def _norm(s: str) -> str:
        return re.sub(r'\s+', ' ', s.strip().lower())
    target = _norm(last_sylt_text)
    for i, line in enumerate(uslt_lines):
        if _norm(line) == target: return i + 1
    for i, line in enumerate(uslt_lines):
        n = _norm(line)
        if target in n or n in target: return i + 1
    return 0

def _find_markdown_for_audio(audio_path: str) -> str | None:
    """Find .md file next to audio file.
    
    Tries (in order):
      1. {basename}.md
      2. {basename}.dialogue.md
    """
    base = Path(audio_path).stem
    parent = Path(audio_path).parent
    
    for pattern in [f"{base}.md", f"{base}.dialogue.md"]:
        candidate = parent / pattern
        if candidate.exists():
            return str(candidate)
    
    return None


class DialoguePlaybackState:
    """Manages sentence-by-sentence narrative dialogue states cleanly."""
    
    def __init__(self, audio_path: str):
        md_path = _find_markdown_for_audio(audio_path)
        raw_dialogue_lines = None
        self.expanded_chunks: list[dict] = []
        self.line_times: list[tuple[float, float]] = []
        self.current_idx = 0
        self.load_error = None
        
        if md_path:
            try:
                raw_dialogue_lines = parse_markdown_file(md_path)
                if raw_dialogue_lines is None:
                    self.load_error = f"Failed to parse markdown: {md_path}"
            except Exception as e:
                self.load_error = f"Error reading markdown: {str(e)}"
        
        if raw_dialogue_lines:
            # Breaks blocks into individual sentence components automatically
            self.expanded_chunks, self.line_times = expand_dialogue_into_sentences(
                raw_dialogue_lines, words_per_second=2.2, wrap_w=50
            )
            
    def is_active(self) -> bool:
        return bool(self.expanded_chunks)
        
    def update(self, elapsed: float) -> None:
        if self.line_times:
            self.current_idx = find_current_dialogue_line(self.line_times, elapsed)
