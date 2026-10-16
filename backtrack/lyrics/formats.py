"""Lyrics parsing, timing and on-screen rendering (SYLT / USLT / markdown dialogue)."""
from __future__ import annotations
import bisect
import json
import re
import textwrap
from pathlib import Path

from backbone import ui
from backbone.ui import Colors as C
# The MD script is overlaid onto the timed transcript by the same alignment the
# lyrics editor uses, so the player renders the same speakers / directions / text.
from backtrack.lyrics.md_overlay import build_md_overlay, _reading_time
from backtrack.lyrics import text as _lt
from backtrack import tuning as tune

def normalize_lyric_newlines(text: str) -> str:
    """Normalise CRLF/CR line endings to \\n."""
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



# A clear leading "Speaker: " label (capitalised single token), NOT a mid-line
# colon like "9:00" or "waiting: for you", which must not be stripped.
_SPEAKER_PREFIX_RE = re.compile(r"^[A-Z][A-Za-z.'-]{0,19}:\s")


def _timing_word_count(text: str) -> int:
    """Spoken-word count for timing: drop a leading speaker label and any
    parenthetical/bracketed asides, then count words."""
    text = _SPEAKER_PREFIX_RE.sub('', text, count=1)
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    return len(text.split())


# See tuning.LYRIC_MIN_LINE_S.
_MIN_LINE_S = tune.LYRIC_MIN_LINE_S

# See tuning.LYRIC_FALLBACK_WPS.
_FALLBACK_WPS = tune.LYRIC_FALLBACK_WPS


def _allocate_line_seconds(counts: list, track_duration: float) -> list:
    """Split track_duration across lines in proportion to their word counts, with
    every line getting at least _MIN_LINE_S.

    Lifting a short line up to the floor has to come out of the others, or the
    windows drift past the end of the track, so the floor is applied by
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


def _parse_markdown_dialogue(text: str) -> list[DialogueLine]:
    """Parse markdown dialogue text into DialogueLine entries, recognising
    stage-direction-only lines, `**Speaker** (stage dir): text` headers, and
    unlabelled continuation lines appended to the previous entry."""
    lines = normalize_lyric_newlines(text).split('\n')
    dialogue_lines = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Stage-direction-only lines FIRST, so one whose text contains a colon,
        # *(Immediately: bing bong.)*, is never split as a `Speaker: text` header.
        # `standalone_stage_dir` shares the bracket grammar with the inline matcher.
        sdir = _lt.standalone_stage_dir(stripped)
        if sdir is not None:
            dialogue_lines.append(DialogueLine(stage_dir=sdir))
            continue

        # Split at first colon only
        if ':' in stripped:
            header, dialogue = stripped.split(':', 1)

            # Extract stage dirs from the header (e.g., "**A** (sigh): text").  All
            # of them: "**DEROCHE** *(Swiss accent)* *(muffled)*:" carries two, and
            # taking only the first dropped the second on the floor.  Cut by span:
            # str.replace would also delete an identical bracket elsewhere.
            dirs = [(m.start(), m.end(), m.group(1))
                    for m in re.finditer(r'\(([^)]+)\)', header)]
            stage_dir = "; ".join(d.strip() for _, _, d in dirs)
            for lo, hi, _ in reversed(dirs):
                header = header[:lo] + header[hi:]

            # Extract all **Name** bolded blocks.  Cutting the directions out can
            # leave the markers behind ("**DEROCHE** ** **"), so blanks are dropped
            # rather than becoming a nameless speaker.
            speakers = re.findall(r'\*\*([^*]+)\*\*', header)

            dialogue_lines.append(DialogueLine(
                speakers=[n for n in (s.strip() for s in speakers) if n],
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
    **strong** / *emphasis* when a caller wants them to stand out more (both
    default to bold)."""
    if not text:
        return ""
    strong = C.BOLD if strong is None else strong
    em     = C.BOLD if em     is None else em
    close  = f"{C.RESET}{base}"
    text = re.sub(r'\*\*([^*]+)\*\*', f'{strong}\\1{close}', text)
    text = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', f'{em}\\1{close}', text)
    # _underscores_ are the other emphasis the scripts use (_Those Magnificent Men
    # in their Flying Machines_); word-internal ones are left alone.
    text = re.sub(r'(?<![\w*])_([^_]+)_(?![\w*])', f'{em}\\1{close}', text)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', f'\\1 ({C.DIM}\\2{close})', text)
    return text


def _md_visible_len(text: str) -> int:
    """Printed width of a markdown fragment: what it measures after the emphasis
    markers are consumed.  Derived from the rendered output rather than a marker
    strip-list, so it can never disagree with `_apply_markdown_formatting` about
    which characters actually reach the screen."""
    return ui.visual_len(_apply_markdown_formatting(text))


def _close_open_md(row: str) -> tuple[str, str]:
    """Close any emphasis span still open at a row's end.

    Returns (row, reopen): the row with the outstanding markers appended, and
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
    restores `base` (the row's own colour), so the emphasis never strips the rest
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
    """One parsed line of the markdown script: speakers, a stage direction, the
    text, and its timing window once matched."""

    def __init__(self, speakers: list[str] | None = None, stage_dir: str = "", text: str = "", start: float = 0.0, end: float = 0.0):
        """Store speakers, stage direction and text (whitespace-trimmed) plus the timing window."""
        self.speakers = speakers or []
        self.stage_dir = stage_dir.strip()
        self.text = text.strip()
        self.start = start
        self.end = end

    @property
    def speaker(self) -> str:
        """The speakers as one display string ('A, B and C')."""
        return _format_speaker_list(self.speakers)

    def is_stage_direction(self) -> bool:
        """True if this line is a stage direction with no speakers or spoken text."""
        return not self.speakers and not self.text and bool(self.stage_dir)

    def is_empty(self) -> bool:
        """True if this line has no speakers, text, or stage direction at all."""
        return not self.speakers and not self.text and not self.stage_dir


clean_text_for_timing = _lt.clean_for_timing


_AIR_THRESHOLD = tune.LYRIC_AIR_THRESHOLD_S


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

    # The shared word normaliser: both sides of this comparison go through the
    # same fold as the aligner, so a word that matches when the overlay is built
    # still matches when the sentence is timed.
    _norm = _lt.matchable

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
            for i in range(local_cursor,
                           min(local_cursor + tune.LYRIC_MATCH_WINDOW_WORDS, len(word_timings))):
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

        # Stage-direction-only lines have no spoken words: give them a time
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
                'is_stage': True, 'is_air': False,
            })
            time_windows.append((gap_start, gap_end))
            # Don't advance total_elapsed: stage directions don't consume time.
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
                    'parent_idx': idx, 'line': idx, 'speaker': line.speaker,
                    'stage_dir': line.stage_dir, 'text': sub_s,
                    'is_stage': False, 'is_air': False,
                })
                time_windows.append((chunk_start, chunk_end))
                total_elapsed = chunk_end

    # Pre-pass: evenly divide consecutive is_stage chunks within their gap.
    # All stage directions in a group are given the same (gap_start, next_word_start)
    # window because total_elapsed isn't advanced for them. Fix that here so
    # the lyric pane can step through each one in sequence.
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





_strip_markdown = _lt.strip_markdown



def _parse_sylt(audio) -> list[tuple[str, int]]:
    """Collect all SYLT frame entries from the tag, sorted by timestamp."""
    sylt_data = []
    for tag in audio.getall('SYLT'):
        sylt_data.extend(tag.text)
    sylt_data.sort(key=lambda x: x[1])
    return sylt_data


_LRC_TIMESTAMP_RE = re.compile(r'\[(\d+):(\d+)(?:[.:](\d{1,3}))?\]')
_LRC_META_RE = re.compile(r'^\s*\[(ti|ar|al|by|offset|re|ve)\s*:.+\]\s*$', re.I)


def parse_lrc_text(raw: str) -> list[tuple[str, int | None]]:
    """LRC text (a file's, or lyrics kept in a tag) as (text, timestamp_ms) lines;
    an untimed line has None."""
    entries: list[tuple[str, int | None]] = []
    for raw_line in raw.splitlines():
        if _LRC_META_RE.match(raw_line):
            continue
        timestamps = list(_LRC_TIMESTAMP_RE.finditer(raw_line))
        text = _LRC_TIMESTAMP_RE.sub("", raw_line).strip()
        if not text and not timestamps:
            continue
        if timestamps:
            for match in timestamps:
                mins = int(match.group(1))
                secs = int(match.group(2))
                frac = match.group(3) or "0"
                ms = int(frac.ljust(3, "0")[:3])
                entries.append((text, mins * 60_000 + secs * 1_000 + ms))
        else:
            entries.append((text, None))
    return entries


def save_sylt_entries(file_path: str, sylt_entries: list[tuple[str, int]],
                      desc: str = '', lang: str = 'eng') -> None:
    """Write timestamped lyrics to the file's SYLT frame for `desc`/`lang`,
    replacing that one frame only; other SYLT frames (other languages or
    descriptions) are kept."""
    from mutagen.id3._frames import SYLT
    from backtrack.id3.tag_handler import load_id3, save_id3

    try:
        audio = load_id3(file_path)
        frame = SYLT(encoding=3, lang=lang, desc=desc, format=2, type=1, text=sylt_entries)
        audio.delall(frame.HashKey)
        audio.add(frame)
        save_id3(audio, file_path)   # v2.4 iff a multi-value frame is present
    except Exception as exc:
        ui.show_error(f"couldn't save SYLT: {exc}")
        raise


def _parse_uslt(audio) -> list[tuple[str, int]]:
    """Extract USLT text into (line, 0) tuples, normalising newlines and dropping blank lines."""
    tags = audio.getall('USLT')
    if not tags:
        return []
    text = tags[0].text
    if isinstance(text, list):
        text = '\n'.join(text)
    text = normalize_lyric_newlines(text)
    return [(line.strip(), 0) for line in text.split('\n') if line.strip()]



def _find_markdown_for_audio(audio_path: str, packed: bool = True) -> str | None:
    """The track's script: from its .bkts (unless `packed` is False), else
    `<stem>.md`, `<stem>.dialogue.md` or `transcript.md` beside it; None if
    there's none."""
    from backtrack.lyrics import bkts
    if packed and (inside := bkts.member(audio_path, bkts.SCRIPT)):
        return inside
    base = Path(audio_path).stem
    parent = Path(audio_path).parent

    for pattern in [f"{base}.md", f"{base}.dialogue.md", "transcript.md"]:
        candidate = parent / pattern
        if candidate.exists():
            return str(candidate)

    return None


def _find_timing_files_for_audio(audio_path: str, packed: bool = True) -> tuple[str | None, str | None]:
    """Find SRT and/or JSON timing files next to or beneath the audio file.

    Search order (JSON):
      0. the track's .bkts (unless `packed` is False)
      1. `<stem>.json`, `<stem>_timings.json` or `transcript.json` beside the track
      2. Subdirectories: any *.json whose stem matches the audio stem OR whose
         name contains "timings"
    Stem matching handles an audio file and transcript that share a name but
    live in different dirs (e.g. `ep1.mp3` and `timings/ep1.json`).  The lyrics
    editor uses the same finder.  Returns (srt_path, json_path).
    """
    base = Path(audio_path).stem
    parent = Path(audio_path).parent

    from backtrack.lyrics import bkts
    srt_path = None
    json_path = bkts.member(audio_path, bkts.TRANSCRIPT) if packed else None

    # SRT: same dir first, then subdirectories by stem
    srt_candidate = parent / f"{base}.srt"
    if srt_candidate.exists():
        srt_path = str(srt_candidate)
    else:
        for srt_file in sorted(parent.rglob("*.srt")):
            if srt_file.parent != parent and srt_file.stem == base:
                srt_path = str(srt_file)
                break

    # JSON: same dir first. Each candidate is opened and checked, because a file
    # can be named like a transcript without being one: an anchor transcription
    # sitting in `timings/` matches by stem, and taking it would quietly cost the
    # track its real timings.
    for name in ([] if json_path else [f"{base}.json", f"{base}_timings.json", "transcript.json"]):
        candidate = parent / name
        if candidate.exists() and load_transcript(candidate):
            json_path = str(candidate)
            break

    # JSON: subdirectories, match by stem or "timings" in name
    if not json_path:
        for json_file in sorted(parent.rglob("*.json")):
            if json_file.parent == parent:
                continue
            if (json_file.stem == base or "timings" in json_file.name.lower()) \
                    and load_transcript(json_file):
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


def load_transcript(path) -> dict | None:
    """A timed transcript read off disk, or None if that file is not one.

    Not everything named `<track>.json` beside a track is a transcript. A
    transcription's own export (MacWhisper writes a bare list of
    {"text", "timestamp"}) is an ANCHOR file: it exists to be aligned against,
    not to be played from. Mistaking one for a transcript silently drops playback
    onto the evenly-paced fallback, which starts about right and drifts further
    the longer the track runs, so the shape is checked.
    """
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get('segments'), list) and not data.get('word_segments'):
        return None
    return data


def _parse_word_timings_json(json_path: str) -> list[dict]:
    """Return the flat list of spoken word timings.

    Prefers a top-level ``word_segments`` list (WhisperX / the lyrics-editor
    export, already spoken-only); otherwise flattens ``segments[].words``.  The
    editor's enriched transcripts add ``kind: stage_dir`` / ``dead_air`` segments
    that carry no spoken words; those are skipped so the timing stream stays
    purely the spoken words regardless of which format the file is in."""
    data = load_transcript(json_path)
    if not data:
        return []

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
    what playback shows.  The MD script is the shared authority instead:
    `_chunks_from_segments` re-derives the speakers and directions from it on every
    load.  Returns None when no enriched transcript exists."""
    _, json_path = _find_timing_files_for_audio(audio_path)
    parent = Path(audio_path).parent
    candidates: list[Path] = []
    # Prefer any enriched transcript files that are NOT the editor's working
    # copy (.sync.json): that is a WIP save and should not be
    # treated as the authoritative enriched transcript for playback. Look for
    # other JSON files that contain editor `kind` beats instead.
    if json_path:
        candidates.append(Path(json_path))                       # transcript.json (preferred)
    # Search subdirectories too, but only for files that BELONG to this track.
    # A whole-library folder holds one transcript per episode, so a scan that takes
    # any JSON it finds will happily merge another episode's silences and stage
    # directions into this one the moment this one's own file is missing. A
    # transcript is this track's because it is named after it, not because it is
    # nearby; a bare `transcript.json` is the single-track folder convention.
    base = Path(audio_path).stem
    for p in sorted(parent.rglob('*.json')):
        if (json_path and p == Path(json_path)) or p.name.endswith('.sync.json'):
            continue
        if p.stem != base and p.name != 'transcript.json':
            continue
        candidates.append(p)
    seen: set[str] = set()
    for c in candidates:
        cs = str(c)
        if cs in seen or not c.exists():
            continue
        seen.add(cs)
        data = load_transcript(c)
        if not data:
            continue
        if any(s.get('kind') in ('dead_air', 'stage_dir') for s in data.get('segments', [])):
            return cs
    return None


def _parse_air_beats(json_path: str) -> list[tuple[float, float]]:
    """Timed silence windows (start, end) from an enriched transcript's `dead_air`
    segments: the editor's explicit silences, in absolute track time."""
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
    transcript's `stage_dir` segments; preserves the editor's explicit
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


_matchable = _lt.matchable


def _match_md_to_timings(md_lines: list[DialogueLine], word_timings: list[dict], json_segments: list[dict] | None = None) -> None:
    """Walk each markdown line's words against the word_timings stream in order,
    stamping line.start/line.end from the first/last matched word."""
    # If the original transcript.json carries segment boundaries, prefer
    # assigning an MD line the start/end of the transcript segment that
    # contains most of its words. This anchors MD→timings to the user's
    # Whisper segments and reduces mis-association of repeated words.
    if json_segments:
        # Precompute normalised word sets for segments
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


# The least time a stage direction's own beat is given, however short its note.
_SD_MIN_GAP = 0.35
_SD_READ_FRACTION = tune.LYRIC_SD_READ_FRACTION


def _read_need(reading_time: float) -> float:
    """How long a direction's own beat is given to be read."""
    return max(_SD_MIN_GAP, reading_time * _SD_READ_FRACTION)


def _chunks_from_segments(segs: list[dict], md_path: str, track_duration: float = 0.0,
                          kinds: dict | None = None) -> tuple[list[dict], list[tuple[float, float]]]:
    """Build playback chunks from a timed transcript, overlaid with the MD script.

    The transcript owns the timing; the MD owns everything the timing cannot
    carry: who is speaking, the script's own punctuation and capitalisation, its
    markdown emphasis, and the stage directions.  Both are joined by
    `md_overlay.build_md_overlay`, the SAME single word-level alignment the lyrics
    editor renders from, so the player and the editor can never disagree about a
    line's speaker, text or directions.

    Every direction is a beat of its own, in the silence it sits in, borrowing
    from the beat before when that silence is too short to read it (`_flush`).
    Any silence still unclaimed and longer than `_AIR_THRESHOLD` becomes a dead-air
    beat, so no line is left on screen, or shown early, while nobody is speaking.
    `track_duration` extends that to the run-out after the last line; 0 means
    unknown, which leaves the run-out alone.

    A direction in the middle of a line splits the line there, its halves the
    same speaker's. An action between them is placed like any other direction,
    in the pause if it holds one and is that speaker's; a tone note becomes the
    aside of the rest of the line. `kinds` is the editor's tone/action choices.

    Returns (chunks, line_times) in the shape `lyric_pane.from_chunks` expects.
    """
    overlay, quality, links = build_md_overlay(segs, md_path, kinds)

    ov_map: dict[int, list] = {}
    cuts: dict[int, list] = {}            # seg → [(k, gap_from, at, text, tone, line)]: splits inside it
    for ov in overlay:
        if ov.get('within'):
            for si, k, gap_from, at in ov['within']:
                cuts.setdefault(si, []).append((k, gap_from, at, ov['text'].strip(), ov.get('tone'), ov.get('line'), ov.get('back')))
        else:
            ov_map.setdefault(ov['before_si'], []).append(ov)

    # Who speaks each MD line, and the line's own aside.  Attribution is looked up
    # per segment through `links` (the alignment's seg → MD line pin) rather than
    # carried forward from the last banner: a segment the MD does not explain gets
    # no speaker, instead of inheriting whoever spoke last for the rest of the
    # track.  Every segment of a line carries its speaker and aside, and its
    # `line`; the pane names them once however many segments are on screen.
    speaker_of_line: dict[int, tuple[str, str]] = {}
    for ov in overlay:
        if ov['kind'] == 'speaker' and ov.get('line') is not None:
            speaker_of_line[ov['line']] = (ov['text'], ov.get('stage', '') or '')
    for ov in overlay:                    # a line with no words, `**MARTIN**: *(screams)*`, is still his
        if ov['kind'] == 'stage_dir' and ov.get('line') is not None and ov.get('speaker'):
            speaker_of_line.setdefault(ov['line'], (ov['speaker'], ''))

    chunks: list[dict] = []
    times: list[tuple[float, float]] = []
    pending: list[tuple[str, int | None]] = []   # (text, line) awaiting a place to show
    tone_of_line: dict[int, str] = {}     # a line's aside since a mid-line tone note

    def _aside(line) -> str:
        """The manner the line's words are spoken in at this point."""
        return tone_of_line.get(line, speaker_of_line.get(line, ('', ''))[1]) if line is not None else ''

    held_to = [0.0]                       # a direction's beat runs on into the next to here

    def _emit(chunk: dict, window: tuple[float, float]) -> None:
        """Append a chunk and its time window, starting it late if the direction
        before borrowed from it (never more than half of it)."""
        start, end = window
        if held_to[0] > start:
            start = min(held_to[0], start + (end - start) / 2)
            times[-1] = (times[-1][0], start)
            held_to[0] = 0.0
        chunks.append(chunk)
        times.append((start, end))

    def _prev_end() -> float:
        """End of the last emitted window (0.0 before anything is emitted)."""
        return times[-1][1] if times else 0.0

    def _stage(text: str, line: int | None = None) -> dict:
        """A direction shown as its own beat: a mid-line one (`line`) as its
        speaker's, between the halves of what they say."""
        owner = {} if line is None else {'line': line, 'speaker': speaker_of_line.get(line, ('', ''))[0],
                                         'aside': _aside(line)}
        return {'parent_idx': -1, 'speaker': '', 'stage_dir': text,
                'text': '', 'is_stage': True, 'is_air': False, **owner}

    def _flush(next_start: float | None) -> None:
        """Give each queued direction a beat of its own before `next_start`.

        A direction is never part of another beat. It takes the silence ahead
        of it, shared out by reading time so a long note gets longer, and when
        that's too short to read it borrows the rest from the end of the beat
        before, never more than half of it, as the lyric pane's run-up does,
        and then from the start of the beat after.
        """
        nonlocal pending
        if not pending:
            return
        held, pending = pending, []
        weights = [_reading_time(text) for text, _line in held]
        t = _prev_end()
        if next_start is None:                       # nothing follows: show them out
            next_start = t + sum(weights)
        want = _read_need(sum(weights))
        short = want - (next_start - t)
        if short > 0 and times:
            s0, e0 = times[-1]
            t = e0 - min(short, (e0 - s0) / 2)
            times[-1] = (s0, t)
        end = max(next_start, t + want)               # still short: into the beat after
        total, t0 = sum(weights) or 1.0, t
        for (text, line), w in zip(held, weights):
            span = (end - t0) * (w / total)
            _emit(_stage(text, line), (t, t + span))
            t += span
        held_to[0] = end if end > next_start else 0.0

    def _air_if_silent(next_start: float) -> None:
        """Blank the display through silence that nothing else claimed.

        The lyric pane tiles the beats (lyric_pane._tile / handover), so a moment
        in a gap belongs to the beat before it, and without this the line just
        gone stays on screen for the whole pause.  A dead-air beat holds
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
                   'text': '', 'is_stage': False, 'is_air': True},
                  (start, next_start))

    def _tone_back(line: int, text: str) -> bool:
        """A tone note about the words before it (ending its line, or on how
        "it" was said) becomes their aside, back to the line's start or its
        last tone change, after the header's own note if there is one. Words
        after it are spoken as before. False when no words of the line come
        before it (a line that is only the note), to show it as a beat."""
        cur = _aside(line)
        new = text if line in tone_of_line or not cur else f"{cur}; {text}"
        said = False
        for c in reversed(chunks):
            if c['is_air']:
                continue
            if c.get('line') != line or (not c['is_stage'] and c['stage_dir'] != cur):
                break
            c['stage_dir' if not c['is_stage'] else 'aside'] = new
            said = said or not c['is_stage']
        return said

    def _queue(ov: dict) -> None:
        """A direction met at a segment boundary: a tone note sets the manner of
        the rest of its line, or of the line it ends; anything else waits for a
        place to show."""
        text = ov['text'].strip()
        if ov.get('tone') and ov.get('back'):
            if not _tone_back(ov['line'], text):
                pending.append((text, ov.get('line')))
        elif ov.get('tone'):
            tone_of_line[ov['line']] = text
        else:
            pending.append((text, ov.get('line')))

    for si, seg in enumerate(segs):
        for ov in ov_map.get(si, []):
            if ov['kind'] == 'stage_dir':
                _queue(ov)

        s_start = seg.get('start')
        s_end = seg.get('end')
        kind = seg.get('kind')

        if s_start is None or s_end is None:
            # Untimed beat: keep it out of the timeline rather than parking the
            # display on a zero-length window.  Anything queued stays queued, to
            # be placed against the next segment that does carry a time.
            continue
        window = (float(s_start), float(s_end))

        # Directions get first claim on a silence; whatever is left goes dead.
        _flush(float(s_start))
        _air_if_silent(float(s_start))

        if kind == 'dead_air':
            _emit({**_stage(''), 'parent_idx': si, 'is_stage': False, 'is_air': True}, window)
        elif kind == 'stage_dir':
            label = seg.get('text', '').strip().strip('()')
            line = links.get(si)
            scope = seg.get('scope')
            tone = scope == 'tone' if scope else _lt.direction_kind(label, kinds) == 'tone'
            # Tone only with more of its line still to say, as for one not committed.
            said_next = next((links.get(j) for j in range(si + 1, len(segs))
                              if segs[j].get('kind') not in ('stage_dir', 'dead_air')), None)
            back = said_next != line or _lt.points_back(label)
            if line is not None and tone and back and _tone_back(line, label):
                continue                        # committed, about the words before it
            if line is not None and tone and said_next == line:
                tone_of_line[line] = label      # committed mid-line tone note: the manner from here
                continue
            if window[1] - window[0] < _read_need(_reading_time(label)):   # too short to read: placed like any other
                pending.append((label, line))
                continue
            _emit({**_stage(label, line), 'parent_idx': si}, window)
        else:
            mq = quality.get(si) or {}
            text = (mq.get('md_text') or seg.get('text', '')).strip()
            line = links.get(si)
            speaker = speaker_of_line.get(line, ('', ''))[0] if line is not None else ''

            def _say(words: str, part: tuple[float, float]) -> None:
                # `stage_dir` is the aside, the manner the words are spoken in.
                _emit({'parent_idx': si, 'line': -1 if line is None else line,
                       'speaker': speaker, 'stage_dir': _aside(line),
                       'text': words, 'is_stage': False, 'is_air': False}, part)

            words, k0, t0 = text.split(), 0, window[0]
            for k, gap_from, at, dtext, tone, dline, back in sorted(cuts.get(si, [])):
                if k > k0:
                    _say(" ".join(words[k0:k]), (t0, max(t0, gap_from)))
                    k0, t0 = k, max(t0, gap_from)
                _queue({'text': dtext, 'tone': tone, 'line': dline, 'back': back})
                _flush(max(t0, at))
                _air_if_silent(max(t0, at))
                t0 = max(t0, at)
            _say(" ".join(words[k0:]), (t0, window[1]))

    for ov in ov_map.get(len(segs), []):             # trailing overlay items
        if ov['kind'] == 'stage_dir':
            _queue(ov)
    _flush(None)
    if track_duration > 0:
        _air_if_silent(float(track_duration))        # the run-out after the last line

    # The gaps between beats are left as they are: the pane tiles the timeline
    # (lyric_pane.handover), once.
    return chunks, times


class DialoguePlaybackState:
    """Manages sentence-by-sentence narrative dialogue states with timing support."""

    def __init__(self, audio_path: str, track_duration: float):
        """Load and expand the dialogue track for audio_path: find markdown + timing
        files, parse dialogue lines, match them to word timings, expand into timed
        sentence chunks, and merge in explicit dead-air beats; sets load_error on failure."""
        md_path = _find_markdown_for_audio(audio_path)
        _, json_path = _find_timing_files_for_audio(audio_path)
        # Kept so the player can NAME what it read. Resolved paths, not candidates:
        # the point of showing them is to catch the case where what was opened is
        # not what was meant.
        self.md_path = md_path
        self.json_path = json_path

        raw_dialogue_lines = None
        word_timings = None
        self.expanded_chunks: list[dict] = []
        self.line_times: list[tuple[float, float]] = []
        self.load_error = None
        # Where the timing came from, so playback can say so rather than look
        # confident. 'transcript' is measured; 'estimated' is the script paced
        # across the track, which starts about right and drifts all the way to the
        # middle, the failure that is impossible to spot from a still screen.
        self.timing_source = 'none'
        loaded_from_segments = False  # Track if we already have a full segmentation

        # Diagnostic: report if files aren't found
        if not md_path:
            self.load_error = f"No markdown dialogue file found near {audio_path}"
        if not json_path:
            if self.load_error:
                self.load_error += "; no timing file found"
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
                                    json_segments, md_path, track_duration, jcontainer.get('direction_kinds'))
                                self.load_error = None
                                self.timing_source = 'transcript'
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
            dynamic_wps = total_words / track_duration if track_duration > 0 else tune.LYRIC_FALLBACK_WPS
            self.expanded_chunks, self.line_times = expand_dialogue_into_sentences(
                raw_dialogue_lines,
                words_per_second=dynamic_wps,
                word_timings=word_timings,
                wrap_w=50
            )
            self.timing_source = 'transcript' if word_timings else 'estimated'
            self._merge_air_beats(audio_path)

    def _merge_air_beats(self, audio_path: str) -> None:
        """Fold the explicit dead-air and stage-direction beats from an enriched
        transcript (never the .sync.json) into the timeline, wherever they land in
        a gap nothing else covers. Dead air draws as a blank row."""
        enriched = _find_enriched_transcript(audio_path)
        if not enriched:
            return
        for a_s, a_e in _parse_air_beats(enriched):
            if any(not (e <= a_s or s >= a_e) for (s, e) in self.line_times):
                continue   # overlaps existing content: leave it be
            pos = bisect.bisect_left([s for (s, _) in self.line_times], a_s)
            self.line_times.insert(pos, (a_s, a_e))
            self.expanded_chunks.insert(pos, {
                'parent_idx': -1, 'speaker': '', 'stage_dir': '',
                'text': '', 'is_stage': False, 'is_air': True,
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
                'text': '', 'is_stage': True, 'is_air': False,
            })

    def is_active(self) -> bool:
        """True if any dialogue chunks were loaded."""
        return bool(self.expanded_chunks)

