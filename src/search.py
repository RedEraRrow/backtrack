"""Fuzzy library search: tiered matching (exact → prefix → word → substring →
subsequence → keyboard-aware typo) with search-engine-style ranking and match
spans for highlighting. Pure and unit-testable — no I/O."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field as _dcfield

# Field importance (title matches matter most). Callers can override.
DEFAULT_WEIGHTS: dict[str, float] = {
    'title': 10.0, 'artist': 7.0, 'album': 5.0, 'composer': 4.0,
    'lyricist': 3.5, 'genre': 3.0, 'people': 2.0,
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
_SOLID_BAND = 1000.0

_WORD_RE = re.compile(r'\w+')


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
        pos = 1.0 - min(p, 30) / 60.0                      # earlier hit ranks higher
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


def search(library: list, query: str, fields: list | None = None, *,
           recent: set | None = None, weights: dict | None = None,
           limit: int | None = None, min_ratio: float = 0.15) -> list:
    """Rank the library against a query. Every token must match some field (fuzzy
    AND). Contiguous ("solid") matches get a large band so exact substrings sort
    above fuzzy ones; within a band the fine score (field weight × match geometry
    + recent/play-count boosts) orders results. Results whose fine score falls
    below `min_ratio` × the best fine score are pruned. Best first."""
    fields = fields or ['title', 'artist', 'album']
    weights = weights or DEFAULT_WEIGHTS
    recent = recent or set()
    tokens = tokenize(query)
    if not tokens:
        return []

    tier_rank = {t: i for i, t in enumerate(
        ('typo', 'subsequence', 'substring', 'word', 'prefix', 'exact'))}
    raw: list = []                       # (song, fine, solid_count, matched_fields, best_tier)

    for song in library:
        vals = {f: str(song.get(f, '') or '') for f in fields}
        fine = 0.0
        solid = 0
        matched: list = []
        best_tier = 'typo'
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
                ok = False
                break
            fine += best_w
            matched.append(best_f)
            if best_m.tier in _SOLID:
                solid += 1
            if tier_rank[best_m.tier] > tier_rank[best_tier]:
                best_tier = best_m.tier
        if not ok:
            continue

        if song.get('path') in recent:
            fine *= 1.15
        try:
            fine += min(int(song.get('play_count') or 0), 20) * 0.05
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
_ENTITY_QUALITY = 1000.0
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
