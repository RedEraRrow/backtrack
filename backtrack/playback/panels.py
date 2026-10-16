"""What the player's newer panels show: one function a panel, from the tags,
the file, the listening history or the track's sound.

Two kinds. Most are drawn with the rest of the frame (Credits, Info, Notes,
Booklet). The Visualiser changes from moment to moment, so `Live` repaints it
every tick, only the rows that changed, the way the lyric pane does; it's a
pure function of (time, size).

`has` says which a track has anything for, for `w`'s list and the layout.
"""
from __future__ import annotations

import functools
import hashlib
import os

from backbone import ui
from backbone.prompt import core as _pc
from backbone.ui import Colors as C

# Credits in their own tags, not TIPL/TMCL: (frame, role).
CREDIT_FRAMES = (('TCOM', 'composer'), ('TEXT', 'lyricist'), ('TPE3', 'conductor'),
                 ('TPE4', 'remixer'), ('TOPE', 'original artist'), ('TOLY', 'original lyricist'))

VIS_STYLES = ('bars', 'scope', 'meters')


def _frame_texts(audio, frame_id: str) -> list[str]:
    out = []
    for fr in (audio.getall(frame_id) if audio else []):
        out += [str(t).strip() for t in getattr(fr, 'text', []) if str(t).strip()]
    return out


def tag_credits(audio) -> list[tuple[str, str]]:
    """The people credited in their own tags (composer, lyricist and the rest),
    as (role, name), each name once a role."""
    out = []
    for frame_id, role in CREDIT_FRAMES:
        for name in _frame_texts(audio, frame_id):
            if (role, name) not in out:
                out.append((role, name))
    return out


def lines_of(pairs: list, width: int, rows: int) -> list[str]:
    """(label, value) pairs as rows of a panel: the value, its label dim before
    it, wrapping under itself when it's long; a None label is a heading, '' a
    blank row. Cut to `rows`."""
    label_w = min(max((ui.visual_len(k) for k, v in pairs if k), default=0), max(6, width // 3))
    out = []
    for k, v in pairs:
        if k is None:
            out.append(f"{C.HEADING}{ui.truncate_text(v, width)}{C.RESET}")
        elif not k and not v:
            out.append("")
        else:
            room = max(1, width - label_w - 2)
            label = f"{C.DIM}{ui.truncate_text(k, label_w):<{label_w}}{C.RESET}  "
            from backtrack.lyrics.formats import _md_rows
            for n, part in enumerate(_md_rows(v, room) or [""]):            # a long value wraps under itself
                out.append((label if n == 0 else " " * (label_w + 2)) + part)
    if len(out) > rows:
        out = out[:max(0, rows - 1)] + [f"{C.DIM}…{C.RESET}"]
    return out


# --- Info --------------------------------------------------------------------

def _copyright(text: str) -> str:
    low = text.lower()
    return text if low.startswith(('©', '℗', '(c)', '(p)', 'copyright')) else f"© {text}"


@functools.lru_cache(maxsize=8)
def _plays(path: str, _history_stamp: float, before: float) -> tuple[int, str]:
    """Times played, and when the last play before `before` (this one's start)
    began, to the minute, from the listening history. An entry is written as a
    play ends, stamped then, with how long it lasted."""
    import datetime
    from backtrack.history import get_history
    n, last = 0, ''
    for ts, listened, p in get_history(limit=10 ** 9):
        if p != path:
            continue
        n += 1
        try:
            ended = datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if not last and ended.timestamp() < before:
            began = ended - datetime.timedelta(seconds=int(str(listened).rstrip('s') or 0))
            last = began.strftime("%Y-%m-%d %H:%M")
    return n, last


def info_pairs(audio, path: str, started: float = 0.0) -> list:
    """The Info panel: the release (copyright, label, dates, ISRC), the file
    (format, quality, loudness, size) and its plays."""
    pairs: list = []
    release = [("Copyright", _copyright(v)) for v in _frame_texts(audio, 'TCOP')]
    release += [("Label", v) for v in _frame_texts(audio, 'TPUB')]
    released = _frame_texts(audio, 'TDRL') or _frame_texts(audio, 'TDRC')
    release += [("Released", released[0])] if released else []
    first = _frame_texts(audio, 'TDOR') or _frame_texts(audio, 'TORY')
    release += [("First out", first[0])] if first and first[:1] != released[:1] else []
    release += [("ISRC", v) for v in _frame_texts(audio, 'TSRC')]
    release += [("Web", str(f.url)) for f in (audio.getall('WCOP') if audio else [])]
    if release:
        pairs += [(None, "Release")] + release + [("", "")]

    from backtrack.music_library import library_entry
    e = library_entry(path) or {}
    quality = " · ".join(x for x in (
        f"{int(e['bitrate'])} kbps" if e.get('bitrate') else "",
        f"{float(e['sample_rate']) / 1000:g} kHz" if e.get('sample_rate') else "",
        f"{e['bit_depth']}-bit" if e.get('bit_depth') else "",
        {1: "mono", 2: "stereo"}.get(e.get('channels'), f"{e['channels']} channels" if e.get('channels') else ""),
    ) if x)
    pairs += [(None, "File"), ("Format", str(e.get('format') or os.path.splitext(path)[1][1:]).upper())]
    pairs += [("Quality", quality)] if quality else []
    gain = _frame_texts(audio, 'TXXX:REPLAYGAIN_TRACK_GAIN') if audio else []
    pairs += [("ReplayGain", gain[0])] if gain else []
    from backtrack.history import HISTORY_FILE
    try:
        pairs.append(("Size", f"{os.path.getsize(path) / 1e6:.1f} MB"))
    except OSError:
        pass
    try:
        stamp = os.path.getmtime(HISTORY_FILE)
    except OSError:
        stamp = 0.0
    n, last = _plays(path, stamp, started or float('inf'))
    pairs += [("", ""), (None, "Plays"), ("Played", ui.plural(n, 'time')), *([("Last", last)] if last else [])]
    return pairs


# --- Notes -------------------------------------------------------------------

def notes(audio) -> list[tuple[str, str]]:
    """The track's comments as (description, text), one a description, in the
    order they're stored: iTunes' bookkeeping ones left out, and of the same
    comment in several languages, the system's, else English, else
    unspecified (xxx), else the first."""
    from backtrack.id3.languages import system_codes
    rank = {code: i for i, code in enumerate((*system_codes(), 'eng', 'xxx'))}
    best: dict = {}
    for fr in (audio.getall('COMM') if audio else []):
        desc = str(fr.desc).strip()
        text = "\n".join(str(t) for t in fr.text).strip()
        if not text or desc.lower().startswith('itun'):
            continue
        r = rank.get(str(fr.lang).lower(), len(rank))
        if desc not in best or r < best[desc][0]:
            best[desc] = (r, text)
    out = []
    for desc, (_r, text) in best.items():
        if all(text != t for _d, t in out):
            out.append((desc, text))
    return out


def notes_lines(audio, width: int, rows: int) -> list[str]:
    """The Notes panel: each comment, headed with its description when there's
    more than one."""
    from backtrack.lyrics.formats import _md_rows
    items, out = notes(audio), []
    for n, (desc, text) in enumerate(items):
        if len(items) > 1:
            out += ([""] if n else []) + [f"{C.HEADING}{ui.truncate_text(desc or 'Comment', width)}{C.RESET}"]
        for para in text.split("\n"):
            out += _md_rows(para, width) or [""]
    return out[:rows]


# --- Booklet -----------------------------------------------------------------

_LEAFLET = 5                     # ID3's picture type for a leaflet page


def _booklet_dir(path: str) -> str | None:
    """The album's `booklet` folder: beside the track, or beside its disc folder."""
    from backtrack.id3.cover_matcher import is_disc_dir
    here = os.path.dirname(os.path.abspath(path))
    for base in (here, os.path.dirname(here)) if is_disc_dir(os.path.basename(here)) else (here,):
        try:
            hit = next((e for e in os.listdir(base) if e.lower() == 'booklet'
                        and os.path.isdir(os.path.join(base, e))), None)
        except OSError:
            continue
        if hit:
            return os.path.join(base, hit)
    return None


@functools.lru_cache(maxsize=8)
def _pages(path: str, _mtime: float) -> tuple:
    from backtrack.album_art import picture_file
    from backtrack.id3.cover_matcher import IMAGE_EXTS
    from backtrack.id3.tag_formats import embedded_pictures
    pages, seen = [], set()
    for data, ptype, desc in embedded_pictures(path):
        if ptype != _LEAFLET and not str(desc).lower().startswith('booklet'):
            continue
        h = hashlib.sha1(data).hexdigest()
        if h not in seen and (f := picture_file(data)):
            seen.add(h)
            pages.append(f)
    folder = _booklet_dir(path)
    if folder:
        from backtrack.music_library import _natural
        pages += [os.path.join(folder, e) for e in sorted(os.listdir(folder), key=lambda e: _natural(e.lower()))
                  if os.path.splitext(e)[1].lower() in IMAGE_EXTS and not e.startswith('.')]
    return tuple(pages)


def booklet_pages(path: str) -> list[str]:
    """The booklet's pages, as image files: the pictures embedded as booklet
    pages (described "Booklet…" or typed leaflet page), then the images in the
    album's `booklet` folder, in name order."""
    try:
        return list(_pages(path, os.path.getmtime(path)))
    except OSError:
        return []


def page_box(page: str, width: int, rows: int) -> tuple[int, int]:
    """The cells a page fits in, its own shape, within width × rows."""
    from PIL import Image
    try:
        with Image.open(page) as im:
            w, h = im.size
    except Exception:
        return width, rows
    aspect = ui.cell_aspect()
    cols = min(width, round(rows * aspect * w / max(1, h)))
    return max(1, cols), max(1, min(rows, round(cols / aspect * h / max(1, w))))


def page_lines(page: str, cols: int, rows: int) -> tuple[list[str], str]:
    """A page drawn in cols × rows cells: as an image where the terminal shows
    them (the cells under it, and the escape that draws it), else as text art."""
    from backtrack.playback.player_art import fill_art, image_cells
    shown = image_cells(page, cols, rows)
    if shown:
        return shown
    return fill_art(page, cols, rows), ''


# --- which a track has ---------------------------------------------------------

def has(audio, path: str) -> dict:
    """The newer panels this track has anything for."""
    from backtrack.playback import sound
    return {'credits': bool(tag_credits(audio)), 'info': bool(path), 'notes': bool(notes(audio)),
            'booklet': bool(path and booklet_pages(path)), 'visualiser': bool(path) and sound.available()}


# --- the moving ones ---------------------------------------------------------

_EIGHTHS = " ▁▂▃▄▅▆▇█"


def _note(text: str, width: int, rows: int) -> list[str]:
    """A dim line in the middle of a panel."""
    return [""] * (rows // 2) + [f"{C.DIM}{ui.truncate_text(text, width).center(width)}{C.RESET}"]


def visualiser_lines(snd, t: float, width: int, rows: int, style: str, state: dict) -> list[str]:
    if snd is None:
        return _note("Needs ffmpeg", width, rows)
    i = snd.frame(t)
    if i is None:
        return _note(f"Listening… {snd.progress:.0%}", width, rows)
    if style == 'meters':
        return _meters(snd.levels[i], width, rows, state)
    if style == 'scope':
        return _scope(snd.wave(t, min(4096, width * 2 * 8)), width, rows)
    return _bars(snd.bands[i], width, rows, t, state)


def _bars(bands, width: int, rows: int, t: float, state: dict) -> list[str]:
    """Spectrum bars, one a column, standing on a baseline: each smoothed with
    its neighbours so none stands alone, rising at once and falling back slowly."""
    import numpy as np
    x = np.linspace(0, len(bands) - 1, width)
    v = np.interp(x, np.arange(len(bands)), bands.astype(float)) / 255.0
    v = np.convolve(np.pad(v, 2, mode='edge'), np.array([1, 2, 3, 2, 1]) / 9.0, mode='valid')
    v = np.clip((v - 0.15) / 0.5, 0, 1) ** 1.2                    # loud speech reaches near the top
    h = v * rows * 8                                             # in eighths of a row
    shown = state.get('h')
    if shown is not None and len(shown) == width and t >= state.get('t', t):
        dt = max(0.0, min(0.5, t - state['t']))
        h = np.maximum(h, shown - dt * rows * 8 * 1.5)           # falls back over about two thirds of a second
    state.update(h=h, t=t)
    out = []
    for r in range(rows):
        base = (rows - 1 - r) * 8
        colour = C.ACCENT2 if r < rows // 3 else C.ACCENT
        cells = []
        for c in range(width):
            fill = h[c] - base
            if fill >= 8:
                cells.append(f"{colour}█")
            elif fill >= 1:
                cells.append(f"{colour}{_EIGHTHS[int(fill)]}")
            elif r == rows - 1:
                cells.append(f"{C.DIM}▁")                       # the baseline
            else:
                cells.append(" ")
        out.append("".join(cells) + C.RESET)
    return out


def _scope(wave, width: int, rows: int) -> list[str]:
    """The waveform as a line in braille dots."""
    if wave is None or not len(wave):
        return [""] * rows
    import numpy as np
    dots_w, dots_h = width * 2, rows * 4
    idx = np.linspace(0, len(wave) - 1, dots_w).astype(int)
    ys = np.clip(((wave[idx].astype(float) / 127.0) * 0.9 + 1) / 2 * (dots_h - 1), 0, dots_h - 1).astype(int)
    grid = [[0] * width for _ in range(rows)]
    bit = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))
    prev = ys[0]
    for x, y in enumerate(ys):
        for yy in range(min(prev, y), max(prev, y) + 1):            # joined, so a steep edge stays a line
            row = dots_h - 1 - yy
            grid[row // 4][x // 2] |= bit[row % 4][x % 2]
        prev = y
    return [f"{C.ACCENT}" + "".join(chr(0x2800 + b) if b else " " for b in line) + C.RESET for line in grid]


def _meters(levels, width: int, rows: int, state: dict) -> list[str]:
    """Left and right level meters, the loudness filled and the peak held a moment."""
    bar_w = max(4, width - 8)
    held = state.setdefault('held', [0, 0])
    out = []
    for side, (rms, peak) in enumerate(((levels[0], levels[2]), (levels[1], levels[3]))):
        held[side] = max(int(peak), held[side] - 3)
        fill = int(rms) * bar_w // 255
        mark = min(bar_w - 1, held[side] * bar_w // 255)
        hot = bar_w * 9 // 10                                         # the last tenth: loud
        line = "".join(f"{C.ACCENT2}│" if k == mark and k >= fill
                       else f"{C.ACCENT2 if k >= hot else C.ACCENT}█" if k < fill
                       else f"{C.DIM}·" for k in range(bar_w))
        db = int(peak) * 70 // 255 - 70
        out.append(f"{C.DIM}{'LR'[side]}{C.RESET} {line}{C.RESET} {C.DIM}{db:>4}{C.RESET}")
    if rows >= 4:
        scale = [" "] * bar_w
        for d in (-60, -40, -20, -10, 0):
            pos = min(bar_w - 3, (d + 70) * bar_w // 70)
            for k, ch in enumerate(str(d)):
                if pos + k < bar_w:
                    scale[pos + k] = ch
        out.append(f"  {C.DIM}{''.join(scale)}{C.RESET}")
    top = max(0, (rows - len(out)) // 2)
    return [""] * top + out


class Live:
    """Repaints the moving panels each tick: only the rows that changed, so a
    still frame costs nothing. Its regions come from the last full redraw."""

    def __init__(self):
        self.regions: dict = {}      # kind → (row, col, width, rows)
        self.shown: dict = {}        # kind → the lines on screen
        self.state: dict = {}        # kind → what a panel keeps between frames (falling peaks)

    def place(self, regions: dict) -> None:
        self.regions = dict(regions)
        self.shown = {}

    def paint(self, out, t: float, ctx: dict, force: bool = False) -> None:
        if force:
            self.shown = {}
        for kind, (row, col, width, rows) in self.regions.items():
            if kind != 'visualiser':
                continue
            lines = visualiser_lines(ctx.get('sound'), t, width, rows, ctx.get('style', 'bars'),
                                     self.state.setdefault(kind, {}))
            lines = (lines + [""] * rows)[:rows]
            old = self.shown.get(kind, [])
            for k, line in enumerate(lines):
                if k < len(old) and old[k] == line:
                    continue
                line = ui.clip_ansi(line, width)
                out.write(_pc.screen_span_paint(row + k, col, line + " " * max(0, width - ui.visual_len(line))))
            self.shown[kind] = lines


LIVE = Live()
