"""python-vlc, loaded once for every module that plays audio: `vlc` is None
when libvlc can't be loaded (VLC not installed, or built for another
architecture), and `LOAD_ERROR` says why. python-vlc raises OSError (macOS),
NotImplementedError (Linux) or even SystemExit (a bad PYTHON_VLC_LIB_PATH)
then, not ImportError, so a narrower guard would still crash.
deps.require stops the app with how to install VLC; the editors that can
play without it just don't."""
try:
    import vlc  # type: ignore[import-untyped]
    LOAD_ERROR = ""
except (Exception, SystemExit) as exc:      # noqa: BLE001 (see above)
    vlc = None
    LOAD_ERROR = f"{type(exc).__name__}: {exc}"
