"""The player's album art: the text (half-block) art sized to fit, and on an
image-capable terminal (iTerm2, opt-in) the real image drawn over those cells:
average colour, then a quick preview, then full quality."""
from __future__ import annotations
import functools
import math
import os
import sys
from backbone import ui
from backtrack.playback.player_geom import geom
from backbone.log import log
from backtrack.album_art import fit_art, get_art, get_art_bytes, get_art_from_mp3
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
        _art_cache[key] = (get_art_from_mp3(file_path, width, preferred_desc='Booklet', preferred_type=6)
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
    import cv2
    import numpy as np
    try:
        key = (file_path, os.path.getmtime(file_path))
    except OSError:
        return None
    if key not in _decoded_cache:
        if len(_decoded_cache) > 4:            # decoded covers are large: keep a few
            _decoded_cache.clear()
        raw = get_art_bytes(file_path)
        img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR) if raw else None
        if img is not None and max(img.shape[:2]) > _WORKING_PX:
            # A 4000-pixel cover is shrunk once, here, to a working size still
            # above anything the player shows; every later step works on that.
            f = _WORKING_PX / max(img.shape[:2])
            img = cv2.resize(img, (round(img.shape[1] * f), round(img.shape[0] * f)),
                             interpolation=cv2.INTER_AREA)
        _decoded_cache[key] = (raw, img) if raw else None
    return _decoded_cache[key]


def _inline_art_data(file_path: str, cols: int = 0, rows: int = 0,
                     px: int = _INLINE_PX_PER_COL) -> tuple[str, int] | None:
    """The cover as base64 for the image escape, scaled to the cells it fills
    (cols × rows at `px` pixels per column) and cached per file and size, so a
    resize back to a size already seen costs nothing. The preview size is also
    saved at a lower quality: it's only on screen for a moment."""
    import base64
    import cv2
    try:
        key = (file_path, os.path.getmtime(file_path), cols, rows, px, ui.cell_aspect())
    except OSError:
        return None
    if key not in _inline_art_cache:
        if len(_inline_art_cache) > 24:
            _inline_art_cache.clear()
        dec = _cover_decoded(file_path)
        data = None
        if dec:
            raw, img = dec
            data = raw
            if img is not None and cols and rows:
                # One factor for both sides, so the image keeps its proportions;
                # the terminal fits it to the cells (preserveAspectRatio=1).
                f = min(cols * px / img.shape[1], rows * ui.cell_aspect() * px / img.shape[0])
                if f < 1:                                   # only ever scale down
                    img = cv2.resize(img, (max(1, round(img.shape[1] * f)), max(1, round(img.shape[0] * f))),
                                     interpolation=cv2.INTER_AREA)
                quality = 70 if px == _INLINE_PREVIEW_PX else 85
                ok, jpg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
                if ok and len(jpg) < len(raw):              # a heavy original still shrinks
                    data = jpg.tobytes()
        _inline_art_cache[key] = (base64.b64encode(data).decode('ascii'), len(data)) if data else None
    return _inline_art_cache[key]


_mean_cache: dict = {}                   # (path, mtime) → the cover's average colour


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
        import cv2
        _mean_cache[key] = (None if img is None else
                            tuple(int(v) for v in cv2.mean(img)[2::-1]))    # BGR → RGB
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
    if not _inline_art['resizing']:
        _inline_art['incomplete'] = not _send_image(path, _INLINE_PX_PER_COL, chunked=True)


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
            f"\033]1337;File=inline=1;size={size};width={geom.art_width};"
            f"height={geom.art_height};preserveAspectRatio=1;doNotMoveCursor=1:")
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


@functools.lru_cache(maxsize=8)
def _idle_art_lines(max_w: int, avail_h: int) -> list[str]:
    """The note centred in the box a square cover gets at this size (see
    _art_fit for the cover's sizing), scaled to it, so the empty player lays
    out like a playing one."""
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
                       if _in_note((2 * c + dx + 0.5 - x0) / scale, (4 * r + dy + 0.5 - y0) / scale))
            cells.append(chr(0x2800 + mask) if mask else " ")
        rows[r] = " " * c0 + "".join(cells) + " " * (w - c1)
    return rows


def _art_width_for_height(file_path: str, max_w: int, avail_h: int,
                          pre_art: str | None) -> tuple[str, list[str]]:
    """The art for the layout (see _art_fit). In image mode its cells become the
    cover's average colour, to be drawn over by the image (_draw_inline_art);
    the text art stays only when there's no image to show: no cover, one that
    won't decode, or a group cover, which is a composite of several."""
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
    IDLE_ART for the empty player's note, GROUP_COVER for a grouping file's
    booklet image, else None for the file's cover."""
    if pre_art is IDLE_ART:
        lines = _idle_art_lines(max_w, avail_h)
    else:
        booklet = pre_art is GROUP_COVER
        lines = fit_art(lambda w: _get_art_cached(file_path, w, booklet), max_w, avail_h)
    return "\n".join(lines), lines
