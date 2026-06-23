from __future__ import annotations
import bisect
import json
import re
import textwrap
import sys
from pathlib import Path

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C

def normalize_lyric_newlines(text: str) -> str:
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


def _sub_split_sentence(text: str, max_w: int, max_lines: int = 2) -> list[str]:
    """Recursively split a clause that wraps beyond max_lines at the most natural
    midpoint: comma, semicolon, colon, dash, then conjunctions, then word boundary."""
    if len(textwrap.wrap(text, width=max_w)) <= max_lines:
        return [text]

    mid = len(text) // 2

    # After a comma / semicolon / colon / em-dash / en-dash followed by whitespace
    punct_candidates = [m.end() for m in re.finditer(r'[,;:—–]\s', text)]
    # Before common conjunctions (split just before the conjunction word)
    conj_candidates  = [m.start() for m in re.finditer(
        r'\s(?=\b(?:and|but|or|so|yet|because|although|though|while|whereas)\b)',
        text, re.IGNORECASE,
    )]
    candidates = punct_candidates + conj_candidates

    if candidates:
        best = min(candidates, key=lambda c: abs(c - mid))
        left, right = text[:best].rstrip(), text[best:].lstrip()
    else:
        # Hard fall-through: split at word boundary nearest midpoint
        words = text.split()
        mid_w = max(1, len(words) // 2)
        left, right = ' '.join(words[:mid_w]), ' '.join(words[mid_w:])

    return _sub_split_sentence(left, max_w, max_lines) + _sub_split_sentence(right, max_w, max_lines)


_expand_cache: dict = {}
_EXPAND_CACHE_MAX = 10


def expand_uslt_lines(
    lines: list[str],
    line_times: list[tuple],
    wrap_w: int,
    max_lines_per_chunk: int = 6,
) -> tuple[list[str], list[tuple]]:
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


def build_uslt_line_times(lines: list, track_duration: float) -> list[tuple[float, float]]:
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

    words_per_second = track_duration / total_words if total_words > 0 else 2.2

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


def _parse_markdown_dialogue(text: str) -> list[DialogueLine]:
    lines = normalize_lyric_newlines(text).split('\n')
    dialogue_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for stage-only lines
        stage_match = re.match(r'^\*+\(([^)]*)\)\*+$|^\(([^)]*)\)$', stripped)
        if stage_match:
            dialogue_lines.append(DialogueLine(stage_dir=(stage_match.group(1) or stage_match.group(2)).strip()))
            continue

        # Split at first colon only
        if ':' in stripped:
            header, dialogue = stripped.split(':', 1)

            # Extract stage dir from header (e.g., "**A** (sigh): text")
            stage_dir = ""
            stage_match = re.search(r'\(([^)]+)\)', header)
            if stage_match:
                stage_dir = stage_match.group(1).strip()
                header = header.replace(stage_match.group(0), "")

            # Extract all **Name** bolded blocks
            speakers = re.findall(r'\*\*([^*]+)\*\*', header)

            dialogue_lines.append(DialogueLine(
                speakers=[s.strip() for s in speakers],
                stage_dir=stage_dir,
                text=dialogue.strip()
            ))
        else:
            # Append to last line if it doesn't look like a header
            if dialogue_lines:
                dialogue_lines[-1].text += f" {stripped}"
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


def _format_speaker_list(items: list[str]) -> str:
    if not items:
        return ""
    items = [item.strip() for item in items]
    if len(items) == 1:
        return f"**{items[0]}**"
    return ", ".join(f"**{item}**" for item in items[:-1]) + " and " + f"**{items[-1]}**"


class DialogueLine:
    def __init__(self, speakers: list[str] | None = None, stage_dir: str = "", text: str = "", start: float = 0.0, end: float = 0.0):
        self.speakers = speakers or []
        self.stage_dir = stage_dir.strip()
        self.text = text.strip()
        self.start = start
        self.end = end

    @property
    def speaker(self) -> str:
        """Compatibility property for existing rendering logic."""
        return _format_speaker_list(self.speakers)

    def is_stage_direction(self) -> bool:
        return not self.speakers and not self.text and bool(self.stage_dir)

    def is_empty(self) -> bool:
        return not self.speakers and not self.text and not self.stage_dir


def clean_text_for_timing(text: str) -> str:
    cleaned = re.sub(r'\s*\*[^(]*\([^)]*\)\*\s*|\s*\([^)]*\)\s*', ' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


_AIR_THRESHOLD = 2.0  # seconds of silence that warrants a blank-line gap indicator


def expand_dialogue_into_sentences(
    dialogue_lines: list[DialogueLine],
    words_per_second: float,
    word_timings: list[dict] | None = None,
    wrap_w: int = 50,
) -> tuple[list[dict], list[tuple[float, float]]]:
    expanded_chunks: list[dict] = []
    time_windows: list[tuple[float, float]] = []
    total_elapsed = 0.0

    sentence_end_re = re.compile(r'(?<!\bMr)(?<!\bDr)(?<!\bMs)(?<!\bMrs)(?<!\.\.)(?<=[.!?])\s+(?=[A-Z"\*])')

    # Monotonic cursor: each sentence scans forward from where the last left off.
    wt_cursor = 0

    def _norm(raw: str) -> str:
        """Normalize a word for comparison: strip whitespace (Whisper leading space) and punctuation."""
        return raw.lower().strip().strip(".,!?;:\"'")

    def get_sentence_timing(sentence_words: list[str]) -> tuple[float, float] | None:
        nonlocal wt_cursor
        if not word_timings or not sentence_words:
            return None

        start_time: float | None = None
        end_time: float | None = None
        local_cursor = wt_cursor

        for word in sentence_words:
            norm = _norm(word)
            if not norm:
                continue
            # Search forward from local_cursor within a bounded window
            for i in range(local_cursor, min(local_cursor + 80, len(word_timings))):
                if _norm(word_timings[i].get('word', '')) == norm:
                    if start_time is None:
                        start_time = float(word_timings[i]['start'])
                    end_time = float(word_timings[i]['end'])
                    local_cursor = i + 1
                    break

        if start_time is not None and end_time is not None:
            wt_cursor = local_cursor
            return (start_time, end_time)
        return None

    for idx, line in enumerate(dialogue_lines):
        if line.is_empty():
            continue

        # Stage-direction-only lines have no spoken words — give them a time
        # window from the current position to the next word timing start (i.e.
        # they fill the gap), and mark them so the renderer can place them in
        # the text column without a speaker label.
        if line.is_stage_direction():
            gap_start = total_elapsed
            if word_timings and wt_cursor < len(word_timings):
                gap_end = max(gap_start + 0.5, float(word_timings[wt_cursor]['start']))
            else:
                gap_end = gap_start + 0.5
            expanded_chunks.append({
                'parent_idx': idx, 'speaker': '',
                'stage_dir': line.stage_dir, 'text': '',
                'is_stage': True,
            })
            time_windows.append((gap_start, gap_end))
            # Don't advance total_elapsed — stage directions don't consume time.
            continue

        spoken_text_only = clean_text_for_timing(line.text)
        spoken_word_count = max(1, len(spoken_text_only.split()))

        raw_sentences = [s.strip() for s in sentence_end_re.split(line.text) if s.strip()]
        if not raw_sentences:
            raw_sentences = [line.text.strip()] if line.text.strip() else []

        if not raw_sentences:
            continue

        line_duration = (line.end - line.start) if (line.start > 0 or line.end > 0) else (spoken_word_count / words_per_second)

        for s in raw_sentences:
            # Further split any clause that still wraps too wide at natural breaks.
            sub_sentences = _sub_split_sentence(s, wrap_w)
            for sub_s in sub_sentences:
                sentence_words = re.findall(r"[\w']+", clean_text_for_timing(sub_s))
                exact = get_sentence_timing(sentence_words)
                if exact:
                    chunk_start, chunk_end = exact
                else:
                    wc = max(1, len(clean_text_for_timing(sub_s).split()))
                    chunk_dur = line_duration * (wc / spoken_word_count)
                    chunk_start, chunk_end = total_elapsed, total_elapsed + chunk_dur

                expanded_chunks.append({
                    'parent_idx': idx, 'speaker': line.speaker,
                    'stage_dir': line.stage_dir, 'text': sub_s,
                    'is_stage': False, 'is_air': False,
                })
                time_windows.append((chunk_start, chunk_end))
                total_elapsed = chunk_end

    # Pre-pass: evenly divide consecutive is_stage chunks within their gap.
    # All stage directions in a group are given the same (gap_start, next_word_start)
    # window because total_elapsed isn't advanced for them. Fix that here so
    # find_current_dialogue_line can step through each one in sequence.
    i = 0
    while i < len(expanded_chunks):
        if not expanded_chunks[i].get('is_stage'):
            i += 1
            continue
        group_start = i
        while i < len(expanded_chunks) and expanded_chunks[i].get('is_stage'):
            i += 1
        group_end = i
        n = group_end - group_start

        time_before = time_windows[group_start - 1][1] if group_start > 0 else 0.0
        time_after  = time_windows[group_start][1]   # all share the same end
        total_gap   = time_after - time_before
        if total_gap > 0:
            slot = total_gap / n
        else:
            slot = 0.5  # minimum if no measurable gap
        for j in range(n):
            time_windows[group_start + j] = (time_before + j * slot, time_before + (j + 1) * slot)

    # Post-pass: insert is_air chunks for silence gaps not covered by stage directions.
    final_chunks: list[dict] = []
    final_times: list[tuple[float, float]] = []
    for chunk, twin in zip(expanded_chunks, time_windows):
        if final_times:
            gap = twin[0] - final_times[-1][1]
            if gap > _AIR_THRESHOLD:
                final_chunks.append({
                    'parent_idx': -1, 'speaker': '', 'stage_dir': '',
                    'text': '', 'is_stage': False, 'is_air': True,
                })
                final_times.append((final_times[-1][1], twin[0]))
        final_chunks.append(chunk)
        final_times.append(twin)

    return final_chunks, final_times


def draw_dialogue_window(
    row: int,
    dialogue_lines: list[dict],
    current_idx: int,
    width: int,
    max_row: int,
    col: int = 1,
    bottom_row: int | None = None,
    line_times: list[tuple[float, float]] | None = None,
) -> None:
    """Renders the three-line context window (prev, current, next) with non-current lines dimmed."""
    if not dialogue_lines or not (0 <= current_idx < len(dialogue_lines)):
        return

    padded_width = width - 2
    speaker_width = max(12, min(padded_width // 3, 26))
    text_width = max(20, padded_width - speaker_width - 5)
    clear_limit = bottom_row if bottom_row is not None else max_row

    # Clear the entire dialogue area
    for i in range(row, clear_limit + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{'─' * width}{C.RESET}")
    out_row += 2

    # Build window with global indices: (dl_idx, chunk, is_active)
    window_items: list[tuple[int, dict, bool]] = []
    if current_idx - 1 >= 0:
        window_items.append((current_idx - 1, dialogue_lines[current_idx - 1], False))
    window_items.append((current_idx, dialogue_lines[current_idx], True))
    if current_idx + 1 < len(dialogue_lines):
        window_items.append((current_idx + 1, dialogue_lines[current_idx + 1], False))

    current_speaker = dialogue_lines[current_idx].get('speaker', '').strip()

    for win_pos, (dl_idx, chunk, is_active) in enumerate(window_items):
        speaker   = chunk.get('speaker', '').strip()
        stage_dir = chunk.get('stage_dir', '').strip()
        text      = chunk.get('text', '').strip()
        is_stage  = chunk.get('is_stage', False)
        is_air    = chunk.get('is_air', False)

        # Only fired for gaps that weren't covered by an is_air chunk (edge
        # cases). Suppressed when either neighbour is already an air/stage chunk.
        show_air = False
        if win_pos > 0 and line_times and not is_air and not is_stage:
            prev_chunk = window_items[win_pos - 1][1]
            if not prev_chunk.get('is_air') and not prev_chunk.get('is_stage'):
                prev_dl_idx = window_items[win_pos - 1][0]
                if prev_dl_idx < len(line_times) and dl_idx < len(line_times):
                    air = line_times[dl_idx][0] - line_times[prev_dl_idx][1]
                    show_air = air > _AIR_THRESHOLD

        left_lines: list[str] = []
        right_lines: list[str] = []

        if is_air:
            right_lines = [""]
        elif is_stage:
            label = f"({stage_dir})" if stage_dir else text
            if is_active:
                right_lines = textwrap.wrap(_apply_markdown_formatting(label), width=text_width) or [label]
            else:
                right_lines = [f"{C.DIM}{s}{C.RESET}" for s in (textwrap.wrap(label, width=text_width) or [label])]
        else:
            if is_active:
                show_speaker = bool(speaker)
            else:
                show_speaker = bool(speaker) and (speaker != current_speaker)

            if show_speaker:
                raw_spk = _strip_markdown(speaker)
                wrapped_spk = textwrap.wrap(raw_spk, width=speaker_width)
                if is_active:
                    left_lines.extend(f"{C.BOLD}{s}{C.RESET}" for s in wrapped_spk)
                    if stage_dir:
                        left_lines.extend(f"{C.DIM}{s}{C.RESET}" for s in textwrap.wrap(f"({stage_dir})", width=speaker_width))
                else:
                    left_lines.extend(f"{C.DIM}{s}{C.RESET}" for s in wrapped_spk)
                    if stage_dir:
                        left_lines.extend(f"{C.DIM}{s}{C.RESET}" for s in textwrap.wrap(f"({stage_dir})", width=speaker_width))

            if text:
                if is_active:
                    right_lines = textwrap.wrap(_apply_markdown_formatting(text), width=text_width) or [""]
                else:
                    right_lines = [f"{C.DIM}{s}{C.RESET}" for s in (textwrap.wrap(_strip_markdown(text), width=text_width) or [""])]

        max_rows = max(len(left_lines), len(right_lines)) if (left_lines or right_lines) else 1
        air_rows = 2 if show_air else 0  # blank row + indicator row

        if out_row + air_rows + max_rows + 2 > clear_limit:
            if show_air and out_row + max_rows + 2 <= clear_limit:
                show_air = False
                air_rows = 0
            else:
                break

        if show_air:
            out_row += 1
            sys.stdout.write(f"\033[{out_row};{col + speaker_width + 3}H{C.DIM}⋯{C.RESET}")
            out_row += 1

        for i in range(max_rows):
            if i < len(left_lines):
                clean_len = len(re.sub(r'\033\[[0-9;]*m', '', left_lines[i]))
                left_cell = f"{left_lines[i]}{' ' * (speaker_width - clean_len)}"
            else:
                left_cell = " " * speaker_width

            right_cell = right_lines[i] if i < len(right_lines) else ""

            if is_air:
                pass
            elif right_cell:
                sys.stdout.write(f"\033[{out_row};{col + 1}H{left_cell} {C.DIM}│{C.RESET} {right_cell}")
            else:
                sys.stdout.write(f"\033[{out_row};{col + 1}H{left_cell} {C.DIM}│{C.RESET}")
            out_row += 1

        out_row += 2

    sys.stdout.flush()


def find_current_dialogue_line(line_times: list[tuple[float, float]], elapsed: float) -> int:
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def _strip_markdown(text: str) -> str:
    return re.sub(r'[*_`~]', '', text)


def draw_lyric_window(row: int, sylt_data: list, current_idx: int,
                      width: int | None = None, max_row: int | None = None,
                      col: int = 1, bottom_row: int | None = None) -> None:
    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    budget = max(4, max_row - row - 1)
    wrap_w = max(20, width - 10)

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
    c_wrapped = textwrap.wrap(c_flat, width=wrap_w - 4)[:max(1, budget - 4)]

    clear_end = bottom_row if bottom_row is not None else (row + budget)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * width
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 2

    if p_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {p_line}{C.RESET}")
    out_row += 2

    for i, seg in enumerate(c_wrapped or [""]):
        pfx = "▶ " if i == 0 else "  "
        if seg: sys.stdout.write(f"\033[{out_row};{col + 1}H  {C.BOLD}{pfx}{seg}{C.RESET}")
        out_row += 1

    out_row += 1
    if n_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {n_line}{C.RESET}")
    sys.stdout.flush()


def draw_uslt_window(row: int, all_lines: list, line_times: list,
                     elapsed: float, width: int | None = None,
                     manual_idx: int | None = None,
                     max_row: int | None = None,
                     col: int = 1, bottom_row: int | None = None) -> None:
    width = width or ui_utils.get_terminal_width()
    wrap_w = max(20, width - 10)
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
    hl = C.GREEN if manual_idx is not None else C.ACCENT
    pfx = "● " if manual_idx is not None else "▶ "

    clear_end = bottom_row if bottom_row is not None else (row + budget)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * width
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 2

    if prev_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {prev_line}{C.RESET}")
    out_row += 2

    for i, seg in enumerate(curr_wrapped):
        if i == 0:
            sys.stdout.write(f"\033[{out_row};{col + 1}H  {hl}{pfx}{seg}{C.RESET}{scroll_hint}")
        else:
            sys.stdout.write(f"\033[{out_row};{col + 1}H    {hl}{seg}{C.RESET}")
        out_row += 1

    out_row += 1
    if next_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {next_line}{C.RESET}")
    sys.stdout.flush()


def draw_lyric_initial(row: int, first_line: object, width: int | None = None,
                       max_row: int | None = None, col: int = 1,
                       bottom_row: int | None = None) -> None:
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
            raw_str = getattr(first_line, 'text')
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
    """Tries (in order):
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


def _find_timing_files_for_audio(audio_path: str) -> tuple[str | None, str | None]:
    """Find SRT and/or JSON timing files next to or beneath the audio file.

    Search order (JSON):
      1. {stem}.json or {stem}_timings.json in the same directory
      2. Subdirectories: any *.json whose stem matches the audio stem OR whose
         name contains "timings"
    Same stem matching handles the pattern where the audio and transcript share
    a filename (e.g. both named ' .mp3' / ' .json') but live in different dirs.
    Returns (srt_path, json_path).
    """
    base = Path(audio_path).stem
    parent = Path(audio_path).parent

    srt_path = None
    json_path = None

    # SRT: same dir first, then subdirectories by stem
    srt_candidate = parent / f"{base}.srt"
    if srt_candidate.exists():
        srt_path = str(srt_candidate)
    else:
        for srt_file in sorted(parent.rglob("*.srt")):
            if srt_file.parent != parent and srt_file.stem == base:
                srt_path = str(srt_file)
                break

    # JSON: same dir first
    for name in [f"{base}.json", f"{base}_timings.json"]:
        candidate = parent / name
        if candidate.exists():
            json_path = str(candidate)
            break

    # JSON: subdirectories — match by stem or "timings" in name
    if not json_path:
        for json_file in sorted(parent.rglob("*.json")):
            if json_file.parent == parent:
                continue
            if json_file.stem == base or "timings" in json_file.name.lower():
                json_path = str(json_file)
                break

    return srt_path, json_path


def parse_markdown_file(file_path: str) -> list[DialogueLine] | None:
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8')
        return _parse_markdown_dialogue(content)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _parse_word_timings_json(json_path: str) -> list[dict]:
    """Prefers top-level word_segments when present; otherwise flattens from segments[].words."""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if data.get('word_segments'):
        return data['word_segments']

    flattened: list[dict] = []
    for segment in data.get('segments', []):
        flattened.extend(segment.get('words', []))
    return flattened


def _match_md_to_timings(md_lines: list[DialogueLine], word_timings: list[dict]) -> None:
    word_idx = 0
    for line in md_lines:
        if line.is_empty():
            continue

        clean_text = re.sub(r'\*.*?\*|\(.*?\)', '', line.text)
        words_in_line = re.findall(r'\b\w+\b', clean_text.lower())

        if not words_in_line:
            continue

        line_start: float | None = None
        line_end: float | None = None

        for word_to_match in words_in_line:
            while word_idx < len(word_timings):
                current_json_word = word_timings[word_idx].get('word', '').lower().strip().strip(".,!?;:\"'")

                if current_json_word == word_to_match:
                    if line_start is None:
                        line_start = float(word_timings[word_idx]['start'])
                    line_end = float(word_timings[word_idx]['end'])
                    word_idx += 1
                    break
                else:
                    word_idx += 1

        # Apply the found timings
        if line_start is not None and line_end is not None:
            line.start = line_start
            line.end = line_end


class DialoguePlaybackState:
    """Manages sentence-by-sentence narrative dialogue states with timing support."""

    def __init__(self, audio_path: str, track_duration: float):
        md_path = _find_markdown_for_audio(audio_path)
        _, json_path = _find_timing_files_for_audio(audio_path)

        raw_dialogue_lines = None
        word_timings = None
        self.expanded_chunks: list[dict] = []
        self.line_times: list[tuple[float, float]] = []
        self.current_idx = 0
        self.load_error = None

        if md_path:
            try:
                dialogue = parse_markdown_file(md_path)
                if dialogue is None:
                    self.load_error = f"Failed to parse markdown: {md_path}"
                else:
                    raw_dialogue_lines = dialogue
                    if json_path:
                        try:
                            word_timings = _parse_word_timings_json(json_path)
                            _match_md_to_timings(raw_dialogue_lines, word_timings)
                        except Exception as e:
                            self.load_error = f"Error matching timings: {str(e)}"
            except Exception as e:
                self.load_error = f"Error reading markdown: {str(e)}"

        if raw_dialogue_lines:
            total_words = sum(max(1, len(clean_text_for_timing(line.text).split()))
                              for line in raw_dialogue_lines if not line.is_empty())
            dynamic_wps = total_words / track_duration if track_duration > 0 else 2.2
            self.expanded_chunks, self.line_times = expand_dialogue_into_sentences(
                raw_dialogue_lines,
                words_per_second=dynamic_wps,
                word_timings=word_timings,
                wrap_w=50
            )

    def is_active(self) -> bool:
        return bool(self.expanded_chunks)

    def update(self, elapsed: float) -> None:
        if self.line_times:
            self.current_idx = find_current_dialogue_line(self.line_times, elapsed)


def check_for_dialogue_files(audio_path: str) -> bool:
    md_path = _find_markdown_for_audio(audio_path)
    srt_path, json_path = _find_timing_files_for_audio(audio_path)
    return bool(md_path or srt_path or json_path)


def get_dialogue_file_info(audio_path: str) -> str:
    md_path = _find_markdown_for_audio(audio_path)
    srt_path, json_path = _find_timing_files_for_audio(audio_path)
    parts = []
    if md_path:   parts.append("MD")
    if json_path: parts.append("JSON timings")
    if srt_path:  parts.append("SRT")
    return " + ".join(parts) if parts else "No dialogue files"
