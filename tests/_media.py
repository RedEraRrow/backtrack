"""Tiny real audio files for tag tests, made with ffmpeg (tests skip without it)."""
import os
import shutil
import subprocess
import unittest

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")

_CODECS = {
    "mp3": ["-c:a", "libmp3lame"], "wav": ["-c:a", "pcm_s16le"], "aiff": ["-c:a", "pcm_s16be"],
    "aif": ["-c:a", "pcm_s16be", "-f", "aiff"], "flac": ["-c:a", "flac"], "m4a": ["-c:a", "aac"],
    "m4b": ["-c:a", "aac", "-f", "ipod"], "opus": ["-c:a", "libopus"],
    # This ffmpeg may lack libvorbis: its own encoder needs stereo and -strict.
    "ogg": ["-c:a", "vorbis", "-strict", "-2", "-ac", "2"],
    "oga": ["-c:a", "vorbis", "-strict", "-2", "-ac", "2", "-f", "ogg"],
}


def make_audio(directory: str, ext: str, name: str = "t") -> str:
    """A 0.3 s untagged file of type `ext` in `directory`."""
    path = os.path.join(directory, f"{name}.{ext}")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=0.3",
                    "-map_metadata", "-1", *_CODECS[ext], path], check=True)
    return path


def png_bytes() -> bytes:
    """A small valid PNG."""
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (4, 4), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()
