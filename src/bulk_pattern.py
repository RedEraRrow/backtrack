"""Ranged / periodic bulk tag assignment — pure logic (UI in bulk_id3_manager).

Given a set of tracks ordered by (disc, track, filename), assign a tag value to
each by position: explicit ranges, an every-N grouping, or a date schedule that
steps per track / per group / per disc. A counter placeholder in a text value is
the 1-based group index, in any of three number styles:

    "Series {n}"  → Series 1, 2, 3, …      (arabic; "{n:02d}" pads)
    "Act {r}"     → Act I, II, III, …      (roman; "{r:l}" for i, ii, iii)
    "Series {en}" → Series One, Two, …     (written out; "{en:l}", "{en:u}", "{en:t}")
"""
from __future__ import annotations

import datetime
import os
import re

from src.utils import datetime_parse as dtp
from src.utils import numbering


def _num(v) -> int:
    """Leading integer of a tag value ('3', '3/12', 3) → 3; else 0."""
    try:
        return int(str(v).split('/')[0].strip())
    except (TypeError, ValueError):
        return 0


def _fnum(v) -> float:
    """Leading number of a tag value, keeping a fraction ('1.5/4' → 1.5); else 0.0.

    Disc numbers are the one place a non-integer is meaningful: numbering a disc
    ``1.5`` is how you park a disc between two others before reflowing.  Parsing
    those with :func:`_num` yielded 0, which sorted the new disc *before* disc 1
    and made the ordering — and so every position-based operation — wrong.
    """
    try:
        return float(str(v).split('/')[0].strip())
    except (TypeError, ValueError):
        return 0.0


def order_tracks(songs: list) -> list:
    """Order song dicts by disc, then track, then filename (natural-ish).

    Disc and track sort on :func:`_fnum`, so an interleaved ``1.5`` disc lands
    between 1 and 2 instead of ahead of everything.
    """
    def key(s):
        """(disc, track, filename) ordering tuple for one song."""
        base = os.path.basename(str(s.get('path', ''))).lower()
        return (_fnum(s.get('disc')), _fnum(s.get('track')), base)
    return sorted(songs, key=key)


def _fmt_disc(x: float) -> str:
    """Render a disc number without a trailing '.0' (2.0 → '2', 1.5 → '1.5')."""
    return str(int(x)) if float(x).is_integer() else str(x)


def disc_ranges(ordered: list) -> list:
    """Contiguous (from, to, disc_label) runs of the same disc in `ordered`.

    Positions are 1-based inclusive, matching the range editors.  `ordered` is
    already disc-sorted, so each disc forms one run — this is what seeds the
    per-range schedule editor with a row per disc.
    """
    runs: list = []
    for pos, s in enumerate(ordered, start=1):
        d = _fnum(s.get('disc'))
        if runs and runs[-1][2] == d:
            runs[-1][1] = pos
        else:
            runs.append([pos, pos, d])
    return [(lo, hi, _fmt_disc(d)) for lo, hi, d in runs]


# Group-counter placeholders: {n} arabic, {r} roman, {en} written out, each
# with an optional ':spec' — a case modifier for {r}/{en} ("l", "u", "t") or a
# format spec for {n} ("{n:02d}").
_COUNTER_RE = re.compile(r'\{(n|r|en)(?::([^}]*))?\}')


def fmt_value(template: str, n: int) -> str:
    """Substitute the group counter in `template`.

    ``Series {n}`` → Series 3, ``Act {r}`` → Act III, ``Series {en}`` → Series
    Three. ``{n:02d}``-style padding still works, and ``{r:l}`` / ``{en:u}``
    change the case. Unknown braces are left alone.
    """
    if '{' not in template:
        return template
    return _COUNTER_RE.sub(
        lambda m: numbering.render(n, m.group(1), m.group(2) or ''), template)


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
            d = _fnum(s.get('disc'))
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
            groups.setdefault(_fnum(s.get('disc')) or 1.0, []).append(s)
        for members in groups.values():
            total = len(members)
            for i, s in enumerate(members, start=1):
                out[s['path']] = (i, total)
    return out


def norm_time(t) -> str | None:
    """Normalise a 'HH:MM' / 'HH:MM:SS' time to 'HH:MM:SS', or None if invalid.

    Kept as the name the schedule code already calls; the rules live with every
    other date/time reading in :mod:`src.utils.datetime_parse`.
    """
    return dtp.parse_time(t)


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


def parse_start(raw) -> tuple:
    """Parse a schedule's start cell into ``(date, time_or_None, error)``.

    Reads whatever :func:`src.utils.datetime_parse.parse_datetime` accepts and
    keeps any time given.  A schedule steps in whole days, so a year- or
    month-only start is rejected here rather than being completed to the 1st
    behind your back — that is this caller's rule, not the parser's.

    ``error`` is '' on success, otherwise a short reason naming what was wrong.
    """
    parsed = dtp.parse_datetime(raw)
    if parsed.error:
        return None, None, parsed.error
    if parsed.precision in ('year', 'month'):
        return None, None, (f"{str(raw).strip()!r} has no day — a schedule counts in "
                            "days, so it needs a full year-month-day")
    return parsed.date, parsed.time, ''


def validate_schedule_rows(rows: list, n_tracks: int) -> tuple:
    """Turn the range editor's rows into schedule specs, reporting bad ones.

    Returns ``(specs, errors)`` where each spec is
    ``(from, to, start_date, time_or_None, interval_days, step)`` and each error
    is a human-readable ``'row N: …'`` string.  Rows are checked up front so a
    typo is named before anything is written, instead of the row vanishing from
    the result with only a count to show for it.
    """
    specs: list = []
    errors: list = []
    for i, row in enumerate(rows, start=1):
        cells = list(row) if isinstance(row, (list, tuple)) else [row]
        cells += [''] * (5 - len(cells))
        raw_lo, raw_hi, raw_start, raw_every, raw_step = cells[:5]
        if not any(str(c).strip() for c in cells[:4]):
            continue                                    # blank row — ignore
        try:
            lo, hi = int(str(raw_lo).strip()), int(str(raw_hi).strip())
        except (TypeError, ValueError):
            errors.append(f"row {i}: FROM/TO must both be track positions")
            continue
        if lo > hi:
            errors.append(f"row {i}: FROM ({lo}) is after TO ({hi})")
            continue
        if hi < 1 or lo > n_tracks:
            errors.append(f"row {i}: positions {lo}-{hi} are outside 1-{n_tracks}")
            continue
        start, tod, err = parse_start(raw_start)
        if err:
            errors.append(f"row {i}: {err}")
            continue
        try:
            every = int(str(raw_every).strip())
        except (TypeError, ValueError):
            errors.append(f"row {i}: EVERY must be a number of days")
            continue
        if every < 0:
            errors.append(f"row {i}: EVERY cannot be negative")
            continue
        step = str(raw_step).strip().lower() or 'track'
        if not (step.startswith('track') or step.startswith('disc')):
            errors.append(f"row {i}: STEP must be 'track' or 'disc'")
            continue
        specs.append((lo, hi, start, tod, every, step))
    return specs, errors


def assign_range_schedules(ordered: list, specs: list) -> dict:
    """Per-range date schedules: every range carries its own start and interval.

    ``specs`` come from :func:`validate_schedule_rows` —
    ``(from, to, start_date, time_or_None, interval_days, step)`` with 1-based
    inclusive positions.  Within a range the date advances by ``interval_days``
    once per track, or once per disc when ``step`` is ``'disc'``.  That is what
    lets disc 1 run weekly from one date while disc 3 runs fortnightly from
    another.

    A range carrying a time emits a full ``YYYY-MM-DDTHH:MM:SS``; without one the
    value stays a bare date, which is a perfectly valid TDRC precision.  Later
    ranges win on overlap, matching :func:`assign_ranges`.
    """
    out: dict = {}
    n = len(ordered)
    for spec in specs:
        lo, hi, start, tod, interval, step = (list(spec) + ['track'])[:6]
        # Accept a raw string start too, so a direct caller gets the same leniency.
        if not isinstance(start, datetime.date):
            start, parsed_tod, err = parse_start(start)
            if err:
                continue
            tod = tod or parsed_tod
        try:
            iv = int(str(interval).strip())
            lo_c, hi_c = max(1, int(lo)), min(n, int(hi))
        except (ValueError, TypeError):
            continue
        if lo_c > hi_c:
            continue
        idx = -1
        prev_disc = None
        for pos in range(lo_c, hi_c + 1):
            s = ordered[pos - 1]
            if str(step).strip().lower().startswith('disc'):
                d = _fnum(s.get('disc'))
                if prev_disc is None or d != prev_disc:
                    idx += 1
                    prev_disc = d
            else:
                idx += 1
            day = (start + datetime.timedelta(days=iv * idx)).isoformat()
            out[s['path']] = f"{day}T{tod}" if tod else day
    return out


def reflow_discs(ordered: list, renumber: bool = True, disc_totals: bool = True,
                 track_totals: bool = False) -> dict:
    """Re-flow disc numbering across an ordered selection.

    With ``renumber`` the distinct disc numbers are mapped, in order, onto a
    dense ``1…N``.  That single rule covers all three edits: a disc parked at
    ``1.5`` becomes 2 and everything above it shifts up; a deleted disc closes
    the gap and everything above shifts down; a disc appended at the end keeps
    its number and only the totals move.  ``disc_totals`` sets the "of N" half to
    the disc count, and ``track_totals`` additionally rewrites each track's track
    total to its own disc's track count (keeping its existing track number).

    Tracks with no disc tag are treated as disc 1, as :func:`renumber_tracks`
    already does, so an untagged file cannot invent a disc 0 and shift the rest.

    Returns ``{path: {field: value}}`` shaped for ``tag_writer.write_fields``.
    """
    def _disc_of(s) -> float:
        """A song's disc number, defaulting an untagged file to disc 1."""
        return _fnum(s.get('disc')) or 1.0

    counts: dict = {}
    for s in ordered:
        d = _disc_of(s)
        counts[d] = counts.get(d, 0) + 1
    discs = sorted(counts)
    dense = {d: i + 1 for i, d in enumerate(discs)}

    out: dict = {}
    for s in ordered:
        d = _disc_of(s)
        fields: dict = {'disc': dense[d] if renumber else _fmt_disc(d)}
        if disc_totals:
            fields['total_discs'] = len(discs)
        if track_totals:
            # Keep the track's own number; only its total is being corrected.
            fields['track'] = _num(s.get('track')) or 1
            fields['total_tracks'] = counts[d]
        out[s['path']] = fields
    return out
