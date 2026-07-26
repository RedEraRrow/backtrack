"""Ranged / periodic bulk tag assignment — pure logic (UI in bulk_id3_manager).

Given a set of tracks ordered by (disc, track, filename), assign a tag value to
each by position: explicit ranges, an every-N grouping, or a date schedule that
steps per track / per group / per disc. A ``{n}`` placeholder in a text value is
the 1-based group index, so "Series {n}" becomes Series 1, 2, 3, …
"""
from __future__ import annotations

import datetime
import os


def _num(v) -> int:
    """Leading integer of a tag value ('3', '3/12', 3) → 3; else 0."""
    try:
        return int(str(v).split('/')[0].strip())
    except (TypeError, ValueError):
        return 0


def order_tracks(songs: list) -> list:
    """Order song dicts by disc, then track, then filename (natural-ish)."""
    def key(s):
        """(disc, track, filename) ordering tuple for one song."""
        base = os.path.basename(str(s.get('path', ''))).lower()
        return (_num(s.get('disc')), _num(s.get('track')), base)
    return sorted(songs, key=key)


def fmt_value(template: str, n: int) -> str:
    """Substitute the ``{n}`` group counter (supports ``{n:02d}``-style padding)."""
    if '{' not in template:
        return template
    try:
        return template.format(n=n)
    except (KeyError, ValueError, IndexError):
        return template.replace('{n}', str(n))


def assign_ranges(ordered: list, ranges: list) -> dict:
    """ranges: list of (from, to, value) with 1-based inclusive positions.
    Returns {path: value}; ``{n}`` in a value = that range's index (1-based).
    Later ranges win on overlap; positions outside every range are unset."""
    out: dict = {}
    n = len(ordered)
    for i, (lo, hi, value) in enumerate(ranges, start=1):
        for pos in range(max(1, lo), min(n, hi) + 1):
            out[ordered[pos - 1]['path']] = fmt_value(str(value), i)
    return out


def assign_periodic(ordered: list, group_size: int, template: str) -> dict:
    """Group every ``group_size`` tracks; value = template with ``{n}`` = group index."""
    out: dict = {}
    if group_size < 1:
        return out
    for pos, s in enumerate(ordered, start=1):
        group = (pos - 1) // group_size + 1
        out[s['path']] = fmt_value(template, group)
    return out


def _date_group_indices(ordered: list, granularity: str, group_size: int = 1) -> list:
    """1-based group index per ordered track, for date stepping."""
    if granularity == 'track':
        return list(range(1, len(ordered) + 1))
    if granularity == 'disc':
        seen: dict = {}
        out = []
        for s in ordered:
            d = _num(s.get('disc'))
            if d not in seen:
                seen[d] = len(seen) + 1
            out.append(seen[d])
        return out
    gs = max(1, group_size)                       # 'group' / every-N
    return [(pos - 1) // gs + 1 for pos in range(1, len(ordered) + 1)]


def renumber_tracks(ordered: list, mode: str) -> dict:
    """Renumber track numbers across an ordered selection.

    'continuous' (album-relative / movement systems): 1…N straight through, total
    = N for every track. 'per_disc' (disc-relative): restart at 1 within each
    disc, total = that disc's count. Returns {path: (track, total)}."""
    out: dict = {}
    if mode == 'continuous':
        total = len(ordered)
        for i, s in enumerate(ordered, start=1):
            out[s['path']] = (i, total)
    else:                                            # per_disc
        groups: dict = {}
        for s in ordered:
            groups.setdefault(_num(s.get('disc')) or 1, []).append(s)
        for members in groups.values():
            total = len(members)
            for i, s in enumerate(members, start=1):
                out[s['path']] = (i, total)
    return out


def norm_time(t) -> str | None:
    """Normalise a 'HH:MM' / 'HH:MM:SS' time to 'HH:MM:SS', or None if invalid."""
    if not t:
        return None
    parts = str(t).strip().split(':')
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    if 0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return None


def date_groups(ordered: list, granularity: str = 'track', group_size: int = 1) -> list:
    """Distinct group indices (sorted) for the given date-stepping granularity —
    used to prompt a time per group."""
    return sorted(set(_date_group_indices(ordered, granularity, group_size)))


def assign_dates(ordered: list, start_iso: str, interval_days: int,
                 granularity: str = 'track', group_size: int = 1, times=None) -> dict:
    """Date schedule: each group's date = start + interval_days × (group-1).
    granularity ∈ {'track', 'disc', 'group'}. With ``times`` a full ISO timestamp
    is emitted: a single 'HH:MM[:SS]' string applies to all, or a {group: time}
    dict gives a per-group (e.g. per-series) time. Returns {path: value}."""
    try:
        start = datetime.date.fromisoformat(str(start_iso)[:10])
    except (ValueError, TypeError):
        return {}
    single_time = norm_time(times) if isinstance(times, str) else None
    per_group = times if isinstance(times, dict) else {}
    groups = _date_group_indices(ordered, granularity, group_size)
    out: dict = {}
    for s, g in zip(ordered, groups):
        d = (start + datetime.timedelta(days=interval_days * (g - 1))).isoformat()
        t = single_time or norm_time(per_group.get(g))
        out[s['path']] = f"{d}T{t}" if t else d
    return out
