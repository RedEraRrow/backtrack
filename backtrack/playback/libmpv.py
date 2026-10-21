"""python-mpv, loaded once for every module that plays audio: `mpv` is None
when libmpv can't be loaded (mpv not installed, or built for another
architecture), and `LOAD_ERROR` says why. player() makes a player with
backtrack's settings, mpv's warnings going to the log; Clip is an editor's
player, one file to hear parts of."""
import os
import sys

from backbone.log import log

# Where Homebrew puts libmpv (Apple silicon, Intel): ctypes' search on macOS
# looks in neither.
_MAC_LIB_DIRS = ("/opt/homebrew/lib", "/usr/local/lib")
_FALLBACK = "DYLD_FALLBACK_LIBRARY_PATH"


def _load():
    """(the mpv module, '') or (None, why it couldn't be loaded). python-mpv
    raises OSError when it can't find libmpv, AttributeError when libmpv is
    too old for it; anything at all is caught, so a missing player is
    reported (deps), never a traceback."""
    before = os.environ.get(_FALLBACK)
    if sys.platform == "darwin":                   # for this import only, not the tools run later
        os.environ[_FALLBACK] = ":".join([*_MAC_LIB_DIRS, before or os.path.expanduser("~/lib") + ":/usr/lib"])
    try:
        import mpv  # type: ignore[import-untyped]
        return mpv, ""
    except (Exception, SystemExit) as exc:         # noqa: BLE001 (see above)
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        if sys.platform == "darwin":
            if before is None:
                os.environ.pop(_FALLBACK, None)
            else:
                os.environ[_FALLBACK] = before


mpv, LOAD_ERROR = _load()


def version() -> str | None:
    """libmpv's client API version, or None without libmpv."""
    if mpv is None:
        return None
    major, minor = mpv._mpv_client_api_version()
    return f"libmpv {major}.{minor}"


def _log(level: str, component: str, message: str) -> None:
    log.warning("mpv %s %s: %s", level, component, message.strip())


def player(**options):
    """A new player for audio: mpv's `options` (underscored) over backtrack's
    own, which keep video, the user's mpv config and mpv's keys out, and send
    mpv's warnings and errors to the log."""
    return mpv.MPV(video=False, config=False, input_default_bindings=False, ytdl=False,
                   log_handler=_log, loglevel="warn", **options)


class Clip:
    """One file to hear parts of, for an editor: played from a point,
    paused, its position read as it plays. At the end of the file it stops
    there (keep-open), so it can be played from anywhere again."""

    def __init__(self, path: str):
        self._mp = player(pause=True, keep_open="yes")
        try:
            with self._mp.prepare_and_wait_for_event("file-loaded", "end-file", timeout=5):
                self._mp.loadfile(path)
            if self._mp.idle_active:                   # it ended without loading
                raise OSError("mpv couldn't open it")
        except BaseException:
            self._mp.terminate()
            raise

    def play_from(self, seconds: float) -> None:
        """Play from `seconds` in; back once the seek has landed, so position()
        reads from there."""
        try:
            with self._mp.prepare_and_wait_for_event("playback-restart", timeout=1):
                self._mp.seek(max(0.0, seconds), "absolute", "exact")
        except TimeoutError:
            log.warning("clip: the seek to %.2f s didn't land within a second", seconds)
        self._mp.pause = False

    def pause(self) -> None:
        self._mp.pause = True

    def is_playing(self) -> bool:
        return not (self._mp.pause or self._mp.eof_reached)

    def position(self) -> float:
        """Seconds in."""
        return self._mp.time_pos or 0.0

    def length(self) -> float:
        """The file's length in seconds, 0 when unknown."""
        return self._mp.duration or 0.0

    def stop(self) -> None:
        self._mp.terminate()


def clip(path: str) -> Clip | None:
    """A Clip of `path`, or None when there's no player or it can't open the
    file (logged): the editor then edits without sound."""
    if mpv is None:
        return None
    try:
        return Clip(path)
    except Exception as exc:                       # noqa: BLE001 (an editor goes on without sound)
        log.warning("couldn't open %s to play in the editor: %s", path, exc)
        return None
