"""Lyrics parsing, timing and on-screen rendering (SYLT / USLT / markdown dialogue)."""
from __future__ import annotations
import bisect
import json
import re
import textwrap
import sys
import unicodedata
from pathlib import Path

from src.utils import prompt_core as _pc
from src.utils import ui_utils
from src.utils.ui_utils import Colors as C
# The MD script is overlaid onto the timed transcript by the same alignment the
# lyrics editor uses, so the player renders the same speakers / directions / text.
from src.lyrics.md_overlay import build_md_overlay, _reading_time

def normalize_lyric_newlines(text: str) -> str:
    """Normalize CRLF/CR line endings to \\n."""
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
        """True if chunk wraps to no more than max_lines rows."""
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
    """Split any USLT line that wraps past max_lines_per_chunk into shorter chunks,
    dividing its time window across the chunks proportionally by word count.

    Cached by the *content* of `lines` and `line_times`, never by `id(lines)`:
    the caller builds a fresh list per track (and another for the SYLT hand-off
    tail), and CPython readily hands a new list the id of a freed one — which
    served up the previous track's lyrics on the previous track's timings.  The
    times are part of the key because the tail reuses the same line text on
    shifted windows."""
    cache_key = (wrap_w, max_lines_per_chunk, len(lines),
                 hash(tuple(lines)), hash(tuple(line_times)))
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


# A clear leading "Speaker: " label (capitalised single token) — NOT a mid-line
# colon like "9:00" or "waiting: for you", which must not be stripped.
_SPEAKER_PREFIX_RE = re.compile(r"^[A-Z][A-Za-z.'-]{0,19}:\s")


def _timing_word_count(text: str) -> int:
    """Spoken-word count for timing: drop a leading speaker label and any
    parenthetical/bracketed asides, then count words."""
    text = _SPEAKER_PREFIX_RE.sub('', text, count=1)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    return len(text.split())


# Shortest window a line may be given.  A one-word line still needs long enough
# to be read, so proportional allocation alone is not enough.
_MIN_LINE_S = 0.5

# Speaking rate assumed when the track length is unknown (mutagen gave us
# nothing and VLC has not been probed yet).  Only reachable in that fallback.
_FALLBACK_WPS = 2.2


def _allocate_line_seconds(counts: list, track_duration: float) -> list:
    """Split track_duration across lines in proportion to their word counts, with
    every line getting at least _MIN_LINE_S.

    Lifting a short line up to the floor has to come out of the others, or the
    windows drift past the end of the track — so the floor is applied by
    water-filling: pin whatever falls below it, re-divide the time that is left
    over the lines still free, and repeat until nothing new gets pinned.  The
    returned durations sum to track_duration.
    """
    n = len(counts)
    if n == 0:
        return []
    # Not even the floors fit: the track is too short to read these lines at
    # all, so divide it evenly and let them fly by.
    if _MIN_LINE_S * n >= track_duration:
        return [track_duration / n] * n

    durations = [0.0] * n
    pinned = [False] * n
    while True:
        free = [i for i in range(n) if not pinned[i]]
        if not free:
            break
        free_time = track_duration - sum(durations[i] for i in range(n) if pinned[i])
        free_words = sum(counts[i] for i in free)
        if free_words <= 0:                     # only word-less lines left
            for i in free:
                durations[i] = free_time / len(free)
            break
        newly_pinned = False
        for i in free:
            if counts[i] / free_words * free_time < _MIN_LINE_S:
                durations[i] = _MIN_LINE_S
                pinned[i] = True
                newly_pinned = True
        if not newly_pinned:                    # everything left clears the floor
            for i in free:
                durations[i] = counts[i] / free_words * free_time
            break
    return durations


def build_uslt_line_times(lines: list, track_duration: float) -> list[tuple[float, float]]:
    """Estimate (start, end) windows for untimed USLT lines by allocating
    track_duration proportionally to each line's word count, after stripping
    a leading speaker label and parenthetical/bracketed asides.

    The windows are contiguous and end exactly at track_duration, so a long
    episode's lines stay in step with the audio the whole way through instead of
    running off the end.  A track_duration of 0 means the length is not known
    yet; the lines are then paced at _FALLBACK_WPS rather than scaled to fit.
    """
    counts = [_timing_word_count(line.text if hasattr(line, 'text') else str(line))
              for line in lines]
    if not counts:
        return []

    if track_duration and track_duration > 0:
        durations = _allocate_line_seconds(counts, float(track_duration))
    else:
        durations = [max(_MIN_LINE_S, n / _FALLBACK_WPS) for n in counts]

    times = []
    t = 0.0
    for duration in durations:
        times.append((t, t + duration))
        t += duration

    return times


def find_current_uslt_line(line_times: list[tuple[float, float]], elapsed: float) -> int:
    """Binary-search line_times for the line covering elapsed, clamped to range."""
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def _parse_markdown_dialogue(text: str) -> list[DialogueLine]:
    """Parse markdown dialogue text into DialogueLine entries, recognizing
    stage-direction-only lines, `**Speaker** (stage dir): text` headers, and
    unlabeled continuation lines appended to the previous entry."""
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

def _apply_markdown_formatting(text: str, base: str = "",
                               strong: str | None = None,
                               em: str | None = None) -> str:
    """Convert markdown emphasis into dynamic ANSI rendering escapes.

    `base` is the style the surrounding text is already drawn in (e.g. the colour
    of a highlighted row).  Each emphasis span closes by resetting AND then
    re-asserting `base`, so an emphasised word never leaves the rest of the line
    stripped of the row's own styling.  `strong`/`em` override the styles used for
    **strong** / *emphasis* when a caller wants them to stand out more.  The
    defaults reproduce the previous behaviour exactly (both bold, plain reset)."""
    if not text:
        return ""
    strong = C.BOLD if strong is None else strong
    em     = C.BOLD if em     is None else em
    close  = f"{C.RESET}{base}"
    text = re.sub(r'\*\*([^*]+)\*\*', f'{strong}\\1{close}', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', f'{em}\\1{close}', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', f'\\1 ({C.DIM}\\2{close})', text)
    return text


_ANSI_RE = re.compile(r'\033\[[0-9;]*m')


def _md_visible_len(text: str) -> int:
    """Printed width of a markdown fragment — what it measures after the emphasis
    markers are consumed.  Derived from the rendered output rather than a marker
    strip-list, so it can never disagree with `_apply_markdown_formatting` about
    which characters actually reach the screen."""
    return len(_ANSI_RE.sub('', _apply_markdown_formatting(text)))


def _close_open_md(row: str) -> tuple[str, str]:
    """Close any emphasis span still open at a row's end.

    Returns (row, reopen) — the row with the outstanding markers appended, and
    the markers needed to reopen the same spans at the head of the next row, so a
    **strong** or *emphasised* phrase that wraps keeps its styling on every row
    instead of leaving a literal `**` on screen.
    """
    stack: list[str] = []
    for m in re.finditer(r'\*\*|\*', row):
        tok = m.group()
        if stack and stack[-1] == tok:
            stack.pop()
        else:
            stack.append(tok)
    if not stack:
        return row, ''
    return row + ''.join(reversed(stack)), ''.join(stack)


def _md_rows(text: str, width: int, base: str = '', active: bool = False,
             first_width: int | None = None) -> list[str]:
    """Wrap `text` to `width` PRINTED columns and render its markdown as ANSI.

    `first_width` narrows only the first row, for a caller that prints something
    after it (the USLT window's `↕ scroll` hint).

    `textwrap.wrap` cannot be used on already-rendered text: the escape bytes
    count toward its width, so a styled line wraps far too early.  Here the
    wrapping measures printed width (`_md_visible_len`) and the styling is applied
    per row afterwards.

    Emphasis mirrors the editor's `_compose`: **strong** is bold, *emphasis* is
    italic, and a ♪ is accented on the active row / dim elsewhere so its glyph
    reads consistently instead of inheriting the row's weight.  Every span
    restores `base` — the row's own colour — so the emphasis never strips the rest
    of the line of the row's styling.
    """
    words = normalize_lyric_newlines(text).replace('\n', ' ').split()
    if not words:
        return []

    rows: list[str] = []
    cur: list[str] = []
    cur_w = 0
    limit = width if first_width is None else max(1, first_width)
    for word in words:
        wl = _md_visible_len(word)
        step = wl if not cur else wl + 1
        if cur and cur_w + step > limit:
            rows.append(' '.join(cur))
            cur, cur_w = [word], wl
            limit = width
        else:
            cur.append(word)
            cur_w += step
    if cur:
        rows.append(' '.join(cur))

    out: list[str] = []
    carry = ''
    for row in rows:
        row = carry + row
        row, carry = _close_open_md(row)
        disp = _apply_markdown_formatting(row, base=base, strong=C.BOLD, em=C.ITALIC)
        if '\u266a' in disp:
            note_c = C.ACCENT if active else C.DIM
            disp = disp.replace('\u266a', f"{C.RESET}{note_c}\u266a{C.RESET}{base}")
        out.append(f"{base}{disp}{C.RESET}" if base else disp)
    return out


def _dir_rows(text: str, width: int, base: str = '', active: bool = False) -> list[str]:
    """A stage direction as text-column rows: bracketed and italic.

    One place decides how a direction looks, so a cue riding on a line and a
    direction holding its own beat can never drift apart.  An already-bracketed
    text isn't double-bracketed.
    """
    label = (text or '').strip()
    if not label:
        return []
    if not (label.startswith('(') and label.endswith(')')):
        label = f"({label})"
    return [f"{C.ITALIC}{r}{C.RESET}"
            for r in _md_rows(label, width, base=base, active=active)]


def _format_speaker_list(items: list[str]) -> str:
    """Join speaker names into a markdown-bold list, joined with ', ' and 'and'."""
    if not items:
        return ""
    items = [item.strip() for item in items]
    if len(items) == 1:
        return f"**{items[0]}**"
    return ", ".join(f"**{item}**" for item in items[:-1]) + " and " + f"**{items[-1]}**"


class DialogueLine:
    def __init__(self, speakers: list[str] | None = None, stage_dir: str = "", text: str = "", start: float = 0.0, end: float = 0.0):
        """Store speakers, stage direction and text (whitespace-trimmed) plus the timing window."""
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
        """True if this line is a stage direction with no speakers or spoken text."""
        return not self.speakers and not self.text and bool(self.stage_dir)

    def is_empty(self) -> bool:
        """True if this line has no speakers, text, or stage direction at all."""
        return not self.speakers and not self.text and not self.stage_dir


def clean_text_for_timing(text: str) -> str:
    """Strip stage directions and parenthetical asides so what remains is spoken-only text for word-count/timing use."""
    cleaned = re.sub(r'\s*\*[^(]*\([^)]*\)\*\s*|\s*\([^)]*\)\s*', ' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


_AIR_THRESHOLD = 2.0  # seconds of silence that warrants a blank-line gap indicator


def expand_dialogue_into_sentences(
    dialogue_lines: list[DialogueLine],
    words_per_second: float,
    word_timings: list[dict] | None = None,
    wrap_w: int = 50,
) -> tuple[list[dict], list[tuple[float, float]]]:
    """Expand dialogue lines into per-sentence/clause chunks with timing windows.

    Each sentence's words are matched forward against word_timings when available,
    falling back to proportional word-count timing otherwise. Stage-direction-only
    lines get a gap window up to the next word's start without consuming track
    time. A final pass inserts synthetic 'is_air' chunks for silences longer than
    _AIR_THRESHOLD that aren't already covered by a stage direction."""
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
        """Find this sentence's (start, end) by matching its words forward through
        word_timings from the shared cursor; None if no word_timings or no match."""
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
                'cues': [], 'is_stage': True, 'is_air': False,
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
                    'cues': [], 'is_stage': False, 'is_air': False,
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
                    'text': '', 'cues': [], 'is_stage': False, 'is_air': True,
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
    # These rows are painted outside any frame — drop the painter's record of
    # them so the next full redraw repaints rather than trusting stale content.
    _pc.screen_forget_rows(row, max_row or row)

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
    rule_w = ui_utils.get_terminal_width() - col + 1
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{'─' * rule_w}{C.RESET}")
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
        cues      = [c for c in chunk.get('cues', []) if c and c.strip()]

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

        # The row's own colour.  Every markdown span inside the row restores it, so
        # an emphasised word never strips the rest of the line of its styling —
        # the same contract the editor's `_compose` uses.
        base = "" if is_active else C.DIM

        if is_air:
            right_lines = [""]
        elif is_stage:
            # All stage directions read the same way wherever they came from:
            # bracketed and italic, in the text column, never labelled with a
            # speaker.  The brackets are what mark it as a direction rather than
            # something anybody says out loud.
            right_lines = _dir_rows(stage_dir or text, text_width, base, is_active) or [""]
        else:
            if is_active:
                show_speaker = bool(speaker)
            else:
                show_speaker = bool(speaker) and (speaker != current_speaker)

            if show_speaker:
                raw_spk = _strip_markdown(speaker)
                wrapped_spk = textwrap.wrap(raw_spk, width=speaker_width)
                spk_c = C.BOLD if is_active else C.DIM
                left_lines.extend(f"{spk_c}{s}{C.RESET}" for s in wrapped_spk)
                if stage_dir:
                    # The line's own aside (from the MD banner, or a direction with
                    # no silence to occupy) sits under the name, as on the editor's
                    # speaker banner.
                    left_lines.extend(
                        f"{C.DIM}{C.ITALIC}{s}{C.RESET}"
                        for s in textwrap.wrap(f"({stage_dir})", width=speaker_width))

            if text:
                right_lines = _md_rows(text, text_width, base=base, active=is_active) or [""]

        # Directions about the words — a mid-line beat with no silence of its own,
        # or a standalone event happening over the line — sit under the line in the
        # TEXT column, bracketed and italic, the way the editor floats a cue.  They
        # are deliberately NOT put in the speaker column: that is reserved for the
        # MD banner's aside, which is the only direction that really does describe
        # the whole line.
        for cue in cues:
            right_lines.extend(_dir_rows(cue, text_width, base, is_active))

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
                clean_len = len(_ANSI_RE.sub('', left_lines[i]))
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
    """Binary-search line_times for the chunk covering elapsed, clamped to range."""
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))


def _strip_markdown(text: str) -> str:
    """Strip markdown emphasis/code markers (*_`~) for plain-text display."""
    return re.sub(r'[*_`~]', '', text)


def draw_lyric_window(row: int, sylt_data: list, current_idx: int,
                      width: int | None = None, max_row: int | None = None,
                      col: int = 1, bottom_row: int | None = None) -> None:
    """Render the SYLT lyric window (dimmed prev/next line, bold wrapped current line), clearing the area first."""
    # These rows are painted outside any frame — drop the painter's record of
    # them so the next full redraw repaints rather than trusting stale content.
    _pc.screen_forget_rows(row, max_row or row)

    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    budget = max(4, max_row - row - 1)
    wrap_w = max(20, width - 10)

    p_raw = sylt_data[current_idx - 1][0] if current_idx > 0 else ""
    c_raw = sylt_data[current_idx][0] if 0 <= current_idx < len(sylt_data) else ""
    n_raw = sylt_data[current_idx + 1][0] if 0 <= current_idx < len(sylt_data) - 1 else ""

    # Wrapping measures PRINTED width, so the emphasis escapes can't eat the
    # budget and wrap the line early (see `_md_rows`).
    p_wrapped = _md_rows(p_raw, wrap_w - 1, base=C.DIM)
    p_line = p_wrapped[-1] if p_wrapped else ""

    n_wrapped = _md_rows(n_raw, wrap_w - 1, base=C.DIM)
    n_line = n_wrapped[0] if n_wrapped else ""

    c_wrapped = _md_rows(c_raw, wrap_w - 4, base=C.BOLD,
                         active=True)[:max(1, budget - 4)]

    clear_end = bottom_row if bottom_row is not None else (row + budget)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * (ui_utils.get_terminal_width() - col + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 2

    if p_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {C.RESET}{p_line}")
    out_row += 2

    for i, seg in enumerate(c_wrapped or [""]):
        pfx = "▶ " if i == 0 else "  "
        if seg: sys.stdout.write(f"\033[{out_row};{col + 1}H  {C.BOLD}{pfx}{C.RESET}{seg}")
        out_row += 1

    out_row += 1
    if n_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {C.RESET}{n_line}")
    sys.stdout.flush()


def draw_uslt_window(row: int, all_lines: list, line_times: list,
                     elapsed: float, width: int | None = None,
                     manual_idx: int | None = None,
                     max_row: int | None = None,
                     col: int = 1, bottom_row: int | None = None) -> None:
    """Render the USLT lyric window (dimmed prev/next line, wrapped current line),
    honoring a manual scroll override via manual_idx over the elapsed-time index."""
    # These rows are painted outside any frame — drop the painter's record of
    # them so the next full redraw repaints rather than trusting stale content.
    _pc.screen_forget_rows(row, max_row or row)

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

    scroll_hint = f" {C.DIM}↕ scroll{C.RESET}"
    hl = C.GREEN if manual_idx is not None else C.ACCENT
    pfx = "● " if manual_idx is not None else "▶ "

    # Wrapping measures PRINTED width, so the emphasis escapes can't eat the
    # budget and wrap the line early (see `_md_rows`).
    # The first row shares its line with the scroll hint, so it gets that much
    # less room — otherwise a full-width line would push the hint off the edge.
    hint_w = len('↕ scroll') + 1
    curr_wrapped = (_md_rows(curr_text, wrap_w - 4, base=hl, active=True,
                             first_width=wrap_w - 4 - hint_w) or [''])[:max(1, budget - 4)]

    prev_wrapped = _md_rows(prev_text, wrap_w - 1, base=C.DIM)
    prev_line = prev_wrapped[-1] if prev_wrapped else ""

    next_wrapped = _md_rows(next_text, wrap_w - 1, base=C.DIM)
    next_line = next_wrapped[0] if next_wrapped else ""

    clear_end = bottom_row if bottom_row is not None else (row + budget)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * (ui_utils.get_terminal_width() - col + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 2

    if prev_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {C.RESET}{prev_line}")
    out_row += 2

    for i, seg in enumerate(curr_wrapped):
        if i == 0:
            sys.stdout.write(f"\033[{out_row};{col + 1}H  {hl}{pfx}{C.RESET}{seg}{scroll_hint}")
        else:
            sys.stdout.write(f"\033[{out_row};{col + 1}H    {seg}")
        out_row += 1

    out_row += 1
    if next_line: sys.stdout.write(f"\033[{out_row};{col + 1}H{C.DIM}  {C.RESET}{next_line}")
    sys.stdout.flush()


def draw_lyric_initial(row: int, first_line: object, width: int | None = None,
                       max_row: int | None = None, col: int = 1,
                       bottom_row: int | None = None) -> None:
    """Render a one-line preview of the first lyric before playback timing/highlighting begins."""
    # These rows are painted outside any frame — drop the painter's record of
    # them so the next full redraw repaints rather than trusting stale content.
    _pc.screen_forget_rows(row, max_row or row)

    width = width or ui_utils.get_terminal_width()
    _, term_rows = ui_utils.get_terminal_size()
    max_row = max_row or term_rows
    wrap_w = max(20, width - 8)

    clear_end = bottom_row if bottom_row is not None else (row + max_row)
    for i in range(row, clear_end + 1):
        sys.stdout.write(f"\033[{i};{col}H\033[K")

    out_row = row
    rule = "─" * (ui_utils.get_terminal_width() - col + 1)
    sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}{rule}{C.RESET}")
    out_row += 3

    if first_line:
        if isinstance(first_line, dict):
            raw_str = first_line.get('text', '')
        elif hasattr(first_line, 'text'):
            raw_str = getattr(first_line, 'text')
        else:
            raw_str = str(first_line)

        preview = _md_rows(raw_str, wrap_w - 4, base=C.DIM)
        if preview:
            sys.stdout.write(f"\033[{out_row};{col}H{C.DIM}  {C.RESET}{preview[0]}")
    sys.stdout.flush()


def _parse_sylt(audio) -> list[tuple[str, int]]:
    """Collect all SYLT frame entries from the tag, sorted by timestamp."""
    sylt_data = []
    for tag in audio.getall('SYLT'):
        sylt_data.extend(tag.text)
    sylt_data.sort(key=lambda x: x[1])
    return sylt_data


def save_sylt_entries(file_path: str, sylt_entries: list[tuple[str, int]]) -> None:
    """Write timestamped lyrics to the file's SYLT frame (replacing any existing)."""
    from mutagen.id3 import ID3
    from mutagen.id3._frames import SYLT
    from mutagen.id3._util import ID3NoHeaderError

    try:
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            audio = ID3()
        audio.delall('SYLT')
        audio.add(SYLT(encoding=3, lang='eng', format=2, type=1, text=sylt_entries))
        from src.id3.id3_tag_handler import save_id3
        save_id3(audio, file_path)   # v2.4 iff a multi-value frame is present
    except Exception as exc:
        ui_utils.show_status(f"Failed to save SYLT: {exc}", duration=4.0)
        raise


def _parse_uslt(audio) -> list[tuple[str, int]]:
    """Extract USLT text into (line, 0) tuples, normalizing newlines and dropping blank lines."""
    tags = audio.getall('USLT')
    if not tags:
        return []
    text = tags[0].text
    if isinstance(text, list):
        text = '\n'.join(text)
    text = normalize_lyric_newlines(text)
    return [(line.strip(), 0) for line in text.split('\n') if line.strip()]


def estimate_sylt_last_line_end(sylt_data: list[tuple[str, int]], duration_ms: float, words_per_second: float = 2.2) -> float:
    """Estimate when the last SYLT line finishes, from its word count and words_per_second, capped at the track duration."""
    if not sylt_data: return 0.0
    last_text, last_ts_ms = sylt_data[-1]
    last_ts = last_ts_ms / 1000.0
    word_count = max(1, len(last_text.split()))
    estimated_end = last_ts + word_count / words_per_second
    return min(estimated_end, duration_ms / 1000.0)


def find_uslt_handoff_index(uslt_lines: list[str], last_sylt_text: str) -> int:
    """Find the USLT index to resume display at after the last SYLT line, matching
    exactly first then falling back to substring containment."""
    def _norm(s: str) -> str:
        """Collapse whitespace and lowercase for line-text comparison."""
        return re.sub(r'\s+', ' ', s.strip().lower())
    target = _norm(last_sylt_text)
    for i, line in enumerate(uslt_lines):
        if _norm(line) == target: return i + 1
    for i, line in enumerate(uslt_lines):
        n = _norm(line)
        if target in n or n in target: return i + 1
    # No match: resume past the end (show nothing) rather than replaying USLT from
    # the top after SYLT has already covered the song.
    return len(uslt_lines)

def _find_markdown_for_audio(audio_path: str) -> str | None:
    """Tries (in order):
      1. {basename}.md
      2. {basename}.dialogue.md
    """
    base = Path(audio_path).stem
    parent = Path(audio_path).parent

    for pattern in [f"{base}.md", f"{base}.dialogue.md", "transcript.md"]:
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
    for name in [f"{base}.json", f"{base}_timings.json", "transcript.json"]:
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
    """Read and parse a markdown dialogue file; None if missing or unreadable."""
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding='utf-8')
        return _parse_markdown_dialogue(content)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _parse_word_timings_json(json_path: str) -> list[dict]:
    """Return the flat list of spoken word timings.

    Prefers a top-level ``word_segments`` list (WhisperX / the lyrics-editor
    export, already spoken-only); otherwise flattens ``segments[].words``.  The
    editor's enriched transcripts add ``kind: stage_dir`` / ``dead_air`` segments
    that carry no spoken words — those are skipped so the timing stream stays
    purely the spoken words regardless of which format the file is in."""
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if data.get('word_segments'):
        return data['word_segments']

    flattened: list[dict] = []
    for segment in data.get('segments', []):
        if segment.get('kind') in ('stage_dir', 'dead_air'):
            continue
        flattened.extend(segment.get('words', []))
    return flattened


def _find_enriched_transcript(audio_path: str) -> str | None:
    """Find a transcript that still carries the editor's `kind` beats (dead air /
    stage directions), for the fallback path where the transcript has word timings
    but no segments to overlay the MD onto.

    The editor's `.sync.json` is deliberately SKIPPED: it is a work-in-progress
    save, so treating it as authoritative would let a half-finished edit change
    what playback shows.  The MD script is the shared source of truth instead —
    `_chunks_from_segments` re-derives the speakers and directions from it on every
    load.  Returns None when no enriched transcript exists."""
    _, json_path = _find_timing_files_for_audio(audio_path)
    parent = Path(audio_path).parent
    candidates: list[Path] = []
    # Prefer any enriched transcript files that are NOT the editor's working
    # sidecar ("*.sync.json") — the sidecar is a WIP save and should not be
    # treated as the authoritative enriched transcript for playback. Look for
    # other JSON files that contain editor `kind` beats instead.
    if json_path:
        candidates.append(Path(json_path))                       # transcript.json (preferred)
    # Search subdirectories for any enriched transcripts, but skip '*.sync.json'
    for p in sorted(parent.rglob('*.json')):
        if p == Path(json_path) or p.name.endswith('.sync.json'):
            continue
        candidates.append(p)
    seen: set[str] = set()
    for c in candidates:
        cs = str(c)
        if cs in seen or not c.exists():
            continue
        seen.add(cs)
        try:
            with open(c, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if any(s.get('kind') in ('dead_air', 'stage_dir') for s in data.get('segments', [])):
            return cs
    return None


def _parse_air_beats(json_path: str) -> list[tuple[float, float]]:
    """Timed silence windows (start, end) from an enriched transcript's `dead_air`
    segments — the editor's explicit silences, in absolute track time."""
    try:
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    beats: list[tuple[float, float]] = []
    for s in data.get('segments', []):
        if s.get('kind') == 'dead_air' and s.get('start') is not None and s.get('end') is not None:
            beats.append((float(s['start']), float(s['end'])))
    return beats


def _parse_stage_dirs(json_path: str) -> list[tuple[float, float, str]]:
    """Timed stage-direction windows (start, end, text) from an enriched
    transcript's `stage_dir` segments — preserves the editor's explicit
    standalone directions for playback merging."""
    try:
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    out: list[tuple[float, float, str]] = []
    for s in data.get('segments', []):
        if s.get('kind') == 'stage_dir' and s.get('start') is not None and s.get('end') is not None:
            out.append((float(s['start']), float(s['end']), str(s.get('text', '') or s.get('_md_text', '') or '')))
    return out


def _matchable(word: str) -> str:
    """Reduce a word to bare lowercase letters and digits for fuzzy matching.

    NFKD-decomposes the input so accented/modified letters shed their combining
    marks, then keeps only characters whose Unicode category starts with ‘L’
    (letter) or ‘N’ (number).  This handles apostrophes, okinas, curly quotes,
    hyphens, diacritics, and any other punctuation-adjacent characters without
    maintaining an explicit strip-list.
    """
    decomposed = unicodedata.normalize('NFKD', word.lower())
    return ''.join(c for c in decomposed if unicodedata.category(c)[0] in ('L', 'N'))


def _match_md_to_timings(md_lines: list[DialogueLine], word_timings: list[dict], json_segments: list[dict] | None = None) -> None:
    """Walk each markdown line's words against the word_timings stream in order,
    stamping line.start/line.end from the first/last matched word."""
    # If the original transcript.json carries segment boundaries, prefer
    # assigning an MD line the start/end of the transcript segment that
    # contains most of its words. This anchors MD→timings to the user's
    # Whisper segments and reduces mis-association of repeated words.
    if json_segments:
        # Precompute normalized word sets for segments
        seg_word_sets: list[set[str]] = []
        for seg in json_segments:
            txt = seg.get('text', '') if isinstance(seg, dict) else ''
            words = [t for t in re.split(r'[^\\w]+', txt, flags=re.UNICODE) if t]
            seg_word_sets.append(set(_matchable(w) for w in words if _matchable(w)))

        for line in md_lines:
            if line.is_empty():
                continue
            clean_text = re.sub(r'\*.*?\*|\(.*?\)', '', line.text)
            words_in_line = [t for t in re.split(r'[^\w]+', clean_text, flags=re.UNICODE) if t]
            if not words_in_line:
                continue
            norm_words = [ _matchable(w) for w in words_in_line if _matchable(w) ]
            if not norm_words:
                continue
            best_i = -1; best_score = 0
            for i, sset in enumerate(seg_word_sets):
                score = sum(1 for w in norm_words if w in sset)
                if score > best_score:
                    best_score = score; best_i = i
            # Accept a match only if it covers at least half the words, or at
            # least one word when the line is very short.
            if best_i >= 0 and (best_score >= max(1, len(norm_words) // 2)):
                seg = json_segments[best_i]
                if seg.get('start') is not None and seg.get('end') is not None:
                    line.start = float(seg['start']); line.end = float(seg['end'])
                    continue

    # Fallback: sequential word-timings matching (existing behaviour)
    word_idx = 0
    for line in md_lines:
        if line.is_empty():
            continue

        clean_text = re.sub(r'\*.*?\*|\(.*?\)', '', line.text)
        # Split on any run of non-letter/digit chars so Unicode word-internal
        # punctuation (apostrophes, okinas, hyphens, …) doesn’t fragment tokens.
        words_in_line = [t for t in re.split(r'[^\w]+', clean_text, flags=re.UNICODE) if t]

        if not words_in_line:
            continue

        line_start: float | None = None
        line_end: float | None = None

        for word_to_match in words_in_line:
            match_key = _matchable(word_to_match)
            if not match_key:
                continue
            while word_idx < len(word_timings):
                current_json_word = _matchable(word_timings[word_idx].get('word', ''))

                if current_json_word == match_key:
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


# A queued stage direction needs at least this much silence before the next line
# to be worth its own beat on screen.  Below it the direction is describing
# something happening *during* the speech (a bell still dinging, a name being
# struggled over), so it rides along on the line itself instead of stealing a beat
# from it.  Set from the gap distribution of real scripts, where the large
# majority of directions sit in a clear pause and a handful genuinely overlap.
_SD_MIN_GAP = 0.35


def _chunks_from_segments(segs: list[dict], md_path: str,
                          track_duration: float = 0.0) -> tuple[list[dict], list[tuple[float, float]]]:
    """Build playback chunks from a timed transcript, overlaid with the MD script.

    The transcript owns the timing; the MD owns everything the timing cannot
    carry — who is speaking, the script's own punctuation and capitalisation, its
    markdown emphasis, and the stage directions.  Both are joined by
    `md_overlay.build_md_overlay`, the SAME single word-level alignment the lyrics
    editor renders from, so the player and the editor can never disagree about a
    line's speaker, text or directions.

    A queued direction is given the silence it sits in as its own beat; one with
    no silence to occupy is attached to the following line (see `_SD_MIN_GAP`).
    Any silence still unclaimed and longer than `_AIR_THRESHOLD` becomes a dead-air
    beat, so no line is left on screen — or shown early — while nobody is speaking.
    `track_duration` extends that to the run-out after the last line; 0 means
    unknown, which leaves the run-out alone.

    Returns (chunks, line_times) in the shape `draw_dialogue_window` expects.
    """
    overlay, quality, links = build_md_overlay(segs, md_path)

    ov_map: dict[int, list] = {}
    for ov in overlay:
        ov_map.setdefault(ov['before_si'], []).append(ov)

    # Who speaks each MD line, and the line's own aside.  Attribution is looked up
    # per segment through `links` (the alignment's seg → MD line pin) rather than
    # carried forward from the last banner: a segment the MD does not explain gets
    # no speaker, instead of inheriting whoever spoke last for the rest of the
    # track.  `line_seen` makes the aside show once, on its line's opening row.
    speaker_of_line: dict[int, tuple[str, str]] = {}
    for ov in overlay:
        if ov['kind'] == 'speaker' and ov.get('line') is not None:
            speaker_of_line[ov['line']] = (ov['text'], ov.get('stage', '') or '')

    chunks: list[dict] = []
    times: list[tuple[float, float]] = []
    line_seen: set[int] = set()
    pending: list[tuple[str, str]] = []   # (text, lean) awaiting a silence to show in

    def _emit(chunk: dict, window: tuple[float, float]) -> None:
        """Append a chunk and its time window."""
        chunks.append(chunk)
        times.append(window)

    def _prev_end() -> float:
        """End of the last emitted window (0.0 before anything is emitted)."""
        return times[-1][1] if times else 0.0

    def _stage(text: str) -> dict:
        """A direction shown as its own beat."""
        return {'parent_idx': -1, 'speaker': '', 'stage_dir': text,
                'text': '', 'cues': [], 'is_stage': True, 'is_air': False}

    def _flush(next_start: float | None) -> list[str]:
        """Place queued directions in the silence before `next_start`.

        With enough silence each gets a beat of its own.  Without it the direction
        becomes a CUE on the line it belongs with — an extra bracketed row in the
        text column, the way the editor floats one, rather than a note hung off the
        speaker: '*(She pronounces it "roth.")*' describes one word, not the whole
        line, and the speaker column is reserved for the MD banner's own aside
        ('**NAME** (exasperated): …'), which really is a whole-line manner note.

        `lean == 'prev'` (a mid-line note sitting after the words it refers to)
        attaches to the line just gone; the rest are returned for the line ahead.
        """
        nonlocal pending
        if not pending:
            return []
        held = pending
        pending = []
        if next_start is None:                       # nothing follows — show them out
            t = _prev_end()
            for text, _lean in held:
                span = _reading_time(text)
                _emit(_stage(text), (t, t + span))
                t += span
            return []
        gap = next_start - _prev_end()
        if gap >= _SD_MIN_GAP:
            # Share the silence out by reading time so a long direction gets longer.
            weights = [_reading_time(text) for text, _ in held]
            total = sum(weights) or 1.0
            t = _prev_end()
            for (text, _lean), w in zip(held, weights):
                span = gap * (w / total)
                _emit(_stage(text), (t, t + span))
                t += span
            return []
        forward: list[str] = []
        for text, lean in held:
            back = chunks[-1] if chunks else None
            if lean == 'prev' and back is not None and not back['is_air'] and not back['is_stage']:
                back['cues'].append(text)
            else:
                forward.append(text)
        return forward

    def _air_if_silent(next_start: float) -> None:
        """Blank the display through silence that nothing else claimed.

        `find_current_dialogue_line` resolves a moment in a gap to the line that
        comes NEXT, so without this the upcoming line sits on screen for the whole
        pause — up to several seconds before anyone says it.  A dead-air beat holds
        that space instead, drawn as nothing (the previous and next lines stay
        dimmed either side, so the place in the script is still legible).

        Consecutive silences merge into one beat rather than stacking up, and
        because the cursor starts at 0.0 this also covers the lead-in before the
        first line.
        """
        start = _prev_end()
        if next_start <= start:
            return
        if chunks and chunks[-1]['is_air']:
            times[-1] = (times[-1][0], next_start)      # one continuous run
        elif next_start - start > _AIR_THRESHOLD:
            _emit({'parent_idx': -1, 'speaker': '', 'stage_dir': '',
                   'text': '', 'cues': [], 'is_stage': False, 'is_air': True},
                  (start, next_start))

    for si, seg in enumerate(segs):
        for ov in ov_map.get(si, []):
            if ov['kind'] == 'stage_dir':
                pending.append((ov['text'].strip(), ov.get('lean', 'next')))

        s_start = seg.get('start')
        s_end = seg.get('end')
        kind = seg.get('kind')

        if s_start is None or s_end is None:
            # Untimed beat — keep it out of the timeline rather than parking the
            # display on a zero-length window.  Anything queued stays queued, to
            # be placed against the next segment that does carry a time.
            continue
        window = (float(s_start), float(s_end))

        # A direction with no silence ahead of it rides along on this beat.
        carried = _flush(float(s_start))
        # Directions get first claim on a silence; whatever is left goes dead.
        _air_if_silent(float(s_start))

        if kind == 'dead_air':
            # Silence, unless a direction is riding along — then it is silence
            # with something to say about it.
            _emit({**_stage(carried[0] if carried else ''), 'parent_idx': si,
                   'cues': carried[1:], 'is_stage': bool(carried),
                   'is_air': not carried}, window)
        elif kind == 'stage_dir':
            label = seg.get('text', '').strip().strip('()')
            _emit({**_stage(label), 'parent_idx': si, 'cues': carried}, window)
        else:
            mq = quality.get(si) or {}
            text = (mq.get('md_text') or seg.get('text', '')).strip()
            line = links.get(si)
            speaker, stage = speaker_of_line.get(line, ('', '')) if line is not None else ('', '')
            if line in line_seen:
                stage = ''          # the line's aside belongs to its opening row
            elif line is not None:
                line_seen.add(line)
            # `stage_dir` is the MD banner's aside and belongs to the speaker;
            # `cues` are directions about the words, shown in the text column.
            _emit({'parent_idx': si, 'speaker': speaker, 'stage_dir': stage,
                   'text': text, 'cues': carried,
                   'is_stage': False, 'is_air': False}, window)

    for ov in ov_map.get(len(segs), []):             # trailing overlay items
        if ov['kind'] == 'stage_dir':
            pending.append((ov['text'].strip(), ov.get('lean', 'next')))
    _flush(None)
    if track_duration > 0:
        _air_if_silent(float(track_duration))        # the run-out after the last line

    return chunks, times


class DialoguePlaybackState:
    """Manages sentence-by-sentence narrative dialogue states with timing support."""

    def __init__(self, audio_path: str, track_duration: float):
        """Load and expand the dialogue track for audio_path: find markdown + timing
        files, parse dialogue lines, match them to word timings, expand into timed
        sentence chunks, and merge in explicit dead-air beats; sets load_error on failure."""
        md_path = _find_markdown_for_audio(audio_path)
        _, json_path = _find_timing_files_for_audio(audio_path)

        raw_dialogue_lines = None
        word_timings = None
        self.expanded_chunks: list[dict] = []
        self.line_times: list[tuple[float, float]] = []
        self.current_idx = 0
        self.load_error = None
        loaded_from_segments = False  # Track if we already have a full segmentation

        # Diagnostic: report if files aren't found
        if not md_path:
            self.load_error = f"No markdown dialogue file found near {audio_path}"
        if not json_path:
            if self.load_error:
                self.load_error += f"; no timing file found"
            else:
                self.load_error = f"No timing file found near {audio_path}"

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
                            # Load the transcript's segments too: they own the
                            # timing, and overlaying the MD onto them (rather than
                            # re-timing the MD ourselves) is what makes playback
                            # show the same lines the editor does.
                            try:
                                with open(json_path, encoding='utf-8') as jf:
                                    jcontainer = json.load(jf)
                                    json_segments = jcontainer.get('segments', [])
                            except (OSError, ValueError):
                                json_segments = None
                            if json_segments:
                                # Segments + MD overlay: exact timing from the
                                # transcript, speakers / directions / script text
                                # and emphasis from the markdown.
                                self.expanded_chunks, self.line_times = _chunks_from_segments(
                                    json_segments, md_path, track_duration)
                                self.load_error = None
                                loaded_from_segments = True   # skip the MD re-timing path
                                json_segments = None
                                word_timings = None
                            else:
                                _match_md_to_timings(raw_dialogue_lines, word_timings, json_segments)
                        except Exception as e:
                            self.load_error = f"Error matching timings: {str(e)}"
            except Exception as e:
                self.load_error = f"Error reading markdown: {str(e)}"

        if raw_dialogue_lines and not loaded_from_segments:
            total_words = sum(max(1, len(clean_text_for_timing(line.text).split()))
                              for line in raw_dialogue_lines if not line.is_empty())
            dynamic_wps = total_words / track_duration if track_duration > 0 else 2.2
            self.expanded_chunks, self.line_times = expand_dialogue_into_sentences(
                raw_dialogue_lines,
                words_per_second=dynamic_wps,
                word_timings=word_timings,
                wrap_w=50
            )
            self._merge_air_beats(audio_path)

    def _merge_air_beats(self, audio_path: str) -> None:
        """Fold the editor's explicit dead-air windows (from the enriched sidecar)
        into the timeline as silence beats, wherever they land in a gap not already
        covered by a spoken/stage/air chunk.  They render as the usual unlabelled
        `⋯` silence indicator — we just place them where the editor marked them,
        rather than only inferring silence from wide gaps."""
        enriched = _find_enriched_transcript(audio_path)
        if not enriched:
            return
        for a_s, a_e in _parse_air_beats(enriched):
            if any(not (e <= a_s or s >= a_e) for (s, e) in self.line_times):
                continue   # overlaps existing content — leave it be
            pos = bisect.bisect_left([s for (s, _) in self.line_times], a_s)
            self.line_times.insert(pos, (a_s, a_e))
            self.expanded_chunks.insert(pos, {
                'parent_idx': -1, 'speaker': '', 'stage_dir': '',
                'text': '', 'cues': [], 'is_stage': False, 'is_air': True,
            })
        # Also merge any explicit stage-direction beats so playback matches the
        # editor's standalone directions. Insert them as `is_stage` chunks
        # carrying the stage text.
        for s_s, s_e, s_txt in _parse_stage_dirs(enriched):
            if any(not (e <= s_s or s >= s_e) for (s, e) in self.line_times):
                continue
            pos = bisect.bisect_left([s for (s, _) in self.line_times], s_s)
            self.line_times.insert(pos, (s_s, s_e))
            self.expanded_chunks.insert(pos, {
                'parent_idx': -1, 'speaker': '', 'stage_dir': s_txt,
                'text': '', 'cues': [], 'is_stage': True, 'is_air': False,
            })

    def is_active(self) -> bool:
        """True if any dialogue chunks were loaded."""
        return bool(self.expanded_chunks)

    def update(self, elapsed: float) -> None:
        """Advance current_idx to the chunk covering elapsed."""
        if self.line_times:
            self.current_idx = find_current_dialogue_line(self.line_times, elapsed)