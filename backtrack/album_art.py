"""Album-art extraction and terminal rendering."""
import functools
import os
from io import BytesIO

from PIL import Image

from backbone import ui
from backtrack.id3.tag_formats import AUDIO_EXTENSIONS, embedded_cover


def decode_image(img_bytes: bytes, keep_alpha: bool = False) -> "Image.Image | None":
    """Image bytes as RGB pixels, any transparency laid over black (so a
    transparent PNG isn't mangled), or None when they can't be decoded.
    `keep_alpha`: RGBA instead, transparency kept."""
    try:
        img = Image.open(BytesIO(img_bytes))
        img.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    if keep_alpha:
        return img.convert("RGBA")
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba)
    return img.convert("RGB")


def render_native_half_block(img_bytes: bytes, width: int = 100) -> str:
    """Render image bytes as ANSI half-block characters for terminal display."""
    # 1. Decode bytes to pixels, keeping transparency (a round artist picture).
    img = decode_image(img_bytes, keep_alpha=True)
    if img is None: return "Error decoding image."

    # 2. Resize keeping the image's proportions on screen. Each cell shows one
    # pixel column and two pixel rows, so a pixel row is half a cell tall:
    # `ui.cell_aspect()` (cell height over width) says how many rows square pixels need.
    width = max(1, width)
    height = max(1, round(width * img.height / img.width * 2 / ui.cell_aspect()))
    img = img.resize((width, height), Image.Resampling.BOX)   # area average, as shrinking wants
    return half_blocks(img)


def half_blocks(img: "Image.Image") -> str:
    """RGBA pixels as ANSI half-block lines, one cell per pixel column and two
    pixel rows (U+2580: the top pixel as foreground, the bottom as background).
    A transparent half (and an odd height's last row's bottom half) is the
    terminal's own background, the other half drawn as ▀ or ▄."""
    width, height = img.size
    px = img.load()
    output = []
    for y in range(0, height, 2):
        for x in range(width):
            top = px[x, y]
            bot = px[x, y + 1] if y + 1 < height else (0, 0, 0, 0)
            if top[3] >= 128 and bot[3] >= 128:
                output.append(f"\033[38;2;{top[0]};{top[1]};{top[2]}m\033[48;2;{bot[0]};{bot[1]};{bot[2]}m\u2580")
            elif top[3] >= 128:
                output.append(f"\033[38;2;{top[0]};{top[1]};{top[2]}m\033[49m\u2580")
            elif bot[3] >= 128:
                output.append(f"\033[38;2;{bot[0]};{bot[1]};{bot[2]}m\033[49m\u2584")
            else:
                output.append("\033[49m ")
        output.append("\033[0m\n")
    return "".join(output)

def render_album_art(image_source: str | bytes, width: int = 100, is_bytes: bool = False) -> str:
    """Render art from a path or raw bytes to terminal text."""
    if is_bytes:
        assert isinstance(image_source, bytes)
        return render_native_half_block(image_source, width)
    else:
        if not isinstance(image_source, str):
            return "Error: Expected a file path string."
            
        try:
            with open(image_source, 'rb') as f:
                img_data = f.read()
            return render_native_half_block(img_data, width)
        except Exception as e:
            return f"Error reading file: {e}"


def get_art_from_image_file(file_path: str, width: int) -> str:
    """Render a standalone image file (not embedded in a tag) to terminal text."""
    return render_album_art(file_path, width=width, is_bytes=False)


def get_embedded_art(file_path: str, width: int,
                     preferred_desc: str | None = None,
                     preferred_type: int | None = None) -> str:
    """Render the picture embedded in an audio file of any kind: the one
    described `preferred_desc`, else of `preferred_type`, else the cover."""
    raw = embedded_cover(file_path, preferred_desc=preferred_desc, preferred_type=preferred_type)
    return render_album_art(raw, width=width, is_bytes=True) if raw else "No album art found."


_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def get_art_bytes(file_path: str) -> bytes | None:
    """The raw cover image for a file (an audio file's embedded picture, or an
    image file itself); None if there's none."""
    if file_path.lower().endswith(_IMAGE_EXTENSIONS):
        try:
            with open(file_path, "rb") as f:
                return f.read()
        except OSError:
            return None
    return embedded_cover(file_path)


def folder_image(folder: str, names: tuple = ("artist", "folder", "poster")) -> str | None:
    """A picture kept beside the music in `folder` (Artist.jpg, folder.png…):
    the first of `names` there, in any case and image format."""
    try:
        files = {f.lower(): f for f in os.listdir(folder)}
    except OSError:
        return None
    return next((os.path.join(folder, files[n + ext]) for n in names for ext in _IMAGE_EXTENSIONS
                 if n + ext in files), None)


_PORTRAIT_PX = 600                       # a round portrait's size, ample for any box


def has_alpha(img_bytes: bytes) -> bool:
    """Whether an image has transparency (a round portrait does)."""
    try:
        img = Image.open(BytesIO(img_bytes))
    except (OSError, ValueError):
        return False
    return img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info


def _derived_picture(path: str, tag: str, make) -> str | None:
    """`make(img)` of `path`'s picture (an RGBA image back), made once per
    picture, its mtime and `tag`, as a PNG in the cache folder; that file's
    path, or None when the picture won't decode."""
    import hashlib
    from backtrack.music_library import CACHE_DIR
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    # ponytail: old copies stay when a picture changes; they're small, clear the folder if it matters.
    out = CACHE_DIR / "portraits" / (hashlib.sha1(f"{path}\0{stamp}\0{tag}".encode()).hexdigest()[:16] + ".png")
    if out.exists():
        return str(out)
    raw = get_art_bytes(path)
    img = decode_image(raw) if raw else None
    if img is None:
        return None
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        make(img).save(out, "PNG")
    except OSError:
        return None
    return str(out)


def _smooth_mask(size: tuple, draw) -> "Image.Image":
    """An alpha mask `size`, drawn by `draw(ImageDraw, (w, h))` four times as
    big and shrunk, for a smooth edge."""
    from PIL import ImageDraw
    w, h = size
    mask = Image.new("L", (w * 4, h * 4), 0)
    draw(ImageDraw.Draw(mask), (w * 4, h * 4))
    return mask.resize((w, h), Image.Resampling.LANCZOS)


def round_portrait(path: str) -> str | None:
    """`path`'s picture cropped to its centre square and cut to a circle,
    transparent outside it (see _derived_picture)."""
    def make(img):
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        size = min(side, _PORTRAIT_PX)
        img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)
        img = img.convert("RGBA")
        img.putalpha(_smooth_mask(img.size, lambda d, wh: d.ellipse((0, 0, wh[0] - 1, wh[1] - 1), fill=255)))
        return img
    return _derived_picture(path, "round", make)


def picture_file(raw: bytes) -> str | None:
    """Picture bytes (a tag's, not a file's cover) kept as an image file in
    the cache folder, named by their content, so everything that shows a
    picture from a file (an image, text art) shows these too. None when the
    bytes aren't an image."""
    import hashlib
    from backtrack.music_library import CACHE_DIR
    if not raw:
        return None
    try:
        kind = (Image.open(BytesIO(raw)).format or "").lower()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    ext = {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(kind)
    if not ext:
        return None
    out = CACHE_DIR / "pictures" / (hashlib.sha1(raw).hexdigest()[:16] + ext)
    if not out.exists():
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(raw)
        except OSError:
            return None
    return str(out)


def booklet_file(path: str) -> str | None:
    """A grouping file's booklet picture as an image file (picture_file)."""
    try:
        return _booklet_file(path, os.path.getmtime(path))
    except OSError:
        return None


@functools.lru_cache(maxsize=16)
def _booklet_file(path: str, _mtime: float) -> str | None:
    return picture_file(embedded_cover(path, preferred_desc='Booklet', preferred_type=6) or b"")


def artist_image(paths: list) -> str | None:
    """An artist's own picture, from the folder their albums are in
    (Artist/Album/track: Artist/artist.jpg, folder.jpg or poster.jpg), or an
    artist.jpg beside the tracks themselves."""
    for path in paths[:20]:
        album_dir = os.path.dirname(path)
        found = folder_image(os.path.dirname(album_dir)) or folder_image(album_dir, ("artist",))
        if found:
            return found
    return None


def get_art(file_path: str, width: int = 100) -> str:
    """Render a file's album art: an audio file's embedded picture, or an image file."""
    if not os.path.isfile(file_path):
        return "Error: file not found"
    if file_path.lower().endswith(AUDIO_EXTENSIONS):
        return get_embedded_art(file_path, width)
    return get_art_from_image_file(file_path, width)


def fit_art(render, max_w: int, avail_h: int) -> list[str]:
    """`render(width)`'s lines at the widest width up to `max_w` that is at most
    `avail_h` rows tall. Only ever narrows: never crops or stretches. Empty when
    not even one column fits. Every place that shows art sizes it through here."""
    w = max(1, max_w)
    lines = render(w).splitlines()
    while len(lines) > avail_h:
        if w == 1:
            return []
        # Straight to the proportional width, then a column at a time for rounding.
        w = max(1, min(w - 1, w * avail_h // len(lines)))
        lines = render(w).splitlines()
    # The proportional guess can land short: take every column that still fits,
    # so a wider window never gets narrower art.
    while w < max_w:
        wider = render(w + 1).splitlines()
        if len(wider) > avail_h:
            break
        w, lines = w + 1, wider
    return lines
