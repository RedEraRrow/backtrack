"""The player's album art: the text (half-block) art sized to fit, and on an
image-capable terminal (iTerm2, opt-in) the real image drawn over those cells —
average colour, then a quick preview, then full quality."""
from __future__ import annotations
import os
import sys
from src.utils import ui_utils
from src.playback.player_geom import geom
from src.utils.log import log
from src.art.album_art import get_art, get_art_bytes
from src.config import setting


ART_MAX_WIDTH = 200  # viu rendering degrades above this width on most terminals


# When fitting art to the terminal height would only shave off a few columns, the
# art lands in a "dead band": too narrow to fill edge-to-edge, too wide to leave a
# clean volume-bar gutter — so the centred art shows thin, lopsided side margins.
# Within this many columns of the full width, snap UP to full width and clip the
# extra bottom pixel-row(s) instead, so the art is always either edge-to-edge or
# has a comfortable gutter.
_ART_SNAP_TO_FULL = 4


_art_cache: dict = {}


def _get_art_cached(file_path: str, width: int) -> str:
    """Return the rendered album art for file_path at width, cached by (path, width, mtime)."""
    # Key on the file's mtime so editing the file (e.g. adding album art)
    # invalidates the cached render — otherwise a "No album art found." result
    # would stick until the program restarts.
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        mtime = 0.0
    key = (file_path, width, mtime)
    if key not in _art_cache:
        # Drop stale-mtime entries for this file+width so repeated edits don't
        # grow the cache unbounded.
        for k in [k for k in _art_cache if k[0] == file_path and k[1] == width and k[2] != mtime]:
            del _art_cache[k]
        _art_cache[key] = get_art(file_path, width=width)
    return _art_cache[key]


# --- Real-image album art (iTerm2 inline images), opt-in -----------------------
# The half-block art is still rendered — it sizes the layout exactly as before —
# but in image mode its cells show the cover's average colour, then a small
# quick-to-decode image, then the full-quality one, all at those same cells.
# The text art itself is only the fallback for when there's no image to show. Every position the layout records (clicks, the volume bar,
# the lyric pane) is unchanged. Off unless the `art_inline_images` setting is on
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
    """Send the full-quality image again without repainting any rows — after a
    resize settles, when the rows are right and the preview is already showing,
    or after a resize cut the last send short."""
    _draw_inline_art(full_only=True)
    sys.stdout.flush()


def art_image_incomplete() -> bool:
    """Whether the last full-quality image was cut short and still needs sending."""
    return _inline_art['incomplete']


_inline_art_cache: dict = {}             # (path, mtime, cols, rows) → (base64, byte count)


_INLINE_PX_PER_COL = 10                  # image pixels per cell column: sharp on a Retina


                                         # screen, a fraction of a full-size cover to send
_INLINE_CHUNK = 16 * 1024                # a full image goes out in pieces this size, so a


                                         # resize mid-send can cut it short (_send_image)
_INLINE_PREVIEW_PX = 3                   # the quick first image: ~1/16 the pixels


_inline_cfg: dict = {}                   # the setting, cached on config.json's mtime


def inline_art_enabled() -> bool:
    """Whether art should be drawn as a real image here. Cheap to ask often
    (the miniplayer asks every second): the setting is re-read only when the
    config file changes."""
    if os.environ.get('TMUX'):
        return False
    if (os.environ.get('TERM_PROGRAM') != 'iTerm.app'
            and os.environ.get('LC_TERMINAL') != 'iTerm2'):
        return False
    from src.config import CONFIG_FILE, load_config
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
    (a large cover takes a noticeable moment to decode — do it once, not per
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
            # above anything the player shows — every later step works on that.
            f = _WORKING_PX / max(img.shape[:2])
            img = cv2.resize(img, (round(img.shape[1] * f), round(img.shape[0] * f)),
                             interpolation=cv2.INTER_AREA)
        _decoded_cache[key] = (raw, img) if raw else None
    return _decoded_cache[key]


def _inline_art_data(file_path: str, cols: int = 0, rows: int = 0,
                     px: int = _INLINE_PX_PER_COL) -> tuple[str, int] | None:
    """The cover as base64 for the image escape, scaled to the cells it fills
    (cols × rows at `px` pixels per column) and cached per file and size — so a
    resize back to a size already seen costs nothing. The preview size is also
    saved at a lower quality: it's only on screen for a moment."""
    import base64
    import cv2
    try:
        key = (file_path, os.path.getmtime(file_path), cols, rows, px)
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
                size = (cols * px, rows * 2 * px)           # cells are ~1:2
                if size[0] < img.shape[1]:                  # only ever scale down
                    img = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
                quality = 70 if px == _INLINE_PREVIEW_PX else 85
                ok, jpg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
                if ok and len(jpg) < len(raw):              # a heavy original still shrinks
                    data = jpg.tobytes()
        _inline_art_cache[key] = (base64.b64encode(data).decode('ascii'), len(data)) if data else None
    return _inline_art_cache[key]


_mean_cache: dict = {}                   # (path, mtime) → the cover's average colour


def _cover_mean(file_path: str) -> tuple[int, int, int] | None:
    """The cover's average colour (r, g, b), or None when it can't be decoded —
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
    (mid-resize, only the preview; at a settle, only the full one — the preview
    is already there). Called after every frame's rows are written — a
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
    had to wait — the window showed the old frame rewrapped until it finished.
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
            f"height={geom.art_height};preserveAspectRatio=0;doNotMoveCursor=1:")
    if not chunked:
        sys.stdout.write(head + b64 + "\a\0338")
        return True
    import time
    start = time.monotonic()
    sys.stdout.write(head)
    for i in range(0, len(b64), _INLINE_CHUNK):
        if ui_utils.last_resize_signal_at() > start:
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


def _art_width_for_height(file_path: str, max_w: int, avail_h: int,
                          pre_art: str | None) -> tuple[str, list[str]]:
    """The art for the layout (see _art_fit). In image mode its cells become the
    cover's average colour, to be drawn over by the image (_draw_inline_art);
    the text art stays only when there's no image to show — no cover, one that
    won't decode, or a group cover, which is a composite of several."""
    art_str, lines = _art_fit(file_path, max_w, avail_h, pre_art)
    _inline_art['path'] = None
    mean = _cover_mean(file_path) if (lines and not pre_art and inline_art_enabled()) else None
    if mean:
        w = max(ui_utils.visual_len(l) for l in lines)
        lines = [f"\033[48;2;{mean[0]};{mean[1]};{mean[2]}m{' ' * w}\033[0m" for _ in lines]
        _inline_art['path'] = file_path
    return art_str, lines


def _art_fit(file_path: str, max_w: int, avail_h: int,
             pre_art: str | None) -> tuple[str, list[str]]:
    """Fetch art at max_w; if the rendered output exceeds avail_h rows,
    compute a narrower width from the actual aspect ratio and re-fetch."""
    art_str = pre_art if pre_art else _get_art_cached(file_path, width=max_w)
    lines = art_str.splitlines()
    if not lines or len(lines) <= avail_h:
        return art_str, lines

    actual_h = len(lines)
    actual_w = max((ui_utils.visual_len(l) for l in lines), default=max_w)
    ratio = actual_w / actual_h if actual_h > 0 else 2.0
    fit_w = max(10, min(max_w - 1, int(avail_h * ratio)))

    # Snap-to-full: if fitting to height only trims a handful of columns, keep the
    # full width and clip the extra bottom row(s) rather than sit in the dead band
    # (see _ART_SNAP_TO_FULL). Larger deficits fall through to a genuine re-fetch.
    if max_w - fit_w <= _ART_SNAP_TO_FULL:
        return art_str, lines[:avail_h]

    art_str2 = _get_art_cached(file_path, width=fit_w)
    lines2 = art_str2.splitlines()
    return art_str2, lines2[:avail_h]  # safety cap in case ratio was off
