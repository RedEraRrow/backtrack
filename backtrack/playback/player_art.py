"""The player's album art: the text (half-block) art sized to fit, and on an
image-capable terminal (iTerm2, opt-in) the real image drawn over those cells:
average colour, then a quick preview, then full quality."""
from __future__ import annotations
import functools
import math
import os
import sys
from io import BytesIO

from PIL import Image, ImageStat
from backbone import ui
from backbone.prompt import core as pc
from backtrack.playback.player_geom import geom
from backbone.log import log
from backtrack.album_art import booklet_file, decode_image, fit_art, get_art, get_art_bytes, get_embedded_art, half_blocks, has_alpha
from backtrack.config import setting
from backtrack import tuning as tune


ART_MAX_WIDTH = 200  # viu rendering degrades above this width on most terminals


# The empty player's note fills the full width when a square box would only be
# this many columns narrower, so it doesn't sit in thin, lopsided margins. Only
# the note: a cover is never cropped to reach full width (see _art_fit).
_ART_SNAP_TO_FULL = 4


def _kept(cache: dict, key, limit: int, make):
    """cache[key], made by `make()` the first time. Past `limit` entries the
    one used longest ago goes, so going back over what was just shown (a list
    scrolled up again) finds it still there."""
    if key in cache:
        cache[key] = cache.pop(key)          # now the most recently used
        return cache[key]
    if len(cache) >= limit:
        del cache[next(iter(cache))]
    cache[key] = made = make()
    return made


_art_cache: dict = {}


def _get_art_cached(file_path: str, width: int, booklet: bool = False) -> str:
    """Return the rendered album art for file_path at width, cached by (path, width, mtime).
    `booklet`: a grouping file's booklet image rather than its cover."""
    # Key on the file's mtime so editing the file (e.g. adding album art)
    # invalidates the cached render; otherwise a "No album art found." result
    # would stick until the program restarts.
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        mtime = 0.0
    key = (file_path, width, mtime, booklet, ui.cell_aspect())
    return _kept(_art_cache, key, 64,                 # every width a drag passes through
                 lambda: (get_embedded_art(file_path, width, preferred_desc='Booklet', preferred_type=6)
                          if booklet else get_art(file_path, width=width)))


# --- Real-image album art (iTerm2 inline images), opt-in -----------------------
# The half-block art still sizes the layout; in image mode its cells show the
# cover's average colour, then a preview, then the full image. The text art is
# only the fallback when there's no image. Every position the layout records
# (clicks, the volume bar, the lyric pane) is the same in both modes. Off unless the `art_inline_images` setting is on
# AND the terminal is iTerm2 (not inside tmux, which would swallow the image).
_inline_art: dict = {'path': None,       # set when this frame's art is an image
               'resizing': False,        # mid-resize: the preview only (see set_resizing)
               'incomplete': False}      # the full image was cut short by a resize


def set_resizing(on: bool) -> None:
    """While the window is still being resized the player redraws at every size
    it passes through; only the small preview image is sent for those, and the
    full-quality one once the size settles (redraw_art_image)."""
    _inline_art['resizing'] = on


def redraw_art_image() -> None:
    """Send the full-quality image again without repainting any rows: after a
    resize settles, when the rows are right and the preview is already showing,
    or after a resize cut the last send short."""
    _draw_inline_art(full_only=True)
    sys.stdout.flush()


def art_image_incomplete() -> bool:
    """Whether the last full-quality image was cut short and still needs sending."""
    return _inline_art['incomplete']


_inline_art_cache: dict = {}             # (path, mtime, cols, rows, px, cell) → (base64, byte count)


_INLINE_PX_PER_COL = tune.ART_INLINE_PX_PER_COL
_INLINE_CHUNK = tune.ART_INLINE_CHUNK
_INLINE_PREVIEW_PX = tune.ART_INLINE_PREVIEW_PX


_inline_cfg: dict = {}                   # the setting, cached on config.json's mtime


def inline_art_enabled() -> bool:
    """Whether art should be drawn as a real image here. Cheap to ask often
    (the now-playing box asks every second): the setting is re-read only when the
    config file changes."""
    if os.environ.get('TMUX'):
        return False
    if (os.environ.get('TERM_PROGRAM') != 'iTerm.app'
            and os.environ.get('LC_TERMINAL') != 'iTerm2'):
        return False
    from backtrack.config import CONFIG_FILE, load_config
    try:
        mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _inline_cfg.get('mtime', object()) != mtime:
        _inline_cfg.update(mtime=mtime, on=bool(setting(load_config(), 'art_inline_images')))
    return _inline_cfg['on']


_decoded_cache: dict = {}                # (path, mtime) → (raw bytes, decoded image or None)


_WORKING_PX = 1400                       # decoded covers are kept at most this big


def _cover_decoded(file_path: str) -> tuple | None:
    """The cover's raw bytes and decoded pixels, read and decoded once per file
    (a large cover takes a noticeable moment to decode, so do it once, not per
    colour, preview and full image). None when the file has no cover."""
    from backtrack.album_art import decode_image
    try:
        key = (file_path, os.path.getmtime(file_path))
    except OSError:
        return None
    def make():
        raw = get_art_bytes(file_path)
        img = decode_image(raw) if raw else None
        if img is not None:
            # A 4000-pixel cover is shrunk once, here, to a working size still
            # above anything the player shows; every later step works on that.
            img.thumbnail((_WORKING_PX, _WORKING_PX), Image.Resampling.BOX)
        return (raw, img) if raw else None
    return _kept(_decoded_cache, key, 8, make)       # decoded covers are large: keep a few


def _inline_art_data(file_path: str, cols: int = 0, rows: int = 0,
                     px: int = _INLINE_PX_PER_COL) -> tuple[str, int] | None:
    """The cover as base64 for the image escape, scaled to the cells it fills
    (cols × rows at `px` pixels per column) and cached per file and size, so a
    resize back to a size already seen costs nothing. The preview size is also
    saved at a lower quality: it's only on screen for a moment."""
    try:
        key = (file_path, os.path.getmtime(file_path), cols, rows, px, ui.cell_aspect())
    except OSError:
        return None
    def make():
        dec = _cover_decoded(file_path)
        return _encoded_picture(dec[0], dec[1], cols, rows, px) if dec else None
    return _kept(_inline_art_cache, key, 64, make)


def _encoded_picture(raw: bytes, img, cols: int, rows: int, px: int) -> tuple[str, int] | None:
    """A picture as base64 for the image escape (and its byte count): whole,
    scaled to fit the cells at `px` pixels a column (only ever down; the
    terminal keeps its shape and centres it), a PNG when it has transparency,
    else a JPEG. The raw bytes when there are no cells to fit."""
    import base64
    data = raw
    transparent = has_alpha(raw)
    if transparent:                                         # keep it: a JPEG has no transparency
        img = decode_image(raw, keep_alpha=True)
    if img is not None and cols and rows:
        f = min(cols * px / img.width, rows * ui.cell_aspect() * px / img.height)
        if f < 1:
            img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))),
                             Image.Resampling.BOX)
        buf = BytesIO()
        if transparent:
            img.save(buf, "PNG", compress_level=1)      # quick: it's sent once, not stored
        else:
            img.save(buf, "JPEG", quality=70 if px == _INLINE_PREVIEW_PX else 85)
        data = buf.getvalue()
    return (base64.b64encode(data).decode('ascii'), len(data)) if data else None


_mean_cache: dict = {}                   # (path, mtime) → (average colour, has transparency)


# Art-derived accents: colours as they are in the cover, the most prominent
# colourful ones (area first, colourfulness after), changed only as far as
# they must be to read on the terminal's background (ui.readable).
_ART_MIN_SATURATION = tune.ART_ACCENT_MIN_SATURATION
_ART_MIN_HUE_GAP = tune.ART_ACCENT_MIN_HUE_GAP
_ART_PALETTE = tune.ART_ACCENT_PALETTE
_accents_cache: dict = {}


def art_accents(file_path: str) -> tuple[str, str] | None:
    """Two accents ("#RRGGBB") from a file's cover, both colours that are in
    it: the most prominent colourful one, and the next of a clearly different
    hue, else the next of any (else a lighter or darker tone of the first).
    Each is only made as much lighter or darker as it has to be to read on the
    terminal's background. None when there's no cover or it's all greys."""
    import colorsys
    decoded = _cover_decoded(file_path)
    if not decoded or decoded[1] is None:
        return None
    back = ui.terminal_background()
    key = (file_path, id(decoded[1]), back)
    if key not in _accents_cache:
        if len(_accents_cache) > 32:
            _accents_cache.clear()
        small = decoded[1].convert("RGB")
        small.thumbnail((64, 64))
        q = small.quantize(colors=_ART_PALETTE)
        pal = q.getpalette() or []
        found = []
        for count, idx in q.getcolors() or []:
            rgb = tuple(pal[idx * 3:idx * 3 + 3])
            h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in rgb))
            if s >= _ART_MIN_SATURATION and 0.06 < l < 0.96:
                found.append((count * (0.35 + s), h, "#%02X%02X%02X" % rgb))
        if not found:
            _accents_cache[key] = None
        else:
            found.sort(reverse=True)
            _w, h1, first = found[0]
            gap = lambda h: min(abs(h - h1), 1 - abs(h - h1))   # noqa: E731
            other = next((f for f in found[1:] if gap(f[1]) >= _ART_MIN_HUE_GAP), None) or \
                next(iter(found[1:]), None)
            if other:
                second = other[2]
            else:                                            # one colour: a tone of it, further from the background
                hh, ll, ss = colorsys.rgb_to_hls(*(c / 255 for c in ui.parse_hex_colour(first)))
                ll = min(1.0, ll + 0.2) if not ui.background_is_light() else max(0.0, ll - 0.2)
                second = "#%02X%02X%02X" % tuple(round(v * 255) for v in colorsys.hls_to_rgb(hh, ll, ss))
            _accents_cache[key] = (ui.readable(first, back), ui.readable(second, back))
    return _accents_cache[key]


def _cover_mean(file_path: str) -> tuple[int, int, int] | None:
    """The cover's average colour (r, g, b), or None when it can't be decoded,
    in which case the text art is the fallback."""
    facts = _cover_facts(file_path)
    return facts[0] if facts else None


def _cover_facts(file_path: str) -> tuple | None:
    """(the cover's average colour, whether it has transparency, its width
    over its height), kept long after the decoded picture is let go: a list
    showing covers asks for them on every move."""
    try:
        key = (file_path, os.path.getmtime(file_path))
    except OSError:
        return None
    def make():
        dec = _cover_decoded(file_path)
        img = dec[1] if dec else None
        return None if img is None else (tuple(int(v) for v in ImageStat.Stat(img).mean[:3]), has_alpha(dec[0]),
                                         img.width / max(1, img.height))
    return _kept(_mean_cache, key, 256, make)


def _draw_inline_art(full_only: bool = False) -> None:
    """Draw this frame's image over its cells: the small preview, then the
    full-quality image, which replaces it once the terminal has decoded it
    (mid-resize, only the preview; at a settle, only the full one, as the preview
    is already there). Called after every frame's rows are written: a
    repainted row erases whatever image was under it."""
    path = _inline_art['path']
    if not (path and geom.art_top and geom.art_width and geom.art_height):
        pc.screen_picture_area('player art', None)
        return
    pc.screen_picture_area('player art', (geom.art_top, geom.art_left + 1, geom.art_height, geom.art_width),
                           redraw_art_image)          # sent again when a box over it goes
    if not full_only:
        _send_image(path, _INLINE_PREVIEW_PX)
    # The full-quality image waits for focus: a terminal reading a background
    # window's output slowly would hold the player on it (the track moving
    # on behind it); focus coming back redraws the player, and sends it.
    if not _inline_art['resizing'] and ui.window_focused():
        _inline_art['incomplete'] = not _send_image(path, _INLINE_PX_PER_COL, chunked=True)


def _image_head(size: int, cols: int, rows: int) -> str:
    """The start of an iTerm2 image escape filling cols × rows cells at the
    cursor, leaving the cursor where it was; the base64 data and BEL follow."""
    return (f"\033]1337;File=inline=1;size={size};width={cols};"
            f"height={rows};preserveAspectRatio=1;doNotMoveCursor=1:")


def cover_box(path: str, cols: int, rows: int, pre_art: str | None = None) -> tuple[int, int]:
    """The cells a picture takes within cols × rows: its own shape, as big as
    fits, so the whole of it shows with no gap. A placeholder (no picture, the
    note, a person) takes a square. A group's picture is its booklet's."""
    if pre_art is GROUP_COVER:
        path = booklet_file(path) or path
    facts = _cover_facts(path) if pre_art not in _SHAPES else None
    shape = facts[2] if facts else 1.0
    aspect = ui.cell_aspect()
    c = max(1, min(cols, round(rows * aspect * shape)))
    return c, max(1, min(rows, round(c / aspect / shape)))


_fill_cache: dict = {}


def _fit_cells(img, cols: int, rows: int):
    """`img` at cols × rows cells' half-block pixels: whole, fitted and
    centred on a clear background, never cut."""
    size = (cols, rows * 2)
    img = img.convert("RGBA")
    # A half-block pixel is half a cell high: the cells' own shape on screen.
    f = min(cols / img.width, rows * 2 * ui.cell_aspect() / 2 / img.height)
    w, h = max(1, round(img.width * f)), max(1, round(img.height * f * 2 / ui.cell_aspect()))
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(img.resize((min(w, cols), min(h, rows * 2)), Image.Resampling.BOX),
              ((cols - min(w, cols)) // 2, (rows * 2 - min(h, rows * 2)) // 2))
    return out


def fill_art(path: str, cols: int, rows: int) -> list[str]:
    """`path`'s picture in cols × rows cells, whole, as half-block text (made
    for cover_box's cells, it fills them); [] when there's no picture."""
    try:
        key = (path, os.path.getmtime(path), cols, rows, ui.cell_aspect())
    except OSError:
        return []
    def make():
        dec = _cover_decoded(path)
        img = (decode_image(dec[0], keep_alpha=True) if dec and has_alpha(dec[0])
               else dec[1].convert("RGBA") if dec and dec[1] is not None else None)
        return (half_blocks(_fit_cells(img, cols, rows)).splitlines()
                if img is not None and cols > 0 and rows > 0 else [])
    return _kept(_fill_cache, key, 64, make)


def boxed_art(path: str, cols: int, rows: int, pre_art: str | None) -> list[str]:
    """The art in cols × rows cells (cover_box's): the whole cover (an image
    where the terminal shows them, its cells standing under it, else text
    art); the empty player's note. A group shows its booklet."""
    _inline_art['path'] = None
    if pre_art in _SHAPES:
        return _shape_lines(pre_art, cols, rows)
    if pre_art is GROUP_COVER:
        path = booklet_file(path) or path
    lines = fill_art(path, cols, rows)
    if not lines:
        return _shape_lines(IDLE_ART, cols, rows)
    mean = _cover_mean(path) if inline_art_enabled() else None
    if mean:
        _inline_art['path'] = path
        return [f"\033[48;2;{mean[0]};{mean[1]};{mean[2]}m{' ' * cols}\033[0m"] * rows
    return lines


def image_cells(path: str, cols: int, rows: int) -> tuple[list[str], str] | None:
    """`path`'s picture as an image over cols × rows cells: (the cells, in the
    picture's average colour, to stand under it, and the escape that draws it).
    None when it isn't shown as an image here (the setting, the terminal) or
    there's no picture to show; the text art is then the fallback."""
    if not inline_art_enabled():
        return None
    facts = _cover_facts(path)
    data = _inline_art_data(path, cols, rows, _INLINE_PX_PER_COL) if facts else None
    if not data:
        return None
    # Under a picture with transparency (a round portrait) the cells stay
    # blank, or its corners would show them.
    mean, blank, _shape = facts
    cells = ([" " * cols] if blank else [f"\033[48;2;{mean[0]};{mean[1]};{mean[2]}m{' ' * cols}\033[0m"]) * rows
    return cells, _image_head(data[1], cols, rows) + data[0] + "\a"


def _send_image(path: str, px: int, chunked: bool = False) -> bool:
    """Write one image escape for the art's cells (cursor left where it was).

    `chunked`: the full image is big enough that writing it blocks for a
    noticeable time while the terminal takes it in, and a resize arriving then
    would have to wait, with the window showing the old frame rewrapped until it finished.
    So it goes out in pieces, and if the terminal reports a resize in between,
    the rest is dropped: the escape is closed early (the terminal discards a
    truncated image) and False returned, for the image to be sent again later.
    """
    data = _inline_art_data(path, geom.art_width, geom.art_height, px)
    if not data:
        return True
    b64, size = data
    head = (f"\0337\033[{geom.art_top};{(geom.art_left or 0) + 1}H"
            + _image_head(size, geom.art_width, geom.art_height))
    if not chunked:
        sys.stdout.write(head + b64 + "\a\0338")
        return True
    import time
    start = time.monotonic()
    sys.stdout.write(head)
    for i in range(0, len(b64), _INLINE_CHUNK):
        if ui.last_resize_signal_at() > start:
            sys.stdout.write("\a\0338")
            sys.stdout.flush()
            log.debug("art image cut short by a resize after %d of %d KB (%.0f ms)",
                      i // 1024, len(b64) // 1024, (time.monotonic() - start) * 1000)
            return False
        sys.stdout.write(b64[i:i + _INLINE_CHUNK])
        sys.stdout.flush()
    sys.stdout.write("\a\0338")
    sys.stdout.flush()
    log.debug("art image %sx%s cells: %d KB in %.0f ms", geom.art_width, geom.art_height,
              len(b64) // 1024, (time.monotonic() - start) * 1000)
    return True


# The empty player's art (#14): a beamed note drawn in braille at the size of
# the box a square cover takes, so it scales with the window. The layout swaps
# this marker for the drawing (see _art_fit).
IDLE_ART = "♫"
# A person with no picture of their own (an artist without an artist image),
# drawn the same way.
AVATAR_ART = "👤"
# A grouping (audiobook-style) file shows its booklet image instead of its cover.
GROUP_COVER = "booklet"
_NOTE_W, _NOTE_H = 56, 64                # the note's design grid, in braille dots
_NOTE_FILL = 0.6                         # share of the cover box the note spans
_BRAILLE_DOTS = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08),
                 (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80))


def _in_note(x: float, y: float) -> bool:
    """Whether design-grid point (x, y) is inked: two tilted heads, their stems
    and two slanted beams."""
    cos, sin = math.cos(math.radians(-25)), math.sin(math.radians(-25))
    for cx, cy in ((13, 52), (43, 46)):
        dx, dy = x - cx, y - cy
        u, v = dx * cos - dy * sin, dx * sin + dy * cos
        if (u / 10) ** 2 + (v / 6.5) ** 2 <= 1:
            return True
    if 20 <= x <= 23 and 10 <= y <= 50 or 50 <= x <= 53 and 4 <= y <= 44:
        return True
    return 20 <= x <= 53 and any(0 <= y - (10 - (x - 20) * 6 / 33 + off) <= 6 for off in (0, 10))


def _in_person(x: float, y: float) -> bool:
    """Whether design-grid point (x, y) is inked: a head and shoulders."""
    return (x - 28) ** 2 + (y - 20) ** 2 <= 11 ** 2 or (y <= 62 and ((x - 28) / 24) ** 2 + ((y - 62) / 22) ** 2 <= 1)


_SHAPES = {IDLE_ART: _in_note, AVATAR_ART: _in_person}


@functools.lru_cache(maxsize=16)
def _shape_lines(shape: str, max_w: int, avail_h: int) -> list[str]:
    """A placeholder (IDLE_ART's note, AVATAR_ART's person) centred in the box
    a square cover gets at this size (see _art_fit for the cover's sizing),
    scaled to it, so the empty player lays out like a playing one."""
    inked = _SHAPES[shape]
    h = max(1, min(max_w // 2, avail_h))
    w = max_w if max_w - 2 * h <= _ART_SNAP_TO_FULL else max(10, 2 * h)
    # Braille cells are 2x4 dots, about square on a 1:2 cell.
    scale = _NOTE_FILL * min(2 * w / _NOTE_W, 4 * h / _NOTE_H)
    x0, y0 = (2 * w - _NOTE_W * scale) / 2, (4 * h - _NOTE_H * scale) / 2
    rows = [" " * w] * h
    # Only the cells over the note's grid can be inked.
    c0, c1 = int(x0 // 2), min(w, int((x0 + _NOTE_W * scale) // 2) + 1)
    for r in range(int(y0 // 4), min(h, int((y0 + _NOTE_H * scale) // 4) + 1)):
        cells = []
        for c in range(c0, c1):
            mask = sum(bit for dx, dy, bit in _BRAILLE_DOTS
                       if inked((2 * c + dx + 0.5 - x0) / scale, (4 * r + dy + 0.5 - y0) / scale))
            cells.append(chr(0x2800 + mask) if mask else " ")
        rows[r] = " " * c0 + "".join(cells) + " " * (w - c1)
    return rows


def _art_width_for_height(file_path: str, max_w: int, avail_h: int,
                          pre_art: str | None) -> tuple[str, list[str]]:
    """The art for the layout (see _art_fit). In image mode its cells become the
    cover's average colour, to be drawn over by the image (_draw_inline_art);
    the text art stays only when there's no image to show: no cover, one that
    won't decode, or a group cover, which is a composite of several."""
    if pre_art is GROUP_COVER and booklet_file(file_path):
        file_path, pre_art = booklet_file(file_path), None
    art_str, lines = _art_fit(file_path, max_w, avail_h, pre_art)
    _inline_art['path'] = None
    mean = _cover_mean(file_path) if (lines and pre_art is None and inline_art_enabled()) else None
    if mean:
        w = max(ui.visual_len(l) for l in lines)
        lines = [f"\033[48;2;{mean[0]};{mean[1]};{mean[2]}m{' ' * w}\033[0m" for _ in lines]
        _inline_art['path'] = file_path
    return art_str, lines


def _art_fit(file_path: str, max_w: int, avail_h: int,
             pre_art: str | None) -> tuple[str, list[str]]:
    """The art at the widest width up to max_w that fits avail_h rows, through
    album_art.fit_art: narrowed to fit, never cropped or stretched. `pre_art` is
    IDLE_ART for the empty player's note (AVATAR_ART: a person), GROUP_COVER
    for a grouping file's booklet image, else None for the file's cover (or
    an image file's picture)."""
    if pre_art in _SHAPES:
        lines = _shape_lines(pre_art, max_w, avail_h)
    else:
        booklet = pre_art is GROUP_COVER
        lines = fit_art(lambda w: _get_art_cached(file_path, w, booklet), max_w, avail_h)
        if lines and not lines[0].startswith("\033["):
            # No picture to draw (none embedded, or one that won't decode): the
            # empty player's note stands in, rather than a line of text. No
            # lines at all means no room for art, which stays that way.
            lines = _shape_lines(IDLE_ART, max_w, avail_h)
    return "\n".join(lines), lines
