"""Build consistent on-disk file names from a track's tags.

The inverse of the "Derive from filename" op: instead of parsing names into
tags, this expands a ``%token%`` pattern using the file's existing tags to make
a clean, uniform file name. Pure and unit-testable; the bulk op in
``bulk_id3_manager`` handles the UI, preview, and the actual (two-phase) rename.

MP3 exposes the full token set; MP4/m4a exposes the common atoms.
"""
from __future__ import annotations

import os
import re

from mutagen.id3 import ID3, ID3NoHeaderError  # type: ignore[attr-defined]
from mutagen.mp4 import MP4  # type: ignore[reportPrivateImportUsage]


# token → human description. Order defines how the help line lists them.
TOKENS: dict[str, str] = {
    'track': 'Track number, zero-padded (01)',
    'tracknopad': 'Track number, no padding (1)',
    'totaltracks': 'Number of tracks on the disc',
    'disc': 'Disc number',
    'totaldiscs': 'Number of discs',
    'title': 'Track title',
    'artist': 'Track artist',
    'albumartist': 'Album artist',
    'album': 'Album',
    'discsubtitle': 'Disc subtitle',
    'year': 'Year (YYYY)',
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
]

# Single-text ID3 frames that map one-to-one to a token.
_ID3_TEXT: dict[str, str] = {
    'title': 'TIT2', 'artist': 'TPE1', 'albumartist': 'TPE2', 'album': 'TALB',
    'genre': 'TCON', 'composer': 'TCOM', 'conductor': 'TPE3', 'remixer': 'TPE4',
    'lyricist': 'TEXT', 'grouping': 'TIT1', 'subtitle': 'TIT3', 'discsubtitle': 'TSST',
    'publisher': 'TPUB', 'copyright': 'TCOP', 'isrc': 'TSRC', 'bpm': 'TBPM',
    'key': 'TKEY', 'language': 'TLAN', 'mood': 'TMOO', 'encoder': 'TSSE',
    'movement': 'MVNM', 'originalartist': 'TOPE', 'originalalbum': 'TOAL',
}

# The common MP4 atoms.
_MP4_TEXT: dict[str, str] = {
    'title': '\xa9nam', 'artist': '\xa9ART', 'albumartist': 'aART', 'album': '\xa9alb',
    'genre': '\xa9gen', 'composer': '\xa9wrt', 'grouping': '\xa9grp', 'comment': '\xa9cmt',
}

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TOKEN_RE = re.compile(r'%([a-zA-Z]+)%')

_MP3_EXTS = ('.mp3',)
# Raw .aac (ADTS) has no MP4 atoms — not tag-writable (see tag_writer).
_MP4_EXTS = ('.m4a', '.mp4', '.m4p')


def _split_frac(raw: str) -> tuple[str, str]:
    """'3/12' → ('3', '12'); '3' → ('3', '')."""
    n, _, tot = str(raw).replace('⁄', '/').partition('/')
    return n.strip(), tot.strip()


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


def _read_id3(path: str) -> dict[str, str]:
    """Read renaming tokens from an ID3 (MP3) file's tags."""
    try:
        audio = ID3(path)
    except (ID3NoHeaderError, OSError, Exception):
        return {}
    raw: dict[str, str] = {}
    for tok, fid in _ID3_TEXT.items():
        frame = audio.get(fid)
        text = getattr(frame, 'text', None)
        if text:
            joined = '; '.join(str(x) for x in text if str(x).strip())
            if joined:
                raw[tok] = joined
    for key in audio.keys():
        if key.startswith('COMM'):
            text = getattr(audio[key], 'text', None)
            if text and str(text[0]).strip():
                raw['comment'] = '; '.join(str(x) for x in text if str(x).strip())
                break
    trck = audio.get('TRCK')
    if trck is not None and trck.text:
        raw['_tracknum'], raw['totaltracks'] = _split_frac(str(trck.text[0]))
    tpos = audio.get('TPOS')
    if tpos is not None and tpos.text:
        raw['_discnum'], raw['totaldiscs'] = _split_frac(str(tpos.text[0]))
    mvin = audio.get('MVIN')
    if mvin is not None and mvin.text:
        raw['movementno'] = _split_frac(str(mvin.text[0]))[0]
    tdrc = audio.get('TDRC')
    if tdrc is not None and tdrc.text:
        date = str(tdrc.text[0]).strip()
        if date:
            raw['date'] = date
            raw['year'] = date[:4]
    return _finalize(raw)


def _read_mp4(path: str) -> dict[str, str]:
    """Read renaming tokens from an MP4/M4A file's atoms."""
    try:
        audio = MP4(path)
    except (OSError, Exception):
        return {}
    raw: dict[str, str] = {}
    for tok, atom in _MP4_TEXT.items():
        val = audio.get(atom)
        if val:
            s = str(val[0]).strip()
            if s:
                raw[tok] = s
    trkn = audio.get('trkn')
    if trkn:
        parts = list(trkn[0]) + [0, 0]
        raw['_tracknum'] = str(parts[0]) if parts[0] else ''
        raw['totaltracks'] = str(parts[1]) if parts[1] else ''
    disk = audio.get('disk')
    if disk:
        parts = list(disk[0]) + [0, 0]
        raw['_discnum'] = str(parts[0]) if parts[0] else ''
        raw['totaldiscs'] = str(parts[1]) if parts[1] else ''
    day = audio.get('\xa9day')
    if day:
        date = str(day[0]).strip()
        if date:
            raw['date'] = date
            raw['year'] = date[:4]
    return _finalize(raw)


def read_tokens(path: str) -> dict[str, str]:
    """All available token → value pairs for a file (empty values omitted)."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _MP3_EXTS:
        return _read_id3(path)
    if ext in _MP4_EXTS:
        return _read_mp4(path)
    return {}


def is_supported(path: str) -> bool:
    """True if the file's extension is a renamable MP3 or MP4 type."""
    return os.path.splitext(path)[1].lower() in _MP3_EXTS + _MP4_EXTS


def _cleanup(s: str) -> str:
    """Tidy a rendered name: collapse whitespace and drop separators orphaned
    by an empty token (e.g. an empty %artist% in '%artist% - %title%')."""
    s = re.sub(r'\s+', ' ', s)
    s = re.sub(r'(?:\s*[-–]\s*){2,}', ' - ', s)   # collapse runs of dashes
    s = re.sub(r'^[\s\-–_.]+', '', s)             # leading separators
    s = re.sub(r'[\s\-–_.]+$', '', s)             # trailing separators
    return s.strip()


def sanitize(name: str) -> str:
    """Make a string safe as a file name (no path separators/illegal chars)."""
    name = _ILLEGAL.sub('', name)
    name = re.sub(r'\s+', ' ', name).strip().rstrip(' .')
    return name or 'untitled'


def render(pattern: str, tokens: dict[str, str]) -> str:
    """Expand a %token% pattern with `tokens` and sanitise → a base name (no ext)."""
    def _sub(m: re.Match) -> str:
        """Look up one %token% match's value, blank if absent."""
        return tokens.get(m.group(1).lower(), '')
    return sanitize(_cleanup(_TOKEN_RE.sub(_sub, pattern)))


def unknown_tokens(pattern: str) -> list[str]:
    """Tokens in the pattern that aren't recognised (case-insensitive)."""
    return sorted({m.group(1) for m in _TOKEN_RE.finditer(pattern)
                   if m.group(1).lower() not in TOKENS})


def artists_vary(paths: list[str], token_cache: dict[str, dict] | None = None) -> bool:
    """True if the selection has more than one distinct (non-empty) track artist
    — the signal to include %artist% in the default file-name pattern."""
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
    # members included) — a name on disk is occupied until vacated. A file may
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
        # Skip any name that's taken — unless it's this file's own current name.
        while candidate.lower() in taken[d] and candidate.lower() != own:
            candidate = f"{base} ({n}){ext}"
            n += 1
        taken[d].discard(own)              # this file vacates its old name
        taken[d].add(candidate.lower())    # …and claims the new one
        results.append((p, old_base, candidate))
    return results
