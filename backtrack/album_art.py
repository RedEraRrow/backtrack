"""Album-art extraction and terminal rendering."""
import os
from io import BytesIO

import mutagen.id3
from mutagen.id3 import ID3
from PIL import Image

from backbone import ui


def decode_image(img_bytes: bytes) -> "Image.Image | None":
    """Image bytes as RGB pixels, any transparency laid over black (so a
    transparent PNG isn't mangled), or None when they can't be decoded."""
    try:
        img = Image.open(BytesIO(img_bytes))
        img.load()
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba)
    return img.convert("RGB")


def render_native_half_block(img_bytes: bytes, width: int = 100) -> str:
    """Render image bytes as ANSI half-block characters for terminal display."""
    # 1. Decode bytes to RGB pixels.
    img = decode_image(img_bytes)
    if img is None: return "Error decoding image."

    # 2. Resize keeping the image's proportions on screen. Each cell shows one
    # pixel column and two pixel rows, so a pixel row is half a cell tall:
    # `ui.cell_aspect()` (cell height over width) says how many rows square pixels need.
    width = max(1, width)
    height = max(1, round(width * img.height / img.width * 2 / ui.cell_aspect()))
    img = img.resize((width, height), Image.Resampling.BOX)   # area average, as shrinking wants
    px = img.load()

    # 3. Convert to ANSI half-blocks (U+2580): top pixel as foreground, bottom as
    # background. An odd height leaves the last row's bottom half the terminal's
    # own background, rather than repeating a pixel row.
    output = []
    for y in range(0, height, 2):
        for x in range(width):
            r, g, b = px[x, y]
            if y + 1 < height:
                br, bg_, bb = px[x, y + 1]
                bg = f"\033[48;2;{br};{bg_};{bb}m"
            else:
                bg = "\033[49m"
            output.append(f"\033[38;2;{r};{g};{b}m{bg}\u2580")
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


def _select_apic_frame(audio: ID3, preferred_desc: str | None = None,
                       preferred_type: int | None = None):
    """Pick an MP3's cover APIC frame: by description, then by picture type,
    else the first embedded picture found."""
    apic_keys = [k for k in audio.keys() if k.startswith('APIC')]
    if not apic_keys:
        return None

    if preferred_desc:
        for key in apic_keys:
            frame = audio[key]
            if getattr(frame, 'desc', '').strip().lower() == preferred_desc.strip().lower():
                return frame

    if preferred_type is not None:
        for key in apic_keys:
            frame = audio[key]
            if getattr(frame, 'type', None) == preferred_type:
                return frame

    return audio[apic_keys[0]]


def get_art_from_mp3(file_path: str, width: int,
                       preferred_desc: str | None = None,
                       preferred_type: int | None = None) -> str:
    """Extract and render an MP3's embedded cover art."""
    try:
        audio = ID3(file_path)
        apic_frame = _select_apic_frame(audio, preferred_desc=preferred_desc, preferred_type=preferred_type)
        if not apic_frame:
            return "No album art found."

        img_data = apic_frame.data
        return render_album_art(img_data, width=width, is_bytes=True)
    except (FileNotFoundError, OSError, mutagen.id3.ID3NoHeaderError) as e:  # type: ignore[reportPrivateImportUsage]
        return f"Error loading MP3 art: {e}"


def get_art_bytes(file_path: str) -> bytes | None:
    """The raw cover image for a file (an MP3's embedded picture, an MP4's
    `covr` atom, or an image file itself); None if there's none."""
    try:
        if file_path.lower().endswith(".mp3"):
            frame = _select_apic_frame(ID3(file_path))
            return bytes(frame.data) if frame is not None and frame.data else None
        if os.path.splitext(file_path)[1].lower() in _MP4_EXTS:
            from mutagen.mp4 import MP4
            covr = (MP4(file_path).tags or {}).get("covr")
            return bytes(covr[0]) if covr else None
        if os.path.splitext(file_path)[1].lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            with open(file_path, "rb") as f:
                return f.read()
    except (OSError, mutagen.MutagenError):
        pass
    return None


_MP4_EXTS = (".m4a", ".mp4", ".m4p", ".m4b")


def get_art(file_path: str, width: int = 100) -> str:
    """Render a file's album art, dispatching to the MP3 tag reader or plain
    image loader by extension."""
    if not os.path.isfile(file_path):
        return "Error: Invalid file path."

    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "mp3":
        return get_art_from_mp3(file_path, width)
    if "." + ext in _MP4_EXTS:
        raw = get_art_bytes(file_path)
        return render_album_art(raw, width=width, is_bytes=True) if raw else "No album art found."
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
