"""Derive track / disc / title / album / artist from file names and folder layout.

Everything here is a *pure* function of the path strings it is given (it never
reads file contents), so the whole parser can be unit-tested headlessly. It backs
the bulk "Derive from filename" operation.

Design decisions (agreed with the user):
  * Title is cleaned by turning `_`→space and collapsing whitespace, and by
    stripping a leading ``Artist - `` **only** when it matches the known artist.
    Case is never altered (keeps stylised names like ``MF DOOM`` intact).
  * Folder layout assumed for album/artist: ``…/Artist/Album/track.ext`` with an
    optional ``Disc N`` folder between the album and the tracks.
  * When a filename disc prefix (``2-05``) disagrees with a ``Disc N`` folder, the
    filename wins. ``SxxExx`` episodic naming maps season→disc, episode→track.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Audio extensions we consider "tracks" when counting siblings for totals.
_AUDIO_EXTS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac', '.flac', '.ogg', '.opus', '.wav', '.wma')

# Folder names that denote a disc/CD level rather than the album itself.
_DISC_FOLDER_RE = re.compile(r'^\s*(?:cd|dis[ck])\s*[-_ ]?(\d{1,3})\b', re.IGNORECASE)


@dataclass
class Derived:
    """Fields derived for a single file. ``None`` means "not determined"."""
    title: str | None = None
    track: int | None = None
    total_tracks: int | None = None
    disc: int | None = None
    total_discs: int | None = None
    album: str | None = None
    artist: str | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            'title': self.title, 'track': self.track, 'total_tracks': self.total_tracks,
            'disc': self.disc, 'total_discs': self.total_discs,
            'album': self.album, 'artist': self.artist,
        }


def _clean_text(s: str) -> str:
    """Underscores → spaces, collapse runs of whitespace, trim. Case untouched."""
    s = s.replace('_', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip(" -\t")


def _match_numbering(stem: str) -> dict | None:
    """Parse the *leading* track/disc token from a filename stem.

    Returns a dict with any of ``disc``/``track`` set, plus ``end`` (index past
    the matched token) and ``kind`` (which pattern matched), or ``None``.
    Patterns are tried most-specific first.
    """
    # SxxExx / SxEy — episodic: season→disc, episode→track. A trailing lookahead
    # (not \b) so an underscore separator — a word char — still ends the token.
    m = re.match(r'\s*S(\d{1,2})\s*E(\d{1,3})(?=\D|$)', stem, re.IGNORECASE)
    if m:
        return {'disc': int(m.group(1)), 'track': int(m.group(2)), 'kind': 'SxxExx', 'end': m.end()}

    # 1x05 — alternate episodic form.
    m = re.match(r'\s*(\d{1,2})x(\d{1,3})(?=\D|$)', stem)
    if m:
        return {'disc': int(m.group(1)), 'track': int(m.group(2)), 'kind': 'NxM', 'end': m.end()}

    # 1-05 — disc-track prefix (filename is authoritative for disc).
    m = re.match(r'\s*(\d{1,2})-(\d{1,3})(?=\D|$)', stem)
    if m:
        return {'disc': int(m.group(1)), 'track': int(m.group(2)), 'kind': 'disc-track', 'end': m.end()}

    # A1 / B2 — vinyl side (A→disc 1, B→disc 2, …). Low confidence; flagged.
    m = re.match(r'\s*([A-Da-d])(\d{1,2})(?=\D|$)', stem)
    if m:
        side = m.group(1).upper()
        return {'disc': ord(side) - ord('A') + 1, 'track': int(m.group(2)),
                'kind': 'vinyl', 'end': m.end()}

    # 01 / 01. / 01 - / 01_  — plain leading track number. A 4-digit leading run
    # (a year) can't match \d{1,3} followed by a separator, so it is left alone.
    m = re.match(r'\s*(\d{1,3})(?:[\s._)\].\-]+|$)', stem)
    if m:
        return {'track': int(m.group(1)), 'kind': 'track', 'end': m.end()}

    # "Track 3" — worded.
    m = re.match(r'\s*[Tt]rack\s*(\d{1,3})\b[\s._)\-]*', stem)
    if m:
        return {'track': int(m.group(1)), 'kind': 'track-word', 'end': m.end()}

    return None


def _folder_disc(folder_name: str) -> int | None:
    """Disc number from a folder name like ``CD1`` / ``Disc 2`` / ``Disk_3``."""
    m = _DISC_FOLDER_RE.match(folder_name)
    return int(m.group(1)) if m else None


def _strip_artist_prefix(title: str, artist: str | None) -> str:
    """Remove a leading ``Artist - `` when it matches the known artist."""
    if not artist:
        return title
    m = re.match(r'\s*(.+?)\s+-\s+(.*)$', title)
    if m and _clean_text(m.group(1)).lower() == _clean_text(artist).lower() and m.group(2).strip():
        return m.group(2).strip()
    return title


def parse_one(path: str, known_artist: str | None = None) -> Derived:
    """Derive fields for one file from its name and folder chain (no totals)."""
    d = Derived()
    stem = os.path.splitext(os.path.basename(path))[0]

    num = _match_numbering(stem)
    residual = stem
    if num:
        residual = stem[num['end']:]
        if num.get('track') is not None:
            d.track = num['track']
        if num.get('disc') is not None:
            d.disc = num['disc']
        if num['kind'] == 'vinyl':
            d.notes.append('vinyl side inferred')

    # --- Folder-derived album / artist (skipping any Disc N level) ---
    parent = os.path.dirname(path)
    parent_name = os.path.basename(parent)
    folder_disc = _folder_disc(parent_name)
    album_folder = os.path.dirname(parent) if folder_disc is not None else parent
    album_name = _clean_text(os.path.basename(album_folder))
    artist_name = _clean_text(os.path.basename(os.path.dirname(album_folder)))
    d.album = album_name or None
    d.artist = artist_name or None

    # Filename disc wins; fall back to the folder's disc.
    if d.disc is None and folder_disc is not None:
        d.disc = folder_disc

    # --- Title from the residual, cleaned; drop a matching "Artist - " prefix ---
    title = _clean_text(residual) or _clean_text(stem)
    title = _strip_artist_prefix(title, known_artist or d.artist)
    d.title = title or None

    return d


def _album_key(path: str) -> str:
    """Group key for totals: the album folder (Disc N folders collapse together)."""
    parent = os.path.dirname(path)
    if _folder_disc(os.path.basename(parent)) is not None:
        return os.path.dirname(parent)
    return parent


def derive_all(paths: list[str], known_artist: str | None = None,
               template: str | None = None) -> dict[str, Derived]:
    """Derive fields for every path, then fill totals across each album group.

    ``total_tracks`` is the size of the file's (album, disc) group (or the highest
    track number seen in it). ``total_discs`` is the count of distinct discs in the
    album group — set only when disc information exists.

    When ``template`` is given, its tokens override the auto-detected title/track/
    disc for files it matches; folder-derived album/artist are kept unless the
    template sets them, and a "template no match" note is added otherwise.
    """
    compiled = compile_template(template) if template else None
    result = {p: parse_one(p, known_artist=known_artist) for p in paths}

    if compiled is not None:
        for p in paths:
            base = result[p]
            t = apply_template(p, compiled, known_artist=known_artist or base.artist)
            if t is None:
                base.notes.append('template no match')
                continue
            for fld in ('title', 'track', 'disc', 'total_tracks', 'total_discs', 'album', 'artist'):
                v = getattr(t, fld)
                if v is not None:
                    setattr(base, fld, v)

    # Bucket by album, then by disc, over audio files only.
    albums: dict[str, dict] = {}
    for p in paths:
        if os.path.splitext(p)[1].lower() not in _AUDIO_EXTS:
            continue
        d = result[p]
        albums.setdefault(_album_key(p), {}).setdefault(d.disc, []).append(p)

    for group in albums.values():
        discs_present = [dv for dv in group if dv is not None]
        total_discs = len(set(discs_present)) if discs_present else None
        for disc_val, members in group.items():
            tracks: list[int] = []
            for m in members:
                t = result[m].track
                if t is not None:
                    tracks.append(t)
            total_tracks = max([len(members)] + tracks) if tracks else len(members)
            for m in members:
                if result[m].total_tracks is None:      # keep any template-set total
                    result[m].total_tracks = total_tracks
                if total_discs is not None and result[m].total_discs is None:
                    result[m].total_discs = total_discs
    return result


# ---------------------------------------------------------------------------
# Template override — reverse a Picard-style token pattern into fields.
# ---------------------------------------------------------------------------

# token → (regex fragment, field it fills). Numeric tokens are greedy digits;
# text tokens are non-greedy so literals between them still anchor the split.
_TEMPLATE_TOKENS = {
    'track':       (r'(?P<track>\d+)', 'track'),
    'disc':        (r'(?P<disc>\d+)', 'disc'),
    'season':      (r'(?P<disc>\d+)', 'disc'),
    'episode':     (r'(?P<track>\d+)', 'track'),
    'totaltracks': (r'(?P<total_tracks>\d+)', 'total_tracks'),
    'totaldiscs':  (r'(?P<total_discs>\d+)', 'total_discs'),
    'title':       (r'(?P<title>.+)', 'title'),
    'artist':      (r'(?P<artist>.+?)', 'artist'),
    'album':       (r'(?P<album>.+?)', 'album'),
}


class TemplateError(ValueError):
    """Raised when a template string is malformed."""


def compile_template(template: str) -> re.Pattern:
    """Compile a ``%token%`` template into an anchored regex over the file stem.

    Example: ``%disc%-%track% %title%`` → matches ``2-05 Song`` giving
    disc=2, track=5, title='Song'. Literal text is matched verbatim.
    """
    parts = re.split(r'(%[a-z]+%)', template)
    used: set[str] = set()
    out = ['^']
    for part in parts:
        if not part:
            continue
        if part.startswith('%') and part.endswith('%'):
            name = part[1:-1]
            if name not in _TEMPLATE_TOKENS:
                raise TemplateError(f"Unknown token %{name}%")
            frag, group = _TEMPLATE_TOKENS[name]
            if group in used:
                raise TemplateError(f"Token for '{group}' used more than once")
            used.add(group)
            out.append(frag)
        else:
            out.append(re.escape(part))
    out.append('$')
    if len(used) == 0:
        raise TemplateError("Template contains no %tokens%")
    try:
        return re.compile("".join(out))
    except re.error as e:  # pragma: no cover - defensive
        raise TemplateError(str(e))


def apply_template(path: str, compiled: re.Pattern, known_artist: str | None = None) -> Derived | None:
    """Apply a compiled template to one file's stem. Returns None on no match."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = compiled.match(stem)
    if not m:
        return None
    g = m.groupdict()
    d = Derived()
    for key in ('track', 'disc', 'total_tracks', 'total_discs'):
        if g.get(key):
            setattr(d, key, int(g[key]))
    if g.get('artist'):
        d.artist = _clean_text(g['artist']) or None
    if g.get('album'):
        d.album = _clean_text(g['album']) or None
    if g.get('title'):
        title = _clean_text(g['title'])
        title = _strip_artist_prefix(title, known_artist or d.artist)
        d.title = title or None
    return d
