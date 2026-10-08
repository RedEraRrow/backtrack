"""Build consistent on-disk file names from a track's tags.

The inverse of the "Derive from filename" op: instead of parsing names into
tags, this expands a ``%token%`` pattern using the file's existing tags to make
a clean, uniform file name. Pure and unit-testable; ``bulk_names`` owns the
screens and ``bulk_ops.rename_files`` does the two-phase rename.

Every kind of tag gives the tokens it has a place for (see tag_formats.TAG_HOMES).
"""
from __future__ import annotations

import os
import re

from backbone import numbering

from backtrack.id3.tag_formats import kind as tag_kind, open_tags, pair, texts


# token → human description. Order defines how the help line lists them.
TOKENS: dict[str, str] = {
    'track': 'Track number as 01',
    'tracknopad': 'Track number as 1',
    'totaltracks': 'Number of tracks on the disc',
    'disc': 'Disc number',
    'totaldiscs': 'Number of discs',
    'title': 'Track title',
    'artist': 'Track artist',
    'albumartist': 'Album artist',
    'album': 'Album',
    'discsubtitle': 'Disc subtitle',
    'year': 'Year',
    'date': 'Full date',
    'genre': 'Genre',
    'composer': 'Composer',
    'conductor': 'Conductor',
    'remixer': 'Remixer / interpreted by',
    'lyricist': 'Lyricist',
    'grouping': 'Grouping / work',
    'subtitle': 'Subtitle',
    'movement': 'Movement name',
    'movementno': 'Movement number',
    'comment': 'Comment',
    'publisher': 'Publisher / label',
    'copyright': 'Copyright',
    'isrc': 'ISRC',
    'bpm': 'BPM',
    'key': 'Musical key',
    'language': 'Language',
    'mood': 'Mood',
    'encoder': 'Encoded by',
    'originalartist': 'Original artist',
    'originalalbum': 'Original album',
}

# Ready-made patterns offered in the picker: (pattern, example rendering).
PRESETS: list[tuple[str, str]] = [
    ('%track% %title%', '01 Song.mp3'),
    ('%track% - %title%', '01 - Song.mp3'),
    ('%track% %artist% - %title%', '01 Artist - Song.mp3'),
    ('%disc%-%track% %title%', '1-01 Song.mp3'),
    ('%disc%-%track% %artist% - %title%', '1-01 Artist - Song.mp3'),
    ('%artist% - %title%', 'Artist - Song.mp3'),
    ('%album% - %track% - %title%', 'Album - 01 - Song.mp3'),
    ('%title%', 'Song.mp3'),
    ('%track:r% - %title%', 'IV - Song.mp3'),
]

# Text tokens → the field each reads (where it lives in every kind of tag: tag_formats).
_TOKEN_FIELDS: dict[str, str] = {
    'title': 'title', 'artist': 'artist', 'albumartist': 'album_artist', 'album': 'album',
    'genre': 'genre', 'composer': 'composer', 'conductor': 'conductor', 'remixer': 'remixer',
    'lyricist': 'lyricist', 'grouping': 'grouping', 'subtitle': 'subtitle', 'discsubtitle': 'disc_subtitle',
    'publisher': 'publisher', 'copyright': 'copyright', 'isrc': 'isrc', 'bpm': 'bpm',
    'key': 'key', 'language': 'language', 'mood': 'mood', 'encoder': 'encoder',
    'movement': 'movement_name', 'originalartist': 'original_artist', 'originalalbum': 'original_album',
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# %token%, optionally with a number style and case: %track:r%, %disc:en:u%.
_TOKEN_RE = re.compile(r'%([a-zA-Z]+)(?::([a-zA-Z]+))?(?::([a-zA-Z]+))?%')

def _finalize(raw: dict[str, str]) -> dict[str, str]:
    """Derive the numeric/padded tokens from raw track/disc parts."""
    tracknum = raw.pop('_tracknum', '')
    totaltracks = raw.get('totaltracks', '')
    if tracknum.isdigit():
        pad = max(2, len(totaltracks)) if totaltracks.isdigit() else 2
        raw['track'] = tracknum.zfill(pad)
        raw['tracknopad'] = str(int(tracknum))
    discnum = raw.pop('_discnum', '')
    if discnum.isdigit():
        raw['disc'] = str(int(discnum))
    return {k: v for k, v in raw.items() if v}


def read_tokens(path: str) -> dict[str, str]:
    """All available token → value pairs for a file (empty values omitted)."""
    try:
        tags = open_tags(path)[0]
    except Exception:
        return {}
    raw = {tok: '; '.join(vals) for tok, f in _TOKEN_FIELDS.items() if (vals := texts(tags, f))}
    if comment := texts(tags, 'comment'):
        raw['comment'] = comment[0]
    if (p := pair(tags, 'track')) is not None:
        raw['_tracknum'], raw['totaltracks'] = p
    if (p := pair(tags, 'disc')) is not None:
        raw['_discnum'], raw['totaldiscs'] = p
    if (p := pair(tags, 'movement_number')) is not None:
        raw['movementno'] = p[0]
    if date := texts(tags, 'year'):
        raw['date'], raw['year'] = date[0], date[0][:4]
    return _finalize(raw)


def is_supported(path: str) -> bool:
    """True if the file keeps tags a name can be made from."""
    return tag_kind(path) != 'unsupported'


def _cleanup(s: str) -> str:
    """Tidy a rendered name: collapse whitespace and drop separators orphaned
    by an empty token (e.g. an empty %artist% in '%artist% - %title%')."""
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(?:\s*[-\u2013]\s*){2,}', ' - ', s)   # collapse runs of dashes
    s = re.sub(r'^[\s\-\u2013_.]+', '', s)             # leading separators
    s = re.sub(r'[\s\-\u2013_.]+$', '', s)             # trailing separators
    return s.strip()


def sanitize(name: str) -> str:
    """Make a string safe as a file name (no path separators/illegal chars)."""
    name = _ILLEGAL.sub('', name)
    name = re.sub(r'\s+', ' ', name).strip().rstrip(' .')
    return name or 'untitled'


def render(pattern: str, tokens: dict[str, str]) -> str:
    """Expand a %token% pattern with `tokens` and sanitise → a base name (no ext)."""
    def _sub(m: re.Match) -> str:
        """Look up one %token% match's value, blank if absent.

        A trailing style renders a numeric token as roman or words:
        ``%track:r%`` → IV, ``%disc:en%`` → Two, ``%movementno:r:l%`` → iv.
        A non-numeric value is returned untouched.
        """
        val = tokens.get(m.group(1).lower(), '')
        style = (m.group(2) or '').lower()
        if val and style in numbering.STYLES:
            return numbering.render(val, style, (m.group(3) or '').lower())
        return val
    return sanitize(_cleanup(_TOKEN_RE.sub(_sub, pattern)))


def unknown_tokens(pattern: str) -> list[str]:
    """Tokens in the pattern that aren't recognised (case-insensitive)."""
    bad = set()
    for m in _TOKEN_RE.finditer(pattern):
        if m.group(1).lower() not in TOKENS:
            bad.add(m.group(1))
        elif m.group(2) and m.group(2).lower() not in numbering.STYLES:
            bad.add(f"{m.group(1)}:{m.group(2)}")
    return sorted(bad)


def artists_vary(paths: list[str], token_cache: dict[str, dict] | None = None) -> bool:
    """True if the selection has more than one distinct (non-empty) track artist:
    the signal to include %artist% in the default file-name pattern."""
    seen: set[str] = set()
    for p in paths:
        toks = (token_cache or {}).get(p) or read_tokens(p)
        a = (toks.get('artist') or '').strip().lower()
        if a:
            seen.add(a)
        if len(seen) > 1:
            return True
    return False


def plan_renames(paths: list[str], pattern: str,
                 token_cache: dict[str, dict] | None = None) -> list[tuple[str, str, str]]:
    """Compute (path, old_basename, new_basename) for each supported file.

    Names are made unique within their directory (existing files that aren't
    part of this batch also block a name), appending ' (2)', ' (3)', … Files
    whose name is unchanged are still returned (old == new) so the caller can
    show and skip them.
    """
    results: list[tuple[str, str, str]] = []
    # Per-directory set of taken names, seeded with EVERY existing file (batch
    # members included): a name on disk is occupied until vacated. A file may
    # still reclaim its OWN current name (see the `!= own` guard below).
    taken: dict[str, set[str]] = {}
    for p in paths:
        d = os.path.dirname(os.path.abspath(p))
        if d not in taken:
            try:
                taken[d] = {e.lower() for e in os.listdir(d)}
            except OSError:
                taken[d] = set()

    for p in paths:
        if not is_supported(p):
            continue
        ext = os.path.splitext(p)[1]
        old_base = os.path.basename(p)
        d = os.path.dirname(os.path.abspath(p))
        toks = (token_cache or {}).get(p) or read_tokens(p)
        base = render(pattern, toks)
        own = old_base.lower()
        candidate = f"{base}{ext}"
        n = 2
        # Skip any name that's taken, unless it's this file's own current name.
        while candidate.lower() in taken[d] and candidate.lower() != own:
            candidate = f"{base} ({n}){ext}"
            n += 1
        taken[d].discard(own)              # this file vacates its old name
        taken[d].add(candidate.lower())    # …and claims the new one
        results.append((p, old_base, candidate))
    return results
