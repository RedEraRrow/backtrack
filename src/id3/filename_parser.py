"""Derive tags from file names and folder layout for the bulk "Derive" operation.

Everything here is a *pure* function of the path strings it is given (it never
reads file contents), so the whole parser can be unit-tested headlessly.

Fields derived: title, track(+total), disc(+total), disc_subtitle, album,
album_artist, artist (track/performer), year/date, and a compilation flag.

Agreed behaviour:
  * Title: `_`→space, whitespace collapsed; case never altered.
  * A leading ``ARTIST - TITLE`` (after the track number) is extracted — ARTIST
    becomes the track artist (TPE1) — only when there's evidence ARTIST really is
    an artist: it's a compilation, or the album artist appears within the prefix
    (covering exact matches and ``Artist feat. Guest``). Otherwise the ``-`` is
    assumed to be part of the title (so ``Interlude - Reprise`` stays a title).
  * album_artist (TPE2) comes from the grandparent folder. A ``Various Artists`` /
    ``Various`` / ``VA`` / ``Compilations`` grandparent marks a **compilation**
    (album_artist = "Various Artists", compilation flag set), with the per-track
    artist taken from the file name.
  * Disc from filename (``2-05``) wins over folder; ``CD/Disc/Disk/Series/Season``
    folders set the disc, and a `` · Disc 2 - Subtitle`` folder also yields a
    disc subtitle. ``SxxExx`` maps season→disc, episode→track.
  * Year/date from ``Album (1997)`` / ``1997 - Album`` folders or a filename date
    (``1952-02-20``). ``Artist - Album (Year)`` single folders split as a fallback.
"""
from __future__ import annotations

import os
import re

from src.utils import numbering
from dataclasses import dataclass, field

_AUDIO_EXTS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac', '.flac', '.ogg', '.opus', '.wav', '.wma')

# Grandparent folder names that mark a Various-Artists compilation.
_VARIOUS = {'various artists', 'various', 'va', 'v.a.', 'v/a',
            'compilation', 'compilations'}
VARIOUS_ARTISTS = 'Various Artists'

# Folder names denoting a disc/series level, optionally followed by a subtitle
# ("Disc 2 - The Remixes", "Series 1 - Origins"). Group 1 = keyword, 2 = number,
# 3 = explicit subtitle.
_DISC_FOLDER_RE = re.compile(
    r'^\s*(cd|dis[ck]|series|season)\s*[-_ ]?(\d{1,3})\b\s*[-_:]?\s*(.*)$',
    re.IGNORECASE)

_ARTIST_TITLE_RE = re.compile(r'\s*(.+?)\s+[-–—]\s+(.+)$')      # "Artist - Title"
_FULL_DATE_RE = re.compile(r'(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)')
_YEAR_PAREN_RE = re.compile(r'[\(\[](\d{4})[\)\]]')
_YEAR_LEAD_RE = re.compile(r'^\s*(\d{4})\s*[-_]\s*(.+)$')


def _is_year(s: str) -> bool:
    """True if ``s`` parses as an integer in the plausible album-year range."""
    try:
        return 1900 <= int(s) <= 2099
    except (TypeError, ValueError):
        return False


@dataclass
class Derived:
    """Fields derived for a single file. ``None`` means "not determined"."""
    title: str | None = None
    track: int | None = None
    total_tracks: int | None = None
    disc: int | None = None
    total_discs: int | None = None
    disc_subtitle: str | None = None
    album: str | None = None
    album_artist: str | None = None
    artist: str | None = None
    year: str | None = None
    compilation: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """This derivation's fields as a plain dict (excludes ``notes``)."""
        return {
            'title': self.title, 'track': self.track, 'total_tracks': self.total_tracks,
            'disc': self.disc, 'total_discs': self.total_discs,
            'disc_subtitle': self.disc_subtitle, 'album': self.album,
            'album_artist': self.album_artist, 'artist': self.artist,
            'year': self.year, 'compilation': self.compilation,
        }


def _clean_text(s: str) -> str:
    """Normalize underscores to spaces, collapse whitespace, trim stray dashes."""
    s = s.replace('_', ' ')
    s = re.sub(r'\s+', ' ', s)
    return s.strip(" -\t")


def _match_numbering(stem: str) -> dict | None:
    """Parse the leading track/disc token from a filename stem (most specific first)."""
    m = re.match(r'\s*S(\d{1,2})\s*E(\d{1,3})(?=\D|$)', stem, re.IGNORECASE)
    if m:
        return {'disc': int(m.group(1)), 'track': int(m.group(2)), 'kind': 'SxxExx', 'end': m.end()}
    m = re.match(r'\s*(\d{1,2})x(\d{1,3})(?=\D|$)', stem)
    if m:
        return {'disc': int(m.group(1)), 'track': int(m.group(2)), 'kind': 'NxM', 'end': m.end()}
    m = re.match(r'\s*(\d{1,2})-(\d{1,3})(?=\D|$)', stem)
    if m:
        return {'disc': int(m.group(1)), 'track': int(m.group(2)), 'kind': 'disc-track', 'end': m.end()}
    m = re.match(r'\s*([A-Da-d])(\d{1,2})(?=\D|$)', stem)
    if m:
        side = m.group(1).upper()
        return {'disc': ord(side) - ord('A') + 1, 'track': int(m.group(2)),
                'kind': 'vinyl', 'end': m.end()}
    m = re.match(r'\s*(\d{1,3})(?:[\s._)\].\-]+|$)', stem)
    if m:
        return {'track': int(m.group(1)), 'kind': 'track', 'end': m.end()}
    m = re.match(r'\s*[Tt]rack\s*(\d{1,3})\b[\s._)\-]*', stem)
    if m:
        return {'track': int(m.group(1)), 'kind': 'track-word', 'end': m.end()}
    return None


def _is_various(folder_name: str) -> bool:
    """True if the folder name matches a known Various-Artists spelling."""
    return _clean_text(folder_name).lower() in _VARIOUS


def _folder_disc_sub(folder_name: str) -> tuple[int | None, str | None]:
    """(disc number, subtitle) from a disc/series folder.

    Explicit trailing text is the subtitle (``Disc 2 - The Remixes`` → "The
    Remixes"). With no trailing text, a Series/Season folder uses its own label
    as the subtitle (``Series 1`` → "Series 1"), so playback shows "Series 1"
    rather than "Disc 1"; plain ``CD1``/``Disc 2`` stay subtitle-less (redundant
    with the disc number)."""
    m = _DISC_FOLDER_RE.match(folder_name)
    if not m:
        return None, None
    kw, num, tail = m.group(1).lower(), int(m.group(2)), _clean_text(m.group(3) or "")
    if tail:
        return num, tail
    if kw in ('series', 'season'):
        return num, _clean_text(folder_name)
    return num, None


def _split_artist_title(text: str) -> tuple[str | None, str]:
    """Split a leading ``Artist - Title`` candidate; returns (artist|None, title)."""
    m = _ARTIST_TITLE_RE.match(text)
    if m and m.group(1).strip() and m.group(2).strip():
        return _clean_text(m.group(1)), _clean_text(m.group(2))
    return None, _clean_text(text)


def _norm_artist(s: str) -> str:
    """Lowercase and drop a leading "the " so artist names compare equal."""
    s = _clean_text(s).lower()
    return s[4:] if s.startswith('the ') else s


def _prefix_is_artist(prefix: str | None, album_artist: str | None) -> bool:
    """True when the album artist actually appears in a filename prefix — the
    signal that the prefix is a real track artist (exact or ``feat.`` form)."""
    if not prefix or not album_artist:
        return False
    a = _norm_artist(album_artist)
    return bool(a) and a in _norm_artist(prefix)


def _folder_year_album(name: str) -> tuple[str | None, str]:
    """Pull a year out of an album folder name; return (year|None, album)."""
    m = _YEAR_PAREN_RE.search(name)
    if m and _is_year(m.group(1)):
        return m.group(1), _clean_text(name[:m.start()] + name[m.end():])
    m = _YEAR_LEAD_RE.match(name)
    if m and _is_year(m.group(1)):
        return m.group(1), _clean_text(m.group(2))
    return None, _clean_text(name)


def _split_folder_aay(name: str) -> tuple[str, str, str | None] | None:
    """Split ``Artist - Album (Year)`` / ``Artist - Album`` → (artist, album, year)."""
    year = None
    s = name
    m = _YEAR_PAREN_RE.search(s)
    if m and _is_year(m.group(1)):
        year = m.group(1)
        s = (s[:m.start()] + s[m.end():])
    m2 = _ARTIST_TITLE_RE.match(s)
    if m2 and m2.group(1).strip() and m2.group(2).strip():
        return _clean_text(m2.group(1)), _clean_text(m2.group(2)), year
    return None


def parse_one(path: str, known_artist: str | None = None, root: str | None = None) -> Derived:
    """Derive fields for one file from its name and folder chain (no totals)."""
    d = Derived()
    stem = os.path.splitext(os.path.basename(path))[0]

    # --- Leading track/disc numbering ---
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

    # --- Folder chain: album folder, disc/series folder, grandparent (artist) ---
    parent = os.path.dirname(path)
    parent_name = os.path.basename(parent)
    folder_disc, folder_sub = _folder_disc_sub(parent_name)
    if folder_disc is not None:
        album_folder = os.path.dirname(parent)
        if folder_sub:
            d.disc_subtitle = folder_sub
    else:
        album_folder = parent
    if d.disc is None and folder_disc is not None:   # filename disc wins
        d.disc = folder_disc

    grand_name = os.path.basename(os.path.dirname(album_folder))
    album_raw = os.path.basename(album_folder)
    is_comp = _is_various(grand_name)

    # Year from album folder (may also be overridden by a filename date below).
    folder_year, album_name = _folder_year_album(album_raw)
    folder_artist = _clean_text(grand_name)

    # --- "Artist - Album (Year)" fallback when there's no clean artist level ---
    no_artist_level = bool(root) and os.path.normpath(os.path.dirname(album_folder)) == os.path.normpath(root)
    aay = _split_folder_aay(album_raw)
    if aay and not is_comp and (no_artist_level or _YEAR_PAREN_RE.search(album_raw)):
        folder_artist, album_name, aay_year = aay
        folder_year = folder_year or aay_year

    d.album = album_name or None

    # --- Filename date → year (and strip it from the title residual) ---
    dm = _FULL_DATE_RE.search(residual)
    if dm:
        d.year = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
        residual = residual[:dm.start()] + residual[dm.end():]
    elif folder_year:
        d.year = folder_year

    # --- Artist / album-artist / title ---
    # Extract "ARTIST - TITLE" only with evidence the prefix is an artist: a
    # compilation, or the album artist appearing in the prefix. Otherwise the
    # "-" is treated as title punctuation and the whole residual is the title.
    fn_artist, split_title = _split_artist_title(residual)
    if is_comp:
        # Flag the compilation, but don't *write* "Various Artists": the app
        # derives that name from the tracks, so storing it would replace an
        # inference with data — and there's nothing to fill in here anyway.
        d.compilation = True
        d.artist = fn_artist                       # per-track artist from the name
        title = split_title
    else:
        d.album_artist = folder_artist or None
        if _prefix_is_artist(fn_artist, folder_artist):
            d.artist = fn_artist
            title = split_title
        else:
            d.artist = folder_artist or None
            title = _clean_text(residual)          # no artist → whole thing is title

    d.title = (title or _clean_text(stem)) or None
    return d


def _album_key(path: str) -> str:
    """The folder identifying this file's album group, skipping a disc/series level."""
    parent = os.path.dirname(path)
    if _folder_disc_sub(os.path.basename(parent))[0] is not None:
        return os.path.dirname(parent)
    return parent


def derive_all(paths: list[str], known_artist: str | None = None,
               template: str | None = None, root: str | None = None,
               regex: str | None = None, regex_base: str | None = None) -> dict[str, Derived]:
    """Derive fields for every path, then fill totals across each album group.

    ``total_tracks`` is the size of the file's (album, disc) group (or the highest
    track number seen in it). ``total_discs`` is the count of distinct discs in the
    album group — set only when disc information exists.

    With ``template`` (``%token%``) or ``regex`` (raw named groups) set — mutually
    exclusive — matched files have those fields override auto-detection; folder-
    derived values are kept for fields the override doesn't capture.
    """
    override = None
    label = ''
    if template:
        _t = compile_template(template)
        override, label = (lambda p, ka: apply_template(p, _t, ka)), 'template'
    elif regex:
        _r = compile_regex(regex)
        override, label = (lambda p, ka: apply_regex(p, _r, ka, regex_base)), 'regex'

    result = {p: parse_one(p, known_artist=known_artist, root=root) for p in paths}

    if override is not None:
        for p in paths:
            base = result[p]
            t = override(p, known_artist or base.artist)
            if t is None:
                base.notes.append(f'{label} no match')
                continue
            for fld in ('title', 'track', 'disc', 'total_tracks', 'total_discs',
                        'album', 'album_artist', 'artist', 'year'):
                v = getattr(t, fld)
                if v is not None:
                    setattr(base, fld, v)

    albums: dict[str, dict] = {}
    for p in paths:
        if os.path.splitext(p)[1].lower() not in _AUDIO_EXTS:
            continue
        d = result[p]
        albums.setdefault(_album_key(p), {}).setdefault(d.disc, []).append(p)

    for group in albums.values():
        discs_present = [dv for dv in group if dv is not None]
        # Never let the total drop below the highest disc number actually seen
        # (e.g. only "Disc 2" selected → total 2, not 1).
        total_discs = max(len(set(discs_present)), max(discs_present)) if discs_present else None
        for disc_val, members in group.items():
            tracks: list[int] = []
            for m in members:
                t = result[m].track
                if t is not None:
                    tracks.append(t)
            total_tracks = max([len(members)] + tracks) if tracks else len(members)
            for m in members:
                if result[m].total_tracks is None:
                    result[m].total_tracks = total_tracks
                if total_discs is not None and result[m].total_discs is None:
                    result[m].total_discs = total_discs
    return result


# ---------------------------------------------------------------------------
# Template override — reverse a Picard-style token pattern into fields.
# ---------------------------------------------------------------------------

# token → (regex fragment, field). Numeric tokens are greedy digits; text tokens
# are non-greedy so literals still anchor the split. %ignore% is a throwaway.
_TEMPLATE_TOKENS = {
    'track':       (r'(?P<track>\d+)', 'track'),
    'disc':        (r'(?P<disc>\d+)', 'disc'),
    'season':      (r'(?P<disc>\d+)', 'disc'),
    'episode':     (r'(?P<track>\d+)', 'track'),
    'totaltracks': (r'(?P<total_tracks>\d+)', 'total_tracks'),
    'totaldiscs':  (r'(?P<total_discs>\d+)', 'total_discs'),
    'year':        (r'(?P<year>\d{4})', 'year'),
    'date':        (r'(?P<year>\d{4}(?:-\d{2}-\d{2})?)', 'year'),
    'title':       (r'(?P<title>.+)', 'title'),
    'artist':      (r'(?P<artist>.+?)', 'artist'),
    'albumartist': (r'(?P<album_artist>.+?)', 'album_artist'),
    'album':       (r'(?P<album>.+?)', 'album'),
}


# Number styles a numeric token can match instead of digits.
_STYLE_FRAGMENTS = {
    'r': numbering.ROMAN_FRAGMENT,
    'en': numbering.WORD_FRAGMENT,
}


class TemplateError(ValueError):
    """Raised when a template string is malformed."""


# Named regex/template group → Derived field, with aliases users may write in a
# raw regex (season→disc, episode→track, date→year, etc.). Groups not listed here
# are ignored.
_GROUP_FIELD = {
    'track': 'track', 'episode': 'track',
    'disc': 'disc', 'season': 'disc',
    'total_tracks': 'total_tracks', 'totaltracks': 'total_tracks',
    'total_discs': 'total_discs', 'totaldiscs': 'total_discs',
    'year': 'year', 'date': 'year',
    'title': 'title', 'artist': 'artist',
    'album_artist': 'album_artist', 'albumartist': 'album_artist',
    'album': 'album',
}
_NUMERIC_FIELDS = ('track', 'disc', 'total_tracks', 'total_discs')


def _fields_from_groups(groups: dict, known_artist: str | None = None) -> Derived:
    """Map a regex match's named groups → a Derived (shared by template + regex).

    Numeric fields are int-parsed, text fields cleaned; unrecognised group names
    are ignored. Captured titles are taken literally (no artist-prefix stripping).
    """
    d = Derived()
    for name, val in groups.items():
        if val is None or val == '':
            continue
        field = _GROUP_FIELD.get(name)
        if field is None:
            continue
        if field in _NUMERIC_FIELDS:
            # Digits, a Roman numeral, or words — so a capture of "III" or
            # "three" lands as 3 whether it came from a template or a raw regex.
            num = numbering.parse(val)
            if num is not None:
                setattr(d, field, num)
        elif field == 'year':
            d.year = val
        else:                                   # title / artist / album_artist / album
            cleaned = _clean_text(val)
            if cleaned:
                setattr(d, field, cleaned)
    return d


def compile_regex(pattern: str) -> re.Pattern:
    """Compile a raw regex for the derive operation.

    Must contain at least one recognised named group (``(?P<track>…)`` etc.);
    raises TemplateError on bad syntax or no usable group."""
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise TemplateError(f"Invalid regex: {e}")
    if not (set(compiled.groupindex) & set(_GROUP_FIELD)):
        raise TemplateError(
            "No recognised named group — use e.g. (?P<track>\\d+), (?P<title>.+)")
    return compiled


def unrecognised_regex_groups(compiled: re.Pattern) -> list[str]:
    """Named groups the parser will ignore (for a heads-up in the UI)."""
    return [n for n in compiled.groupindex if n not in _GROUP_FIELD]


def _regex_target(path: str, base: str | None = None) -> str:
    """The string a regex runs against: the file stem, or — when ``base`` is
    given — the path relative to ``base`` (``/``-separated, extension dropped),
    so a regex can capture folder levels like Artist/Album."""
    if base:
        try:
            rel = os.path.relpath(path, base)
        except ValueError:                      # different drive on Windows
            rel = path
        rel = rel.replace(os.sep, '/')
    else:
        rel = os.path.basename(path)
    return os.path.splitext(rel)[0]


def apply_regex(path: str, compiled: re.Pattern, known_artist: str | None = None,
                base: str | None = None) -> Derived | None:
    """Apply a raw regex (search, not anchored) to a file's stem, or to its path
    relative to ``base`` when given. Returns None on no match."""
    m = compiled.search(_regex_target(path, base))
    if not m:
        return None
    return _fields_from_groups(m.groupdict(), known_artist)


def compile_template(template: str) -> re.Pattern:
    """Compile a ``%token%`` template into an anchored regex over the file stem.

    Example: ``%disc%-%track% %title%`` matches ``2-05 Song`` → disc 2, track 5,
    title 'Song'. ``%ignore%`` swallows a run of characters without capturing
    (handy for dates or noise you don't want to keep).
    """
    parts = re.split(r'(%[a-z]+(?::[a-z]+)?%)', template)
    used: set[str] = set()
    out = ['^']
    have_token = False
    for part in parts:
        if not part:
            continue
        if part.startswith('%') and part.endswith('%'):
            name, _, style = part[1:-1].partition(':')
            if name == 'ignore':
                out.append(r'.+?')
                have_token = True
                continue
            if name not in _TEMPLATE_TOKENS:
                raise TemplateError(f"Unknown token %{name}%")
            frag, group = _TEMPLATE_TOKENS[name]
            if group in used:
                raise TemplateError(f"Token for '{group}' used more than once")
            used.add(group)
            if style:
                # A number style makes the token match roman numerals or words
                # instead of digits ("Act %track:r%", "Series %disc:en%").
                if group not in _NUMERIC_FIELDS or style not in _STYLE_FRAGMENTS:
                    raise TemplateError(f"%{name}% takes no '{style}' number style")
                frag = f'(?P<{group}>{_STYLE_FRAGMENTS[style]})'
            out.append(frag)
            have_token = True
        else:
            out.append(re.escape(part))
    out.append('$')
    if not have_token:
        raise TemplateError("Template contains no %tokens%")
    try:
        return re.compile("".join(out))
    except re.error as e:  # pragma: no cover - defensive
        raise TemplateError(str(e))


def apply_template(path: str, compiled: re.Pattern, known_artist: str | None = None) -> Derived | None:
    """Apply a compiled ``%token%`` template to one file's stem (anchored match)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = compiled.match(stem)
    if not m:
        return None
    return _fields_from_groups(m.groupdict(), known_artist)
