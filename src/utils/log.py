"""Diagnostics log: ~/.config/backtrack/backtrack.log while the `debug` setting
is on (Settings → Diagnostics). Off, it costs a level check and nothing more.

    from src.utils.log import log
    log.debug("redrew in %.0f ms", ms)
"""
import logging
from logging.handlers import RotatingFileHandler

log = logging.getLogger("backtrack")
log.addHandler(logging.NullHandler())
log.propagate = False
log.setLevel(logging.CRITICAL + 1)          # silent until configure(True)


def log_path():
    """Where the log is written."""
    from src.config import CONFIG_DIR
    return CONFIG_DIR / "backtrack.log"


def configure(enabled: bool) -> None:
    """Start or stop writing the log (the `debug` setting)."""
    for h in [h for h in log.handlers if isinstance(h, RotatingFileHandler)]:
        log.removeHandler(h)
        h.close()
    if not enabled:
        log.setLevel(logging.CRITICAL + 1)
        return
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
                                           "%H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    log.info("diagnostics log started")


def enabled() -> bool:
    """Whether anything is being logged, for callers whose message is costly to build."""
    return log.isEnabledFor(logging.DEBUG)


class quietly:
    """`with quietly():` carries on past a failure that mustn't stop anything
    (best-effort cleanup, a cosmetic redraw), but notes it in the diagnostics
    log, so a swallowed error still leaves a trace when Diagnostics is on."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb) -> bool:
        if kind is None or not issubclass(kind, Exception):
            return False
        if log.isEnabledFor(logging.DEBUG):
            import os
            where = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
            log.debug("ignored at %s: %s: %s", where, kind.__name__, exc)
        return True
