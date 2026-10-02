"""What backtrack needs besides Python (see backbone.deps): VLC to play, and
the tools a few features use. Checked at startup for VLC, and all reported by
`backtrack doctor`."""
from __future__ import annotations
import os
import sys

from backbone import deps
from backbone.deps import Dep

_VLC_PAGE = "https://www.videolan.org/vlc/"


def _vlc() -> str | None:
    from backtrack.playback.libvlc import vlc
    return f"libvlc {vlc.libvlc_get_version().decode()}" if vlc else None


def _ffmpeg() -> str | None:
    from backtrack.trim.engine import _resolve_ffmpeg
    return _resolve_ffmpeg()


def _clipboard() -> str | None:
    if sys.platform == "darwin" or os.name == "nt":
        return "built in"
    return deps.which("wl-copy", "xclip", "xsel")()


def _editor() -> str | None:
    from backbone.prompt.text import _find_editor
    return _find_editor()


DEPS = [
    Dep("VLC", _vlc, "playing audio", required=True, hints={
        "macos": "brew install --cask vlc", "debian": "sudo apt install vlc",
        "fedora": "sudo dnf install vlc", "arch": "sudo pacman -S vlc",
        "windows": _VLC_PAGE, "other": _VLC_PAGE}),
    Dep("ffmpeg", _ffmpeg, "trimming audio", hints={
        "macos": "brew install ffmpeg", "debian": "sudo apt install ffmpeg",
        "fedora": "sudo dnf install ffmpeg", "arch": "sudo pacman -S ffmpeg",
        "other": "https://ffmpeg.org/download.html"}),
    Dep("clipboard", _clipboard, "copying paths and tags", hints={
        "debian": "sudo apt install wl-clipboard (Wayland) or xclip (X11)",
        "fedora": "sudo dnf install wl-clipboard (Wayland) or xclip (X11)",
        "arch": "sudo pacman -S wl-clipboard (Wayland) or xclip (X11)",
        "linux": "install wl-clipboard (Wayland) or xclip (X11)"}),
    Dep("text editor", _editor, "editing long text in an editor", hints={
        "other": "set $EDITOR, or install nano"}),
]
