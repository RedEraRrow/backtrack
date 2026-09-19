"""Fuzzy library search: tiered matching (exact → prefix → word → substring →
subsequence → keyboard-aware typo) with search-engine-style ranking and match
spans for highlighting. Pure and unit-testable — no I/O."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field as _dcfield
from src import tuning as tune

# Field importance (title matches matter most). Callers can override.
DEFAULT_WEIGHTS: dict[str, float] = {
    'title': 10.0, 'artist': 7.0, 'album': 5.0, 'composer': 4.0,
    'lyricist': 3.5, 'genre': 3.0, 'people': 2.0, 'disc_label': 4.0,
}

# Base quality per match tier (0–1), scaled by field weight and match geometry.
_TIER = {
    'exact': 1.00, 'prefix': 0.92, 'word': 0.85,
    'substring': 0.65, 'subsequence': 0.45, 'typo': 0.30,
}

# "Solid" (contiguous) tiers. Each solid token adds a large band so any exact
# substring match sorts above purely-fuzzy (subsequence/typo) results.
# Share of the typo tier's score staked on the edits looking like typing slips
# (see _lev): at 1.0 a wholly unexplained edit would score nothing.
_TYPO_SLIP_DISCOUNT = 0.35

_SOLID = {'exact', 'prefix', 'word', 'substring'}
_SOLID_BAND = tune.SEARCH_SOLID_BAND

_WORD_RE = re.compile(r'\w+')

# Connectors in a natural "title by artist" phrasing ("Hungry Like the Wolf
# by Duran Duran"). Every other token still has to match some field — this
# just stops one filler word from sinking an otherwise-good result. If a
# track's own title happens to contain one of these ("Stand By Me"), the
# token still matches normally and scores as usual; this only changes what
# happens when it *fails* to match anywhere.
_CONNECTOR_WORDS = {'by', 'feat', 'ft', 'featuring', 'vs'}


@dataclass
class Match:
    score: float                 # 0–1 quality (pre field-weight)
    spans: list                  # [(start, end)] char ranges within the field value
    tier: str


@dataclass
class SearchResult:
    song: dict
    score: float
    matched_fields: list         # fields that contributed the best per-token match
    best_tier: str               # strongest tier seen (for a subtle quality cue)


# ---------------------------------------------------------------------------
# Low-level matchers
# ---------------------------------------------------------------------------

# Physical key geometry: a key sits at (row, col + stagger), so "adjacent" is a
# real distance rather than a hand-listed neighbour table — s/d are neighbours,
# and so are s/w and s/e one row up. QWERTY until told otherwise; utils.keyboard
# detects the real one at startup and hands it to use_layout().
_QWERTY_ROWS = ("1234567890-=", "qwertyuiop[]", "asdfghjkl;'", "zxcvbnm,./")
_KEY_STAGGER = (0.0, 0.25, 0.5, 0.75)


def _neighbours(rows: tuple[str, ...]) -> frozenset:
    """Every ordered pair of characters within one key of each other."""
    pos = {}
    for r, row in enumerate(rows):
        stagger = _KEY_STAGGER[min(r, len(_KEY_STAGGER) - 1)]
        for c, ch in enumerate(row):
            pos.setdefault(ch, (r, c + stagger))
    return frozenset(
        a + b
        for a, (ra, xa) in pos.items()
        for b, (rb, xb) in pos.items()
        if a != b and abs(ra - rb) <= 1 and abs(xa - xb) <= 1.0
    )


_NEAR_KEYS = _neighbours(_QWERTY_ROWS)


def use_layout(rows: tuple[str, ...]) -> None:
    """Score typos against this key geometry from now on (see utils.keyboard).

    Kept as a setter rather than a lookup inside the matcher so this module stays
    pure: reading the OS's keyboard settings is the caller's business, done once
    at startup, and the matcher only ever sees rows of keys.
    """
    global _NEAR_KEYS
    _NEAR_KEYS = _neighbours(rows)


def _near_key(a: str, b: str) -> bool:
    """Whether two characters sit next to each other on the current keyboard."""
    return a + b in _NEAR_KEYS


# How implausible each kind of edit is as a typing slip, 0 (a finger landing one
# key over) to 1 (an unrelated letter). A dropped or doubled keystroke sits in
# between: a common slip, but no evidence the query was aimed at this word.
_SLIP_ADJACENT = 0.0
_SLIP_INDEL    = 0.6
_SLIP_RANDOM   = 1.0

# Slip rides in the fraction of each DP cost, well below the 1.0 an edit costs,
# so a cell still compares by edit count first and only then by plausibility —
# one float per cell instead of a tuple, which this inner loop feels.
_SLIP_SCALE = 1e-4


def _lev(a: str, b: str, max_d: int):
    """Bounded Levenshtein: (distance, implausibility) or None past max_d.

    The distance is ordinary integer Levenshtein — it alone decides whether the
    words are close enough, so what counts as a typo at all is unchanged. Riding
    in the fraction of each cost is the summed slip cost of the edits, which
    breaks ties on distance in favour of the more typo-like alignment: the pair
    reads as "this far apart, and this hard to explain as a typo". "radiohesd"
    and "radiohepd" are both one edit from "radiohead"; only the first has the
    wrong finger on a neighbouring key.
    """
    la, lb = len(a), len(b)
    if abs(la - lb) > max_d:
        return None
    indel = 1.0 + _SLIP_INDEL * _SLIP_SCALE
    sub_far = 1.0 + _SLIP_RANDOM * _SLIP_SCALE
    limit = max_d + 1                      # edits ≤ max_d ⇔ cost < limit
    prev = [j * indel for j in range(lb + 1)]
    for i in range(1, la + 1):
        cur = [i * indel] + [0.0] * lb
        row_min = cur[0]
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cb = b[j - 1]
            if ca == cb:
                cost = 0.0
            else:
                cost = 1.0 if ca + cb in _NEAR_KEYS else sub_far
            cur[j] = v = min(prev[j] + indel, cur[j - 1] + indel, prev[j - 1] + cost)
            if v < row_min:
                row_min = v
        if row_min >= limit:
            return None
        prev = cur
    total = prev[lb]
    if total >= limit:
        return None
    dist = int(total)
    # Rounded off the scaling's float dust, so equal-plausibility words tie
    # exactly and _typo's tie-break falls to the earlier word rather than noise.
    return dist, round((total - dist) / _SLIP_SCALE, 6)


def _subseq(value_lower: str, token: str):
    """Greedy leftmost subsequence match. Returns (spans, run_count) or None."""
    positions: list[int] = []
    i = 0
    for j, ch in enumerate(value_lower):
        if ch == token[i]:
            positions.append(j)
            i += 1
            if i == len(token):
                break
    if i < len(token):
        return None
    spans = []
    start = prev = positions[0]
    for p in positions[1:]:
        if p == prev + 1:
            prev = p
        else:
            spans.append((start, prev + 1))
            start = prev = p
    spans.append((start, prev + 1))
    return spans, len(spans)


def _typo(value_lower: str, token: str):
    """Closest word in the value within a small edit distance.

    Returns (span, dist, slip) or None — `slip` being how poorly the edits are
    explained by neighbouring keys (see _lev). Words tie-break on it, so a query
    that fat-fingered its way to this word wins over one that merely happens to
    be the same distance away.
    """
    if len(token) < 3:
        return None
    max_d = 1 if len(token) <= 5 else 2
    best = None
    for m in _WORD_RE.finditer(value_lower):
        d = _lev(m.group(), token, max_d)
        if d is not None and (best is None or d < (best[1], best[2])):
            best = ((m.start(), m.end()), d[0], d[1])
    return best


# A subsequence match only means something when the matched characters are
# either packed together — a contracted spelling, "cabpres" for "Cabin
# Pressure" — or each sitting at the start of a word, an initialism. Scattered
# through a long value it means nothing at all: every letter of "sondheim"
# appears, in order, inside both "Thomas Trueblood and the Ridiculous Marathon"
# and "The Sark Football Team and Hovercraft Enthusiasm". Neither is a result
# anyone was looking for, and a low score is not enough to keep them out —
# scoring only decides the order of things already on screen.
#
# Requiring the match to *begin* at a word boundary is not sufficient on its
# own: the Sark title starts its run on "Sark". Density is what separates them.
_SUBSEQ_MIN_LEN = 3
_SUBSEQ_MIN_DENSITY = 0.5


def _starts_word(value_lower: str, i: int) -> bool:
    """Whether position `i` begins a word within `value_lower`."""
    return i == 0 or not value_lower[i - 1].isalnum()


def _subseq_is_meaningful(value_lower: str, token: str, spans: list) -> bool:
    """Whether a subsequence match is tight enough — or word-aligned enough — to
    be worth reporting at all."""
    if len(token) < _SUBSEQ_MIN_LEN:
        return False
    span = spans[-1][1] - spans[0][0]
    if span > 0 and len(token) / span >= _SUBSEQ_MIN_DENSITY:
        return True                        # a contraction: the letters are packed
    return all(_starts_word(value_lower, a) for a, _ in spans)   # an initialism


def match_token(token: str, value: str) -> Match | None:
    """Best match of one (lowercased) query token within a field value."""
    if not token or not value:
        return None
    vl = value.lower()
    m = len(token)

    if vl == token:
        return Match(_TIER['exact'], [(0, len(value))], 'exact')
    if vl.startswith(token):
        return Match(_TIER['prefix'], [(0, m)], 'prefix')
    for wm in _WORD_RE.finditer(vl):                       # token starts a word
        if wm.start() and vl.startswith(token, wm.start()):
            return Match(_TIER['word'], [(wm.start(), wm.start() + m)], 'word')
    p = vl.find(token)
    if p != -1:
        pos = 1.0 - min(p, tune.SEARCH_POSITION_CAP) / tune.SEARCH_POSITION_SPAN   # earlier hit ranks higher
        return Match(_TIER['substring'] * pos, [(p, p + m)], 'substring')
    ss = _subseq(vl, token)
    if ss is not None:
        spans, runs = ss
        if _subseq_is_meaningful(vl, token, spans):
            return Match(_TIER['subsequence'] / runs, spans, 'subsequence')
    ty = _typo(vl, token)
    if ty is not None:
        (a, b), dist, slip = ty
        closeness = max(0.3, 1.0 - dist / (m + 1))
        # Keyboard-plausible slips keep their full score; edits that need an
        # unrelated letter give up a third of it, which is enough to sort them
        # below their neighbour-key equivalents without demoting them past the
        # tiers above.
        plausible = 1.0 - _TYPO_SLIP_DISCOUNT * (slip / dist if dist else 0.0)
        return Match(_TIER['typo'] * closeness * plausible, [(a, b)], 'typo')
    return None


# ---------------------------------------------------------------------------
# Ranking + highlighting
# ---------------------------------------------------------------------------

def _merge_spans(spans: list) -> list:
    """Sort and coalesce overlapping/adjacent (start, end) spans into disjoint ranges."""
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def highlight_spans(value: str, tokens: list) -> list:
    """Merged char ranges in `value` matched by any token — for UI highlighting."""
    spans: list = []
    for t in tokens:
        mt = match_token(t, value)
        if mt is not None:
            spans.extend(mt.spans)
    return _merge_spans(spans)


def tokenize(query: str) -> list:
    """Split a query into lowercase whitespace-delimited search tokens."""
    return [t for t in query.lower().split() if t]


def _total_discs(song: dict) -> int:
    try:
        return int(str(song.get('total_discs', '') or '0').split('/')[0] or 0)
    except ValueError:
        return 0


def _disc_number(song: dict) -> str:
    """The track's own disc number, as tagged (e.g. "5"). Blank if unset."""
    return str(song.get('disc', '') or '').split('/')[0].strip()


def _disc_label(song: dict) -> str:
    """A disc's *fuzzy-searchable* identity, for any album with more than one
    disc: its subtitle when tagged (e.g. "Series 4"), else "Disc N". Blank
    for a single-disc album.

    Deliberately just one form, not both combined: a disc's subtitle number
    and its physical disc number can legitimately disagree (a series
    renumbered relative to its disc ordinal — disc 5 can be "Series 4"), and
    fuzzy-matching a combined string would let a bare digit in the query hit
    whichever number happens to contain it, regardless of which one the
    query meant. An exact "disc N" lookup is handled separately, as a hard
    constraint against the real disc field (`extract_disc_constraint`) —
    never through this fuzzy text.
    """
    if _total_discs(song) <= 1:
        return ''
    subtitle = str(song.get('disc_subtitle', '') or '').strip()
    if subtitle:
        return subtitle
    disc_num = _disc_number(song)
    return f"Disc {disc_num}" if disc_num else ''


def _disc_display_label(song: dict) -> str:
    """Human-facing disc label: subtitle and disc number together when both
    exist ("Series 4 (Disc 5)"), so a divergence between them is visible
    rather than hidden. For display only — never fed to the matcher."""
    label = _disc_label(song)
    if not label:
        return ''
    subtitle = str(song.get('disc_subtitle', '') or '').strip()
    disc_num = _disc_number(song)
    if subtitle and disc_num:
        return f"{subtitle} (Disc {disc_num})"
    return label


# "disc"/"cd" immediately followed by a number is treated as an exact lookup
# against the real disc field, not fuzzy text — see `_disc_label`.
_DISC_WORDS = {'disc', 'cd'}


def extract_disc_constraint(tokens: list) -> tuple[list, str | None]:
    """Pull a "disc N" / "cd N" pair out of `tokens`, if present, as a hard
    constraint on the track's actual disc number. Returns (remaining_tokens,
    disc_number) — remaining_tokens is `tokens` unchanged when no such pair
    is found. Only the first match is taken; a query naming two disc numbers
    is unusual enough not to need defining behaviour for."""
    for i in range(len(tokens) - 1):
        if tokens[i] in _DISC_WORDS and tokens[i + 1].isdigit():
            return tokens[:i] + tokens[i + 2:], tokens[i + 1]
    return tokens, None


def _field_value(song: dict, f: str) -> str:
    """A track's value for field `f` — most fields are stored directly;
    'disc_label' is computed (section: disc search)."""
    if f == 'disc_label':
        return _disc_label(song)
    return str(song.get(f, '') or '')


def search(library: list, query: str, fields: list | None = None, *,
           recent: set | None = None, weights: dict | None = None,
           limit: int | None = None, min_ratio: float = tune.SEARCH_MIN_RATIO) -> list:
    """Rank the library against a query. Every token must match some field (fuzzy
    AND) — except a connector word (_CONNECTOR_WORDS, e.g. "by"), which is
    dropped rather than sinking the result when it matches nothing, so "Hungry
    Like the Wolf by Duran Duran" isn't rejected over "by". Different tokens
    can each match a different field of the same track ("Hungry" against the
    title, "Duran" against the artist) — nothing requires them to agree.
    Contiguous ("solid") matches get a large band so exact substrings sort
    above fuzzy ones; within a band the fine score (field weight × match geometry
    + recent/play-count boosts) orders results. Results whose fine score falls
    below `min_ratio` × the best fine score are pruned. Best first."""
    fields = fields or ['title', 'artist', 'album']
    weights = weights or DEFAULT_WEIGHTS
    recent = recent or set()
    tokens = tokenize(query)
    if not tokens:
        return []

    # "disc N" is an exact lookup against the real field, not fuzzy text —
    # see `_disc_label`. Only meaningful when disc search is actually in
    # scope, so a plain title/artist search isn't affected.
    disc_number = None
    if 'disc_label' in fields:
        tokens, disc_number = extract_disc_constraint(tokens)
    if not tokens and disc_number is None:
        return []

    tier_rank = {t: i for i, t in enumerate(
        ('typo', 'subsequence', 'substring', 'word', 'prefix', 'exact'))}
    raw: list = []                       # (song, fine, solid_count, matched_fields, best_tier)

    for song in library:
        disc_matched = disc_number is not None
        if disc_matched and _disc_number(song) != disc_number:
            continue
        vals = {f: _field_value(song, f) for f in fields}
        fine = _TIER['exact'] * weights.get('disc_label', 1.0) if disc_matched else 0.0
        solid = 1 if disc_matched else 0
        matched: list = ['disc_label'] if disc_matched else []
        best_tier = 'exact' if disc_matched else 'typo'
        ok = True
        for token in tokens:
            best_m: Match | None = None
            best_f = None
            best_w = -1.0
            for f in fields:
                mt = match_token(token, vals[f])
                if mt is None:
                    continue
                w = mt.score * weights.get(f, 1.0)
                if w > best_w:
                    best_w, best_m, best_f = w, mt, f
            if best_m is None:
                if token in _CONNECTOR_WORDS:
                    continue
                ok = False
                break
            fine += best_w
            matched.append(best_f)
            if best_m.tier in _SOLID:
                solid += 1
            if tier_rank[best_m.tier] > tier_rank[best_tier]:
                best_tier = best_m.tier
        # `ok` alone isn't enough: a query that's nothing but connector words
        # ("by", "vs"...) leaves it True with no real match at all, which
        # must not be treated as "everything matches".
        if not ok or not matched:
            continue

        if song.get('path') in recent:
            fine *= tune.SEARCH_RECENCY_BOOST
        try:
            fine += (min(int(song.get('play_count') or 0), tune.SEARCH_PLAY_COUNT_CAP)
                     * tune.SEARCH_PLAY_COUNT_WEIGHT)
        except (TypeError, ValueError):
            pass

        seen: set = set()
        mf = [f for f in matched if not (f in seen or seen.add(f))]
        raw.append((song, fine, solid, mf, best_tier))

    if not raw:
        return []

    floor = max(r[1] for r in raw) * min_ratio
    results = [SearchResult(song, solid * _SOLID_BAND + fine, mf, bt)
               for (song, fine, solid, mf, bt) in raw if fine >= floor]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit] if limit else results


# ---------------------------------------------------------------------------
# Entity grouping
# ---------------------------------------------------------------------------

# Fields that name a *thing* rather than describing a track, and the label each
# gets in the results. Searching "john" over a library with 42 John Finnemore
# episodes should surface the artist once, not the episodes forty-two times.
ENTITY_FIELDS = ('artist', 'album', 'composer', 'lyricist', 'genre', 'people')

# Weight of group size against match quality when ranking entities. Quality
# dominates (a near-exact name beats a vague one however large), but among
# comparably good matches the bigger group wins — that is the whole point of
# collapsing them.
_ENTITY_QUALITY = tune.SEARCH_ENTITY_QUALITY
_ENTITY_SIZE = 50.0

_MULTI_SPLIT_RE = re.compile(r'\s*[;/,]\s*')


@dataclass
class Entity:
    """A named thing several result tracks share — an artist, album, genre."""
    kind: str                              # one of ENTITY_FIELDS
    name: str
    tracks: list = _dcfield(default_factory=list)
    score: float = 0.0
    subtitle: str = ''                     # e.g. an album's artist


def _entity_values(song: dict, kind: str) -> list:
    """The distinct names a song contributes to `kind`.

    Multi-value fields are split, so a track credited "Barlow; Williams" counts
    towards both artists rather than towards a single fused name that matches
    neither well.
    """
    raw = str(song.get(kind, '') or '').strip()
    if not raw:
        return []
    if kind == 'album':                    # album titles legitimately contain / and ,
        return [raw]
    return [v for v in (x.strip() for x in _MULTI_SPLIT_RE.split(raw)) if v]


def collect_entities(results: list, tokens: list, kinds: tuple = ENTITY_FIELDS,
                     min_tracks: int = 1) -> dict:
    """Group `results` into named entities whose own name matches the query.

    Returns ``{kind: [Entity, ...]}``, best first within each kind. Only names
    that themselves match are kept: a track can match on its title while its
    album does not, and collapsing it under that album would claim a match the
    album never made.
    """
    if not tokens:
        return {k: [] for k in kinds}

    buckets: dict = {k: {} for k in kinds}
    for r in results:
        for kind in kinds:
            for name in _entity_values(r.song, kind):
                key = name.casefold()
                ent = buckets[kind].get(key)
                if ent is None:
                    quality = _name_quality(name, tokens)
                    if quality is None:
                        buckets[kind][key] = False     # remember the miss
                        continue
                    ent = Entity(kind=kind, name=name, score=quality)
                    if kind == 'album':
                        ent.subtitle = str(r.song.get('albumartist')
                                           or r.song.get('artist') or '').strip()
                    buckets[kind][key] = ent
                elif ent is False:
                    continue
                ent.tracks.append(r.song)

    out: dict = {}
    for kind in kinds:
        ents = [e for e in buckets[kind].values()
                if e is not False and len(e.tracks) >= min_tracks]
        for e in ents:
            e.score = e.score * _ENTITY_QUALITY + math.log1p(len(e.tracks)) * _ENTITY_SIZE
        ents.sort(key=lambda e: (-e.score, e.name.casefold()))
        out[kind] = ents
    return out


def collect_disc_entities(results: list, tokens: list, min_tracks: int = 1) -> list:
    """Group `results` into per-disc entities within multi-disc albums — e.g.
    "John Finnemore's Souvenir Programme Series 1" should surface just that
    disc's tracks, not the whole album's. Grouped by (artist, album, disc
    number), never by the disc label alone: two different albums can each
    have a "Disc 1" or even both happen to call one "Series 1", and those
    must stay distinct groups. Only albums that actually have more than one
    disc are considered — a single-disc album has no separate disc to find.

    An explicit "disc N" in the query (`extract_disc_constraint`) is checked
    against the real disc field directly, same as `search`; the remaining
    tokens are matched fuzzily against `_disc_label` alone (never the
    combined display form — see its docstring for why that would let "series
    4" and "disc 4" cross-match a disc where those two numbers disagree).
    """
    if not tokens:
        return []
    tokens, disc_number = extract_disc_constraint(tokens)
    if not tokens and disc_number is None:
        return []

    buckets: dict = {}
    for r in results:
        song = r.song
        if not _disc_label(song):   # blank for a single-disc album — see _disc_label
            continue
        if disc_number is not None and _disc_number(song) != disc_number:
            continue
        album = str(song.get('album', '') or '').strip()
        if not album:
            continue
        disc_num = _disc_number(song) or '1'
        artist = str(song.get('album_artist') or song.get('artist') or '').strip()
        key = (artist.casefold(), album.casefold(), disc_num)

        ent = buckets.get(key)
        if ent is False:
            continue
        if ent is None:
            if tokens:
                quality = _name_quality(f"{album} — {_disc_label(song)}", tokens)
                if quality is None:
                    buckets[key] = False       # remember the miss
                    continue
            else:
                quality = 1.0   # the disc-number constraint alone already decided this
            name = f"{album} — {_disc_display_label(song)}"
            ent = Entity(kind='disc', name=name, score=quality, subtitle=artist)
            buckets[key] = ent
        ent.tracks.append(song)

    ents = [e for e in buckets.values() if e is not False and len(e.tracks) >= min_tracks]
    for e in ents:
        e.score = e.score * _ENTITY_QUALITY + math.log1p(len(e.tracks)) * _ENTITY_SIZE
    ents.sort(key=lambda e: (-e.score, e.name.casefold()))
    return ents


def _name_quality(name: str, tokens: list) -> float | None:
    """Mean match quality (0-1) of `name` against every token, or None if some
    token does not match it at all."""
    total = 0.0
    for tok in tokens:
        m = match_token(tok, name)
        if m is None:
            return None
        total += m.score
    return total / len(tokens)
