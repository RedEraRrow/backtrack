"""Match cover-image files to tracks for the per-file album-art bulk op.

The inverse idea to "Derive from filename": instead of reading a track's *name*
into tags, this finds the image file that belongs to each track and pairs them
up, so a folder of ``01 - Song.mp3`` + ``01 - Song.jpg`` (or ``covers/1.png`` …)
can have each track's own artwork embedded in one pass.

Pure and unit-testable — no UI, no writes. The bulk op in ``bulk_id3_manager``
owns the preview/apply; ``tag_writer.write_cover`` owns the actual embedding.

Four pairing strategies are exposed (all return ``{track_path: image_path|None}``):

  * :func:`plan_auto`      — score every (track, image) pair and greedily assign
                             the best 1:1 matches (the recommended default).
  * :func:`plan_basename`  — strict same-stem pairing (``x.mp3`` ↔ ``x.jpg``).
  * :func:`plan_positional`— order tracks and images and zip them together.
  * :func:`plan_template`  — render a ``%token%`` pattern into the expected image
                             stem and match by name.

Scoring favours *track-specific* art: an exact name match, then a track-number
match, then title-word overlap. Whole-album names (cover/folder/front/…) rank
low on purpose — embedding one shared cover on every track is the existing
"Add album art" flow, not this one.
"""
from __future__ import annotations

import os
import re

from src.id3 import file_namer as fnm

# Image formats we can read and embed. MP3/APIC accepts all of these; MP4 `covr`
# is limited to JPEG/PNG (see :func:`mp4_storable`).
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp')

_EXT_TO_MIME = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.bmp': 'image/bmp', '.webp': 'image/webp',
}

# Sibling folders that conventionally hold artwork (matched case-insensitively).
SUBFOLDER_NAMES = ('artwork', 'covers', 'cover', 'scans', 'scan', 'art', 'images')

# A per-disc subfolder of a multi-disc release (CD1 / Disc 2 / DVD1 / Vol. 3 …).
# Tracks live in these, but the shared cover usually sits in the album folder one
# level up, so cover discovery looks there too.
_DISC_DIR_RE = re.compile(r'^(cd|dis[ck]|dvd|vol(?:ume)?|part|side)\s*[._-]?\s*\d+[a-z]?$',
                          re.IGNORECASE)


def _is_disc_dir(name: str) -> bool:
    """Whether ``name`` looks like a per-disc subfolder (CD1, Disc 2, DVD1, …)."""
    return bool(_DISC_DIR_RE.match(name.strip()))

# Whole-album / non-track-specific image names — kept as candidates but scored
# low so they never out-rank a real per-track match.
_GENERIC_NAMES = ('cover', 'folder', 'front', 'albumart', 'albumartsmall',
                  'album', 'back', 'thumb', 'thumbnail', 'artwork', 'art',
                  'scan', 'booklet', 'inlay', 'disc', 'cd')

# A confident, auto-checkable match. A track-number hit (400) clears it; a lone
# shared title word on a long title does not.
MATCH_FLOOR = 150.0

_NUM_RE = re.compile(r'\d+')
_WORD_RE = re.compile(r'[a-z0-9]{2,}')
_LEADING_NUM_RE = re.compile(r'^\s*(\d{1,3})(?!\d)')
_SXXEXX_RE = re.compile(r's\d{1,3}e(\d{1,3})', re.IGNORECASE)
_SEASON_RE = re.compile(r's(\d{1,3})e\d{1,3}', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def _is_image(name: str) -> bool:
    """True if the file extension is one of the supported image formats."""
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def find_images(directory: str) -> list[str]:
    """Image files directly in ``directory`` plus its conventional artwork
    subfolders (one level deep). For a per-disc subfolder (``CD1``/``Disc 2``/…)
    the album folder one level up is scanned too, so a cover shared across discs
    (``The Wall/Cover.jpg`` while tracks sit in ``The Wall/CD1``) is found.
    Absolute, de-duplicated, name-sorted."""
    found: list[str] = []
    seen: set[str] = set()

    def _add_dir(d: str) -> None:
        """Append every unseen image file directly inside ``d``."""
        try:
            entries = sorted(os.listdir(d))
        except OSError:
            return
        for e in entries:
            full = os.path.abspath(os.path.join(d, e))
            if _is_image(e) and os.path.isfile(full) and full not in seen:
                seen.add(full)
                found.append(full)

    def _scan(base: str) -> None:
        """Scan ``base`` and any conventional artwork subfolder one level in."""
        _add_dir(base)
        try:
            subs = sorted(os.listdir(base))
        except OSError:
            subs = []
        for sub in subs:
            subpath = os.path.join(base, sub)
            if os.path.isdir(subpath) and sub.lower() in SUBFOLDER_NAMES:
                _add_dir(subpath)

    directory = os.path.abspath(directory)
    _scan(directory)
    if _is_disc_dir(os.path.basename(directory)):
        _scan(os.path.dirname(directory))          # album folder above CD1/CD2/…
    return found


def find_images_for_track(track_path: str) -> list[str]:
    """Candidate cover images living alongside a single track."""
    return find_images(os.path.dirname(os.path.abspath(track_path)))


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _stem(path: str) -> str:
    """The file name without its directory or extension."""
    return os.path.splitext(os.path.basename(path))[0]


def _numbers(s: str) -> list[int]:
    """Every integer embedded in ``s``, in order."""
    return [int(n) for n in _NUM_RE.findall(s)]


def _words(s: str) -> set[str]:
    """Lowercased alphanumeric "words" (2+ chars) in ``s``, for overlap scoring."""
    return set(_WORD_RE.findall(s.lower()))


def _norm(s: str) -> str:
    """Collapse to lowercase alphanumerics for substring comparison."""
    return re.sub(r'[^a-z0-9]+', '', s.lower())


def track_number(track_path: str, tokens: dict[str, str] | None) -> int | None:
    """The track's number, preferring the tag, falling back to the file name
    (leading ``01``/``1-05`` number or an ``SxxExx`` episode)."""
    tokens = tokens or {}
    for key in ('tracknopad', 'track'):
        raw = str(tokens.get(key, '')).strip()
        if raw.isdigit():
            return int(raw)
    stem = _stem(track_path)
    m = _SXXEXX_RE.search(stem)
    if m:
        return int(m.group(1))
    m = _LEADING_NUM_RE.match(stem)
    if m:
        n = int(m.group(1))
        # A 4-digit run is a year, not a track — guarded by {1,3} in the regex,
        # but a leading "05 - " style is exactly what we want.
        return n
    return None


def season_number(track_path: str) -> int | None:
    """The season from an ``SxxExx`` file name, if present."""
    m = _SEASON_RE.search(_stem(track_path))
    return int(m.group(1)) if m else None


def score_match(track_path: str, tokens: dict[str, str] | None, image_path: str) -> float:
    """How strongly ``image_path`` looks like ``track_path``'s own cover.

    0 = unrelated; ~1000 = the image shares the track's exact base name. The
    caller treats anything ≥ :data:`MATCH_FLOOR` as a confident, auto-checkable
    match.
    """
    tstem = _stem(track_path).lower()
    istem = _stem(image_path).lower()
    if not istem:
        return 0.0
    if tstem == istem:
        return 1000.0

    score = 0.0

    tnum = track_number(track_path, tokens)
    inums = _numbers(istem)
    if tnum is not None and tnum in inums:
        score += 400.0
        if len(inums) == 1:
            score += 120.0          # an unambiguous "05.jpg" style name

    title = str((tokens or {}).get('title', '')).strip()
    if title:
        twords = _words(title)
        iwords = _words(istem)
        if twords:
            shared = twords & iwords
            if shared:
                score += 300.0 * (len(shared) / len(twords))
            nt = _norm(title)
            if nt and nt in _norm(istem):
                score += 200.0      # whole title appears in the image name

    if score == 0.0 and any(g in istem for g in _GENERIC_NAMES):
        score = 5.0                 # a whole-album name, kept but ranked last

    return score


def confidence(score: float) -> str:
    """Short label for the preview's confidence column."""
    if score >= 1000:
        return 'name'
    if score >= 400:
        return 'high'
    if score >= MATCH_FLOOR:
        return 'med'
    if score > 0:
        return 'low'
    return 'none'


def rank_candidates(track_path: str, tokens: dict[str, str] | None,
                    images: list[str]) -> list[tuple[float, str]]:
    """All candidate images for a track, best first: ``[(score, image_path), …]``.

    Ties break on the image name so the ordering is deterministic.
    """
    scored = [(score_match(track_path, tokens, img), img) for img in images]
    scored.sort(key=lambda t: (-t[0], os.path.basename(t[1]).lower()))
    return scored


def best_match(track_path: str, tokens: dict[str, str] | None,
               images: list[str]) -> str | None:
    """The single most likely cover for a track (≥ floor), or None."""
    ranked = rank_candidates(track_path, tokens, images)
    if ranked and ranked[0][0] >= MATCH_FLOOR:
        return ranked[0][1]
    return None


# Front-cover-ish / non-front name hints, used to pick the album cover when
# nothing pairs per-track (a lone shared cover to embed on every track).
_FRONT_COVER_NAMES = ('cover', 'front', 'folder', 'albumart', 'album')
_NON_FRONT_NAMES = ('back', 'inlay', 'inside', 'obi', 'tray', 'disc', 'cd')


def _sole_album_cover(images: list[str]) -> str | None:
    """The single obvious album-wide cover to fall back on when nothing pairs
    per-track: the lone image present, else the one clearly front-cover-named
    image. None when it's ambiguous (leave the choice to the user)."""
    imgs = [im for im in images if im]
    if len(imgs) == 1:
        return imgs[0]
    fronts = [im for im in imgs
              if any(g in _stem(im).lower() for g in _FRONT_COVER_NAMES)
              and not any(b in _stem(im).lower() for b in _NON_FRONT_NAMES)]
    return fronts[0] if len(fronts) == 1 else None


# ---------------------------------------------------------------------------
# Bulk pairing strategies → {track_path: image_path | None}
# ---------------------------------------------------------------------------

def plan_auto(tracks: list[str], images: list[str],
              token_cache: dict[str, dict] | None = None) -> dict[str, str | None]:
    """Greedy 1:1 assignment: take the highest-scoring (track, image) pair,
    claim both, repeat. Confident matches only (≥ floor); leftover tracks map to
    None. This turns a folder of ``1.jpg … 12.jpg`` into clean per-track pairs
    without two tracks fighting over one image.
    """
    token_cache = token_cache or {}
    pairs: list[tuple[float, str, str]] = []
    for t in tracks:
        toks = token_cache.get(t) or fnm.read_tokens(t)
        for img in images:
            s = score_match(t, toks, img)
            if s >= MATCH_FLOOR:
                pairs.append((s, t, img))
    # Highest score first; deterministic tie-break on names.
    pairs.sort(key=lambda p: (-p[0], os.path.basename(p[2]).lower(), p[1]))

    plan: dict[str, str | None] = {t: None for t in tracks}
    used_tracks: set[str] = set()
    used_images: set[str] = set()
    for _s, t, img in pairs:
        if t in used_tracks or img in used_images:
            continue
        plan[t] = img
        used_tracks.add(t)
        used_images.add(img)
    return plan


def plan_basename(tracks: list[str], images: list[str],
                  token_cache: dict[str, dict] | None = None) -> dict[str, str | None]:
    """Strict same-stem pairing: ``01 - Song.mp3`` ↔ ``01 - Song.jpg`` only."""
    by_stem: dict[str, str] = {}
    for img in images:
        by_stem.setdefault(_stem(img).lower(), img)   # first wins, name-sorted
    return {t: by_stem.get(_stem(t).lower()) for t in tracks}


def _positional_key(track_path: str, tokens: dict[str, str] | None) -> tuple:
    """Order tracks by (disc/season, track, name) — the natural album order."""
    tokens = tokens or {}
    disc = tokens.get('disc', '')
    disc_n = int(disc) if str(disc).isdigit() else (season_number(track_path) or 0)
    tnum = track_number(track_path, tokens)
    return (disc_n, tnum if tnum is not None else 10_000,
            os.path.basename(track_path).lower())


def _image_order_key(image_path: str) -> tuple:
    """Order images by their first embedded number, then name (natural-ish)."""
    nums = _numbers(_stem(image_path))
    return (nums[0] if nums else 10_000, os.path.basename(image_path).lower())


def plan_positional(tracks: list[str], images: list[str],
                    token_cache: dict[str, dict] | None = None) -> dict[str, str | None]:
    """Zip tracks (album order) to images (numeric/name order). Extra tracks map
    to None; extra images are ignored. Good for a ``covers/1.jpg … N.jpg`` pool
    whose names don't echo the track titles."""
    token_cache = token_cache or {}
    ordered_tracks = sorted(
        tracks, key=lambda t: _positional_key(t, token_cache.get(t) or fnm.read_tokens(t)))
    ordered_images = sorted(images, key=_image_order_key)
    plan: dict[str, str | None] = {t: None for t in tracks}
    for t, img in zip(ordered_tracks, ordered_images):
        plan[t] = img
    return plan


# ---------------------------------------------------------------------------
# Group pairing — ONE cover shared across a disc / series / work
# ---------------------------------------------------------------------------

def group_key(track_path: str, tokens: dict[str, str] | None,
              group_by: str = 'auto') -> tuple:
    """The (kind, value) a track belongs to, for one-cover-per-group art.

    ``group_by`` picks the axis: ``'disc'`` (the TPOS/disc tag), ``'season'``
    (an ``SxxExx`` file name), ``'work'`` (disc-subtitle / grouping / movement),
    or ``'auto'`` (the first of those that yields a grouping). Falls back to a
    single ``('all', 0)`` group so callers always get a key.
    """
    tokens = tokens or {}

    def _disc():
        """Group by the TPOS/disc tag, if numeric."""
        d = str(tokens.get('disc', '')).strip()
        return ('disc', int(d)) if d.isdigit() else None

    def _season():
        """Group by an SxxExx season parsed from the file name."""
        s = season_number(track_path)
        return ('season', s) if s is not None else None

    def _work():
        """Group by disc-subtitle / grouping / movement tag, whichever is set."""
        for tok in ('discsubtitle', 'grouping', 'movement'):
            v = str(tokens.get(tok, '')).strip()
            if v:
                return ('work', v)
        return None

    if group_by == 'disc':
        return _disc() or ('all', 0)
    if group_by == 'season':
        return _season() or ('all', 0)
    if group_by == 'work':
        return _work() or ('all', 0)
    return _disc() or _season() or _work() or ('all', 0)


def group_label(track_path: str, tokens: dict[str, str] | None,
                group_by: str = 'auto') -> str:
    """A short badge for a track's group (``disc 2`` / ``S1`` / a work name)."""
    kind, val = group_key(track_path, tokens, group_by)
    if kind == 'disc':
        return f"disc {val}"
    if kind == 'season':
        return f"S{val}"
    if kind == 'work':
        return str(val)[:14]
    return 'all'


def _group_sort(k: tuple) -> tuple:
    """Order group keys numerically first, then alphabetically by value."""
    kind, val = k
    if isinstance(val, int):
        return (0, val, '')
    return (1, 0, str(val).lower())


def _grouped_map(tracks: list[str], tokens: dict[str, dict],
                 group_by: str) -> dict[tuple, list[str]]:
    """Bucket tracks by their :func:`group_key`."""
    groups: dict[tuple, list[str]] = {}
    for t in tracks:
        k = group_key(t, tokens.get(t) or fnm.read_tokens(t), group_by)
        groups.setdefault(k, []).append(t)
    return groups


def plan_grouped(tracks: list[str], images: list[str], group_by: str = 'auto',
                 token_cache: dict[str, dict] | None = None) -> dict[str, str | None]:
    """Assign ONE cover per disc / series / work, shared across every track in
    that group. A group carrying a number (disc 2, season 3) is matched to the
    image whose name holds that number (preferring the least-ambiguous name);
    any group left over is matched positionally to the remaining images in
    order. ``Cover 1.jpg`` → all of season 1, ``Cover 2.jpg`` → season 2, …
    """
    token_cache = token_cache or {}
    groups = _grouped_map(tracks, token_cache, group_by)
    ordered_keys = sorted(groups, key=_group_sort)
    ordered_images = sorted(images, key=_image_order_key)

    plan: dict[str, str | None] = {t: None for t in tracks}
    used: set[str] = set()

    # Pass 1 — match a numbered group to the image bearing that number.
    for k in ordered_keys:
        _kind, val = k
        if not isinstance(val, int) or val == 0:
            continue
        cands = [img for img in ordered_images
                 if img not in used and val in _numbers(_stem(img))]
        cands.sort(key=lambda im: (len(_numbers(_stem(im))), _image_order_key(im)))
        if cands:
            used.add(cands[0])
            for t in groups[k]:
                plan[t] = cands[0]

    # Pass 2 — positionally fill any group still without a cover.
    remaining = [img for img in ordered_images if img not in used]
    ri = 0
    for k in ordered_keys:
        if any(plan[t] for t in groups[k]):
            continue
        if ri < len(remaining):
            img = remaining[ri]
            ri += 1
            for t in groups[k]:
                plan[t] = img
    return plan


def plan_best(tracks: list[str], images: list[str],
              token_cache: dict[str, dict] | None = None) -> dict[str, str | None]:
    """Auto strategy: per-track pairing, but fall back to one-cover-per-group
    when that clearly fits better — i.e. there are ≥2 real groups, each matched
    to a *distinct* image, and grouping covers more tracks than per-track does.
    This keeps normal albums per-track while catching a box set / series whose
    artwork is per disc/season.
    """
    token_cache = token_cache or {}
    per = plan_auto(tracks, images, token_cache)
    per_n = sum(1 for v in per.values() if v)

    grp = plan_grouped(tracks, images, 'auto', token_cache)
    grp_imgs = {v for v in grp.values() if v}
    grp_n = sum(1 for v in grp.values() if v)
    groups = _grouped_map(tracks, token_cache, 'auto')

    if len(groups) >= 2 and len(grp_imgs) >= 2 and grp_n > per_n:
        return grp
    # Nothing paired per-track (e.g. a single shared album cover — common on a
    # multi-disc release, `The Wall/Cover.jpg` above `CD1`/`CD2`). Put that one
    # cover on every track so it's ready to apply in one go rather than a manual
    # pick per row.
    if per_n == 0:
        sole = _sole_album_cover(images)
        if sole:
            return {t: sole for t in tracks}
    return per


def plan_template(tracks: list[str], images: list[str], pattern: str,
                  token_cache: dict[str, dict] | None = None) -> dict[str, str | None]:
    """Render ``pattern`` (``file_namer`` ``%token%`` syntax) into each track's
    expected image stem and match it, extension-agnostically, against the
    available images."""
    token_cache = token_cache or {}
    by_stem: dict[str, str] = {}
    for img in images:
        by_stem.setdefault(_norm(_stem(img)), img)
    plan: dict[str, str | None] = {}
    for t in tracks:
        toks = token_cache.get(t) or fnm.read_tokens(t)
        want = _norm(fnm.render(pattern, toks))
        plan[t] = by_stem.get(want) if want else None
    return plan


# ---------------------------------------------------------------------------
# Reading image bytes for embedding
# ---------------------------------------------------------------------------

def mime_for(image_path: str) -> str:
    """The MIME type for an image file's extension, defaulting to JPEG."""
    return _EXT_TO_MIME.get(os.path.splitext(image_path)[1].lower(), 'image/jpeg')


def mp4_storable(mime: str) -> bool:
    """MP4 ``covr`` atoms can only hold JPEG or PNG."""
    return mime in ('image/jpeg', 'image/png')


def read_image(image_path: str) -> tuple[bytes, str] | None:
    """``(data, mime)`` for an image file, or None if unreadable/empty."""
    try:
        with open(image_path, 'rb') as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return None
    return data, mime_for(image_path)
