"""
ASCII art and image conversion for terminal display.

Converts images to coloured ASCII art for terminal output.
"""

import os
import numpy as np
import cv2
from mutagen.id3 import ID3


ASCII_CHARS = "@%#*+=-:. "


def resize_image(image: np.ndarray, new_width: int) -> np.ndarray:
    """
    Resize image for ASCII conversion.
    
    Adjusts height to maintain aspect ratio (0.5x factor for terminal).
    """
    h, w = image.shape[:2]
    new_h = max(1, int(new_width * (h / w) * 0.5))
    return cv2.resize(image, (new_width, new_h))


def get_colour_char(pixel: tuple, chars: str) -> str:
    """
    Convert a pixel to a coloured ASCII character.
    
    Uses brightness to select character, preserves original colour.
    """
    b, g, r = pixel
    brightness = int(0.299 * r + 0.587 * g + 0.114 * b)
    idx = (brightness * (len(chars) - 1)) // 255
    return f"\033[38;2;{r};{g};{b}m{chars[idx]}\033[0m"


def convert_to_ascii_art(img: np.ndarray) -> str:
    """Convert image array to coloured ASCII art string."""
    return "\n".join(
        "".join(get_colour_char(px, ASCII_CHARS) for px in row)
        for row in img
    )


def convert_image_to_ascii(image: np.ndarray, width: int) -> str:
    """Convert image to ASCII art with specified width."""
    if image is None:
        return "Error: no image data."
    return convert_to_ascii_art(resize_image(image, width))


def get_ascii_from_image_file(file_path: str, width: int) -> str:
    """Load image from file and convert to ASCII art."""
    try:
        img = cv2.imread(file_path)
        if img is None:
            return "Could not decode image."
        return convert_image_to_ascii(img, width)
    except Exception as e:
        return f"Error loading image: {e}"


def get_ascii_from_mp3(file_path: str, width: int) -> str:
    """Extract album art from MP3 and convert to ASCII art."""
    try:
        audio = ID3(file_path)
        apic_keys = [k for k in audio.keys() if k.startswith('APIC')]
        
        if not apic_keys:
            return "No album art found."
        
        img_data = audio[apic_keys[0]].data
        nparr = np.frombuffer(img_data, np.uint8)
        image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        return convert_image_to_ascii(image, width)
    except Exception as e:
        return f"Error loading MP3 art: {e}"


def get_ascii(file_path: str, width: int = 100) -> str:
    """
    Convert image or MP3 album art to ASCII art.
    
    Auto-detects file type and uses appropriate loader.
    """
    ext = file_path.rsplit(".", 1)[-1].lower()
    
    if ext in ("jpg", "jpeg", "png"):
        return get_ascii_from_image_file(file_path, width)
    elif ext == "mp3":
        return get_ascii_from_mp3(file_path, width)
    
    return "Unsupported file type."
