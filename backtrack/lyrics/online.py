"""Lyrics from LRCLIB (lrclib.net), the open lyrics database: synced (LRC)
where it has them, else plain."""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from backbone.log import log
from backtrack.feed import fetch

_API = "https://lrclib.net/api"
_DURATION_SLACK_S = 2          # LRCLIB matches a recording by its length to within this
_RETRIES = (1.0, 3.0)          # LRCLIB answers 503 now and then: waits before asking again
_FEATURING = re.compile(r"\s+(?:feat\.?|ft\.?|featuring|with)\s+.*$", re.IGNORECASE)


def _ask(endpoint: str, **params):
    """LRCLIB's answer, asked again after a server error (5xx)."""
    url = f"{_API}/{endpoint}?{urllib.parse.urlencode(params)}"
    for wait in (*_RETRIES, None):
        try:
            return json.loads(fetch(url))
        except OSError as exc:
            if wait is None or not str(exc).startswith('5'):
                raise
            log.info("LRCLIB %s: %s, asking again in %gs", endpoint, exc, wait)
            time.sleep(wait)


def find(title: str, artist: str, album: str, duration: float) -> dict | None:
    """LRCLIB's record for a recording, or None: by its exact details first,
    else searched by title and main artist (no "feat. …"), the closest in
    length within _DURATION_SLACK_S, synced lyrics first. Raises OSError when
    LRCLIB can't be reached."""
    try:
        return _ask('get', track_name=title, artist_name=artist, album_name=album, duration=round(duration))
    except OSError as exc:
        if not str(exc).startswith('404'):
            raise
    hits = _ask('search', track_name=title, artist_name=_FEATURING.sub('', artist))
    near = [h for h in hits if abs((h.get('duration') or 0) - duration) <= _DURATION_SLACK_S]
    return min(near, key=lambda h: (not h.get('syncedLyrics'), abs(h['duration'] - duration)), default=None)


def rows(record: dict | None) -> list[tuple[str, int | None]]:
    """A record's lyrics as (text, ms) rows: timed when it has them synced,
    else untimed; [] for an instrumental or nothing."""
    from backtrack.lyrics.formats import parse_lrc_text
    if not record or record.get('instrumental'):
        return []
    if record.get('syncedLyrics'):
        return [(text, ms) for text, ms in parse_lrc_text(record['syncedLyrics']) if text]
    return [(line.strip(), None) for line in (record.get('plainLyrics') or '').splitlines() if line.strip()]
