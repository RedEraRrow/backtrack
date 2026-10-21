"""What backtrack needs besides Python (see backbone.deps): mpv to play, and
the tools a few features use. Checked at startup for mpv, and all reported by
`backtrack doctor`."""
from __future__ import annotations
import os
import sys

from backbone import deps
from backbone.deps import Dep

def _mpv() -> str | None:
    from backtrack.playback.libmpv import version
    return version()


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
    Dep("mpv", _mpv, "playing audio", required=True, hints={
        "macos": "brew install mpv", "debian": "sudo apt install libmpv2",
        "fedora": "sudo dnf install mpv-libs", "arch": "sudo pacman -S mpv",
        "other": "https://mpv.io/installation/"}),
    Dep("ffmpeg", _ffmpeg, "trimming audio", hints={
        "macos": "brew install ffmpeg", "debian": "sudo apt install ffmpeg",
        "fedora": "sudo dnf install ffmpeg", "arch": "sudo pacman -S ffmpeg",
        "other": "https://ffmpeg.org/download.html"}),
    Dep("clipboard", _clipboard, "copying paths and tags", hints={
        "debian": "sudo apt install wl-clipboard (Wayland) or xclip (X11)",
        "fedora": "sudo dnf install wl-clipboard (Wayland) or xclip (X11)",
        "arch": "sudo pacman -S wl-clipboard (Wayland) or xclip (X11)",
        "linux": "install wl-clipboard (Wayland) or xclip (X11)"}),
    Dep("text editor", _editor, "editing long text in the system editor with Ctrl-E", hints={
        "other": "set $EDITOR, or install nano"}),
]
