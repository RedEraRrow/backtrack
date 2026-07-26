"""Album-art extraction and terminal rendering."""
import os
import subprocess
import mutagen.id3
from mutagen.id3 import ID3

import cv2
import numpy as np

def render_native_half_block(img_bytes: bytes, width: int = 100) -> str:
    """Render image bytes as ANSI half-block characters for terminal display."""
    # 1. Decode bytes to image
    nparr = np.frombuffer(img_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None: return "Error decoding image."

    # 2. Resize maintaining aspect ratio
    # Terminal cells are ~2:1 (tall:wide); half-blocks give 2 pixel rows per char row,
    # so those factors cancel — resize to full pixel height, step-by-2 does the rest.
    aspect_ratio = img.shape[0] / img.shape[1]
    height = int(width * aspect_ratio) 
    img = cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)

    # 3. Convert to ANSI half-blocks
    # Standard approach: Top half = foreground, Bottom half = background
    # using the UPPER HALF BLOCK character (U+2580)
    output = []
    for y in range(0, img.shape[0], 2):
        for x in range(img.shape[1]):
            # Get color of top and bottom pixel
            top = img[y, x]
            bottom = img[y+1, x] if y + 1 < img.shape[0] else [0, 0, 0]
            
            # ANSI escape sequence for Foreground (top) and Background (bottom)
            output.append(f"\033[38;2;{top[2]};{top[1]};{top[0]}m"
                          f"\033[48;2;{bottom[2]};{bottom[1]};{bottom[0]}m"
                          "\u2580")
        output.append("\033[0m\n")
    return "".join(output)

def render_album_art(image_source: str | bytes, width: int = 100, is_bytes: bool = False) -> str:
    """
    Handles both file paths (str) and raw image data (bytes).
    """
    if is_bytes:
        # If it's already bytes, we can pass it directly
        assert isinstance(image_source, bytes)
        return render_native_half_block(image_source, width)
    else:
        # If it's a string, we MUST read the file to get bytes first
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


def get_art(file_path: str, width: int = 100) -> str:
    """Render a file's album art, dispatching to the MP3 tag reader or plain
    image loader by extension."""
    if not os.path.isfile(file_path):
        return "Error: Invalid file path."

    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "mp3":
        return get_art_from_mp3(file_path, width)
    return get_art_from_image_file(file_path, width)
