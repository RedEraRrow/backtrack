"""
ASCII art and image conversion for terminal display powered by viu.

Captures clean terminal graphics sequences using 'viu'. Supports full native
high-resolution half-blocks and inline graphic protocols without destructive flattening.
"""

import os
import subprocess
from mutagen.id3 import ID3

MAX_ART_WIDTH = 48


def render_with_viu(image_source: str | bytes, width: int = 100, is_bytes: bool = False) -> str:
    """
    Execute the 'viu' CLI tool and capture its ANSI terminal output sequence.
    Forces block output (half-blocks) for maximum resolution and stable wrapping.
    """
    if is_bytes:
        cmd = ["viu", "-b", "-w", str(width), "-"]
    else:
        if not os.path.exists(image_source):
            return f"Error: File '{image_source}' does not exist."
        cmd = ["viu", "-b", "-w", str(width), image_source]
    
    try:
        if is_bytes:
            result = subprocess.run(
                cmd, 
                input=image_source, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                check=True
            )
        else:
            result = subprocess.run(
                cmd, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE, 
                check=True
            )
            
        art_str = result.stdout.decode('utf-8', errors='ignore')
        return art_str
        
    except FileNotFoundError:
        return "Error: 'viu' CLI is not installed or not found in your PATH."
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore').strip()
        return f"Error rendering with viu: {error_msg if error_msg else e}"


def get_ascii_from_image_file(file_path: str, width: int) -> str:
    """Load image file and return its viu terminal sequence."""
    return render_with_viu(file_path, width=width, is_bytes=False)


def _select_apic_frame(audio: ID3, preferred_desc: str | None = None,
                       preferred_type: int | None = None):
    """Return the best-matching APIC frame for the requested image type."""
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


def get_ascii_from_mp3(file_path: str, width: int,
                       preferred_desc: str | None = None,
                       preferred_type: int | None = None) -> str:
    """Extract album art from MP3 and return its viu terminal sequence."""
    try:
        audio = ID3(file_path)
        apic_frame = _select_apic_frame(audio, preferred_desc=preferred_desc, preferred_type=preferred_type)
        if not apic_frame:
            return "No album art found."

        img_data = apic_frame.data
        return render_with_viu(img_data, width=width, is_bytes=True)
    except Exception as e:
        return f"Error loading MP3 art: {e}"


def get_ascii(file_path: str, width: int = 100) -> str:
    """Convert image or MP3 album art to a viu ANSI sequence."""
    if not os.path.isfile(file_path):
        return "Error: Invalid file path."

    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "mp3":
        return get_ascii_from_mp3(file_path, width)
    return get_ascii_from_image_file(file_path, width)