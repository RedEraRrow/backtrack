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
from backtrack.playback.player_geom import geom
from backbone.log import log
from backtrack.album_art import booklet_file, decode_image, fit_art, get_art, get_art_bytes, get_embedded_art, half_blocks, has_alpha
from backtrack.config import setting


ART_MAX_WIDTH = 200  # viu rendering degrades above this width on most terminals


# The empty player's note fills the full width when a square box would only be
# this many columns narrower, so it doesn't sit in thin, lopsided margins. Only
# the note: a cover is never cropped to reach full width (see _art_fit).
_ART_SNAP_TO_FULL = 4


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
    if key not in _art_cache:
        if len(_art_cache) > 64:             # every width a drag passes through
            _art_cache.clear()
        _art_cache[key] = (get_embedded_art(file_path, width, preferred_desc='Booklet', preferred_type=6)
                           if booklet else get_art(file_path, width=width))
    return _art_cache[key]


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


_INLINE_PX_PER_COL = 10                  # image pixels per cell column: sharp on a Retina


                                         # screen, a fraction of a full-size cover to send
_INLINE_CHUNK = 16 * 1024                # a full image goes out in pieces this size, so a


                                         # resize mid-send can cut it short (_send_image)
_INLINE_PREVIEW_PX = 3                   # the quick first image: ~1/16 the pixels


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
    if key not in _decoded_cache:
        if len(_decoded_cache) > 4:            # decoded covers are large: keep a few
            _decoded_cache.clear()
        raw = get_art_bytes(file_path)
        img = decode_image(raw) if raw else None
        if img is not None:
            # A 4000-pixel cover is shrunk once, here, to a working size still
            # above anything the player shows; every later step works on that.
            img.thumbnail((_WORKING_PX, _WORKING_PX), Image.Resampling.BOX)
        _decoded_cache[key] = (raw, img) if raw else None
    return _decoded_cache[key]


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
    if key not in _inline_art_cache:
        if len(_inline_art_cache) > 24:
            _inline_art_cache.clear()
        dec = _cover_decoded(file_path)
        _inline_art_cache[key] = _encoded_picture(dec[0], dec[1], cols, rows, px) if dec else None
    return _inline_art_cache[key]


def _encoded_picture(raw: bytes, img, cols: int, rows: int, px: int) -> tuple[str, int] | None:
    """A picture as base64 for the image escape (and its byte count): cropped
    about its centre to the cells' shape so it fills them with no gap, scaled
    to them at `px` pixels a column (only ever down), a PNG when it has
    transparency, else a JPEG. The raw bytes when there are no cells to fit."""
    import base64
    data = raw
    transparent = has_alpha(raw)
    if transparent:                                         # keep it: a JPEG has no transparency
        img = decode_image(raw, keep_alpha=True)
    if img is not None and cols and rows:
        # A cover is cropped to fill its cells; a picture with transparency (a
        # round portrait) is kept whole, fitted inside them (the terminal
        # centres it), so a circle is never cut.
        if not transparent:
            img = crop_to_cells(img, cols, rows)
        f = min(cols * px / img.width, rows * ui.cell_aspect() * px / img.height)
        if f < 1:
            img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))),
                             Image.Resampling.BOX)
        buf = BytesIO()
        if transparent:
            img.save(buf, "PNG")
        else:
            img.save(buf, "JPEG", quality=70 if px == _INLINE_PREVIEW_PX else 85)
        data = buf.getvalue()
    return (base64.b64encode(data).decode('ascii'), len(data)) if data else None


_mean_cache: dict = {}                   # (path, mtime) → the cover's average colour


# Art-derived accents: how colourful a cover colour must be to count, and the
# lightness and saturation the chosen ones are set to, so they read on a dark
# terminal whatever the cover was like.
_ART_MIN_SATURATION = 0.2
_ART_LIGHTNESS = 0.62
_ART_SATURATION = (0.5, 0.9)
_ART_MIN_HUE_GAP = 0.08                  # a second accent this far round the wheel, at least
_accents_cache: dict = {}


def art_accents(file_path: str) -> tuple[str, str] | None:
    """Two accents ("#RRGGBB") from a file's cover: its most prominent colourful
    colour, and a second of a clearly different hue (the first's opposite when
    the cover has only one). None when there's no cover or it's all greys."""
    import colorsys
    decoded = _cover_decoded(file_path)
    if not decoded or decoded[1] is None:
        return None
    key = (file_path, id(decoded[1]))
    if key not in _accents_cache:
        if len(_accents_cache) > 32:
            _accents_cache.clear()
        small = decoded[1].copy()
        small.thumbnail((64, 64))
        q = small.quantize(colors=8)
        pal = q.getpalette() or []
        found = []
        for count, idx in sorted(q.getcolors() or [], reverse=True):
            h, l, s = colorsys.rgb_to_hls(*(c / 255 for c in pal[idx * 3:idx * 3 + 3]))
            if s >= _ART_MIN_SATURATION and 0.12 < l < 0.92:
                found.append((count * s, h, s))
        if not found:
            _accents_cache[key] = None
        else:
            found.sort(reverse=True)
            _w, h1, s1 = found[0]
            gap = lambda h: min(abs(h - h1), 1 - abs(h - h1))   # noqa: E731
            other = next((f for f in found[1:] if gap(f[1]) >= _ART_MIN_HUE_GAP), None)
            h2, s2 = (other[1], other[2]) if other else ((h1 + 0.5) % 1, s1)

            def tone(h: float, s: float) -> str:
                r, g, b = colorsys.hls_to_rgb(h, _ART_LIGHTNESS, min(max(s, _ART_SATURATION[0]), _ART_SATURATION[1]))
                return "#%02X%02X%02X" % (round(r * 255), round(g * 255), round(b * 255))
            _accents_cache[key] = (tone(h1, s1), tone(h2, s2))
    return _accents_cache[key]


def _cover_mean(file_path: str) -> tuple[int, int, int] | None:
    """The cover's average colour (r, g, b), or None when it can't be decoded,
    in which case the text art is the fallback."""
    try:
        key = (file_path, os.path.getmtime(file_path))
    except OSError:
        return None
    if key not in _mean_cache:
        if len(_mean_cache) > 64:
            _mean_cache.clear()
        dec = _cover_decoded(file_path)
        img = dec[1] if dec else None
        _mean_cache[key] = (None if img is None else
                            tuple(int(v) for v in ImageStat.Stat(img).mean[:3]))
    return _mean_cache[key]


def _draw_inline_art(full_only: bool = False) -> None:
    """Draw this frame's image over its cells: the small preview, then the
    full-quality image, which replaces it once the terminal has decoded it
    (mid-resize, only the preview; at a settle, only the full one, as the preview
    is already there). Called after every frame's rows are written: a
    repainted row erases whatever image was under it."""
    path = _inline_art['path']
    if not (path and geom.art_top and geom.art_width and geom.art_height):
        return
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


def crop_to_cells(img, cols: int, rows: int):
    """`img` cropped about its centre to the shape cols × rows cells take on
    screen, so drawn into them it fills them with no gap."""
    want = cols / max(1e-6, rows * ui.cell_aspect())          # width over height, on screen
    w, h = img.size
    if w / h > want:
        nw = max(1, round(h * want))
        return img.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    nh = max(1, round(w / want))
    return img.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))


_fill_cache: dict = {}


def _fill_or_fit(img, cols: int, rows: int):
    """`img` at cols × rows cells' half-block pixels: a cover cropped to fill
    them; a picture with transparency (a round portrait) whole, fitted and
    centred on a clear background, so a circle is never cut."""
    size = (cols, rows * 2)
    if img.mode != "RGBA" or img.getextrema()[3][0] == 255:
        return crop_to_cells(img, cols, rows).resize(size, Image.Resampling.BOX)
    # A half-block pixel is half a cell high: the cells' own shape on screen.
    f = min(cols / img.width, rows * 2 * ui.cell_aspect() / 2 / img.height)
    w, h = max(1, round(img.width * f)), max(1, round(img.height * f * 2 / ui.cell_aspect()))
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(img.resize((min(w, cols), min(h, rows * 2)), Image.Resampling.BOX),
              ((cols - min(w, cols)) // 2, (rows * 2 - min(h, rows * 2)) // 2))
    return out


def fill_art(path: str, cols: int, rows: int) -> list[str]:
    """`path`'s picture filling cols × rows cells exactly, cropped about its
    centre to their shape, as half-block text; [] when there's no picture."""
    try:
        key = (path, os.path.getmtime(path), cols, rows, ui.cell_aspect())
    except OSError:
        return []
    if key not in _fill_cache:
        if len(_fill_cache) > 32:
            _fill_cache.clear()
        dec = _cover_decoded(path)
        img = (decode_image(dec[0], keep_alpha=True) if dec and has_alpha(dec[0])
               else dec[1].convert("RGBA") if dec and dec[1] is not None else None)
        _fill_cache[key] = (half_blocks(_fill_or_fit(img, cols, rows)).splitlines()
                            if img is not None and cols > 0 and rows > 0 else [])
    return _fill_cache[key]


def square_art(path: str, cols: int, rows: int, pre_art: str | None) -> list[str]:
    """The art filling a box cols × rows with no gap: the cover cropped to it
    (an image where the terminal shows them, its cells standing under it, else
    text art); the empty player's note. A group shows its booklet."""
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
    mean = _cover_mean(path)
    data = _inline_art_data(path, cols, rows, _INLINE_PX_PER_COL) if mean else None
    if not data:
        return None
    # Under a picture with transparency (a round portrait) the cells stay
    # blank, or its corners would show them.
    dec = _cover_decoded(path)
    blank = dec is not None and has_alpha(dec[0])
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
