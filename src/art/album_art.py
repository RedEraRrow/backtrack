"""Album-art extraction and terminal rendering (via the viu CLI)."""
import os
import subprocess
import mutagen.id3
from mutagen.id3 import ID3


def render_with_viu(image_source: str | bytes, width: int = 100, is_bytes: bool = False) -> str:
    """Forces block output (-b) for stable half-block rendering; captures stdout."""
    if is_bytes:
        cmd = ["viu", "-b", "-w", str(width), "-"]
    else:
        if not os.path.exists(image_source):
            return f"Error: File '{image_source}' does not exist."
        cmd = ["viu", "-b", "-w", str(width), image_source]

    try:
        kwargs = {'input': image_source} if is_bytes else {}
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, **kwargs)  # type: ignore[call-overload]

        art_str = result.stdout.decode('utf-8', errors='ignore')
        return art_str

    except FileNotFoundError:
        return "Error: 'viu' CLI is not installed or not found in your PATH."
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8', errors='ignore').strip()
        return f"Error rendering with viu: {error_msg if error_msg else e}"


def get_viu_art_from_image_file(file_path: str, width: int) -> str:
    return render_with_viu(file_path, width=width, is_bytes=False)


def _select_apic_frame(audio: ID3, preferred_desc: str | None = None,
                       preferred_type: int | None = None):
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


def get_viu_art_from_mp3(file_path: str, width: int,
                       preferred_desc: str | None = None,
                       preferred_type: int | None = None) -> str:
    try:
        audio = ID3(file_path)
        apic_frame = _select_apic_frame(audio, preferred_desc=preferred_desc, preferred_type=preferred_type)
        if not apic_frame:
            return "No album art found."

        img_data = apic_frame.data
        return render_with_viu(img_data, width=width, is_bytes=True)
    except (FileNotFoundError, OSError, mutagen.id3.ID3NoHeaderError) as e:  # type: ignore[reportPrivateImportUsage]
        return f"Error loading MP3 art: {e}"


def get_viu_art(file_path: str, width: int = 100) -> str:
    if not os.path.isfile(file_path):
        return "Error: Invalid file path."

    ext = file_path.rsplit(".", 1)[-1].lower()
    if ext == "mp3":
        return get_viu_art_from_mp3(file_path, width)
    return get_viu_art_from_image_file(file_path, width)
