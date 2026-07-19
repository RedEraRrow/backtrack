"""Fuzzy library search: tiered matching (exact → prefix → word → substring →
subsequence → typo) with search-engine-style ranking and match spans for
highlighting. Pure and unit-testable — no I/O."""
from __future__ import annotations

import re
from dataclasses import dataclass, field as _dcfield

# Field importance (title matches matter most). Callers can override.
DEFAULT_WEIGHTS: dict[str, float] = {
    'title': 10.0, 'artist': 7.0, 'album': 5.0, 'genre': 3.0, 'people': 2.0,
}

# Base quality per match tier (0–1), scaled by field weight and match geometry.
_TIER = {
    'exact': 1.00, 'prefix': 0.92, 'word': 0.85,
    'substring': 0.65, 'subsequence': 0.45, 'typo': 0.30,
}

# "Solid" (contiguous) tiers. Each solid token adds a large band so any exact
# substring match sorts above purely-fuzzy (subsequence/typo) results.
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

def _lev(a: str, b: str, max_d: int) -> int | None:
    """Levenshtein distance, bounded: returns None once it must exceed max_d."""
    la, lb = len(a), len(b)
    if abs(la - lb) > max_d:
        return None
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        row_min = cur[0]
        ca = a[i - 1]
        for j in range(1, lb + 1):
            cost = 0 if ca == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < row_min:
                row_min = cur[j]
        if row_min > max_d:
            return None
        prev = cur
    return prev[lb] if prev[lb] <= max_d else None


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
    """Closest word in the value within a small edit distance. (span, dist) or None."""
    if len(token) < 3:
        return None
    max_d = 1 if len(token) <= 5 else 2
    best = None
    for m in _WORD_RE.finditer(value_lower):
        d = _lev(m.group(), token, max_d)
        if d is not None and (best is None or d < best[1]):
            best = ((m.start(), m.end()), d)
    return best


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
        return Match(_TIER['subsequence'] / runs, spans, 'subsequence')
    ty = _typo(vl, token)
    if ty is not None:
        (a, b), dist = ty
        return Match(_TIER['typo'] * max(0.3, 1.0 - dist / (m + 1)), [(a, b)], 'typo')
    return None


# ---------------------------------------------------------------------------
# Ranking + highlighting
# ---------------------------------------------------------------------------

def _merge_spans(spans: list) -> list:
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
