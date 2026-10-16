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


def media_for(instance, path: str):
    """`path` as media for `instance` to play, every player and editor alike.

    An MP3 is read through ffmpeg's demuxer, not VLC's own: VLC's seeks in a
    variable-bitrate MP3 land by its coarse seek table (a hundred points over
    the whole file) and then report the time asked for, so after a resume, a
    skip or a seek the clock was seconds away from what's heard, and lyrics
    and timings with it. ffmpeg's land where they say they do."""
    media = instance.media_new(path)
    if path.lower().endswith('.mp3'):
        media.add_option(':demux=avformat')
    return media
