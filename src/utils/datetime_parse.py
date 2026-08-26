"""One date/time parser for the whole project.

Backtrack reads dates typed by hand in several places — the calendar widget's
manual entry, the combined date/time editor, the per-range schedule table, tag
values arriving from a filename — and each had grown its own rules.  They
disagreed: one accepted ``2008-7-2`` and another rejected it, one kept a time and
another silently truncated it, and the two resolved ``02/07/2008`` differently.

Everything now goes through :func:`parse_datetime`.  It accepts what a person
plausibly types, keeps whatever precision was given, and says *why* when it can't
read something so the caller can show that rather than a bare failure.

Accepted, all with ``-``, ``/`` or ``.`` between the parts and zero-padding
optional::

    2008              2008-07           2008-07-02        2008-7-2
    2008/07/02        2008.7.2          20080702
    2008-07-02 18:30  2008-07-02T18:30  2008-07-02 18:30:45

A time may follow the date after a ``T`` (either case) or a space, as ``HH:MM``
or ``HH:MM:SS``.  A trailing timezone (``Z`` or ``±HH:MM``) is stripped — the
tags Backtrack writes are local wall-clock timestamps.

Day-first vs month-first (``02/07/2008``) is genuinely ambiguous and is resolved
only when the caller says how, via ``dayfirst``.  Left unset, an ambiguous date
is refused rather than guessed, because guessing wrong writes a plausible-looking
wrong date that nobody notices.
"""
from __future__ import annotations

import datetime
import re
from typing import NamedTuple, Optional

__all__ = ['ParsedDateTime', 'parse_datetime', 'parse_date', 'parse_time',
           'format_datetime', 'PRECISIONS']

# Coarse → fine.  A caller can compare precisions with `PRECISIONS.index(...)`
# to demand at least a given granularity.
PRECISIONS = ('year', 'month', 'day', 'minute', 'second')

# Year-first, any of - / . between parts, zero-padding optional.
_YEAR_FIRST_RE = re.compile(r'^(\d{4})(?:[-/.\s](\d{1,2})(?:[-/.\s](\d{1,2}))?)?$')
# ISO basic form, 20080702.
_COMPACT_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})$')
# Day- or month-first, e.g. 02/07/2008 — order decided by `dayfirst`.
_YEAR_LAST_RE = re.compile(r'^(\d{1,2})[-/.\s](\d{1,2})[-/.\s](\d{4})$')
# A trailing timezone we drop rather than try to honour.
_TZ_RE = re.compile(r'(Z|[+-]\d{2}:?\d{2})$', re.IGNORECASE)


class ParsedDateTime(NamedTuple):
    """The result of reading a date/time a user typed.

    ``date`` is always a real ``datetime.date`` on success — a year- or
    month-only input is completed to the 1st so callers that just need *a* date
    have one — and ``precision`` records how much was actually given, so a caller
    that needs a real day (a schedule counting in days, say) can insist on it
    instead of silently scheduling from an invented 1 January.

    ``time`` is ``'HH:MM:SS'`` or None.  ``error`` is '' on success and otherwise
    a short phrase naming what was wrong, fit to show the user directly.
    """
    date: Optional[datetime.date]
    time: Optional[str]
    precision: str
    error: str

    @property
    def ok(self) -> bool:
        """True if the input parsed."""
        return not self.error

    def iso(self) -> str:
        """Render back at the precision that was given ('2008', '2008-07-02 18:30:00')."""
        return format_datetime(self)


def parse_time(raw) -> Optional[str]:
    """Normalise ``HH``/``HH:MM``/``HH:MM:SS`` to ``'HH:MM:SS'``; None if unreadable.

    Rejects out-of-range parts (``25:00``, ``18:75``) rather than rolling them
    over, so a typo surfaces instead of quietly becoming a different time.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    parts = s.split(':')
    if len(parts) > 3 or not all(p.strip().isdigit() for p in parts if p.strip() != ''):
        return None
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 and parts[1].strip() else 0
        sec = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 0
    except (ValueError, IndexError):
        return None
    if 0 <= h < 24 and 0 <= m < 60 and 0 <= sec < 60:
        return f"{h:02d}:{m:02d}:{sec:02d}"
    return None


def _split_date_time(s: str) -> tuple:
    """Split a stamp into its date and time halves.

    A ``T`` always separates them.  A space only does when what follows it looks
    like a clock time — it carries a ``:`` — because a space is *also* a legal
    separator inside the date itself: ``2008 07 02`` is a date, while
    ``2008-07-02 18:30`` is a date and a time.
    """
    upper = s.upper()
    if 'T' in upper:
        cut = upper.index('T')
        return s[:cut].strip(), s[cut + 1:].strip()
    if ' ' in s:
        head, _, tail = s.rpartition(' ')
        if ':' in tail:
            return head.strip(), tail.strip()
    return s.strip(), ''


def _read_date(part: str, dayfirst: Optional[bool]) -> tuple:
    """Parse the date half → ``(date, precision, error)``."""
    m = _COMPACT_RE.match(part)
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _build(y, mo, d, 'day')

    m = _YEAR_FIRST_RE.match(part)
    if m:
        year, month, day = m.groups()
        if month is None:
            return _build(int(year), 1, 1, 'year')
        if day is None:
            return _build(int(year), int(month), 1, 'month')
        return _build(int(year), int(month), int(day), 'day')

    m = _YEAR_LAST_RE.match(part)
    if m:
        a, b, year = (int(g) for g in m.groups())
        # Only one ordering can be right when a part exceeds 12 (13/07/2008 has
        # to be day-first), so try both and see how many survive.
        day_first_ok = 1 <= b <= 12
        month_first_ok = 1 <= a <= 12
        if day_first_ok and month_first_ok:
            # Genuinely ambiguous: 02/07/2008 is 2 July or 2 February depending
            # on where you live. Honour an explicit choice, else refuse rather
            # than pick one and be silently wrong.
            if dayfirst is None:
                return None, '', (f"{part!r} could be day-first or month-first — "
                                  "write it year-first (2008-07-02)")
            day, month = (a, b) if dayfirst else (b, a)
        elif day_first_ok:
            day, month = a, b
        elif month_first_ok:
            month, day = a, b
        else:
            return None, '', f"{part!r} has no valid month"
        return _build(year, month, day, 'day')

    return None, '', f"{part!r} is not a date"


def _build(year: int, month: int, day: int, precision: str) -> tuple:
    """Validate y/m/d into a real date, or report why it isn't one."""
    if not 1 <= month <= 12:
        return None, '', f"there is no month {month}"
    try:
        return datetime.date(year, month, day), precision, ''
    except ValueError:
        return None, '', (f"{year:04d}-{month:02d}-{day:02d} is not a real date")


def parse_datetime(raw, *, dayfirst: Optional[bool] = None) -> ParsedDateTime:
    """Read a date, optionally with a time, from something a user typed.

    ``dayfirst`` resolves ``02/07/2008``: True reads it day-first, False
    month-first, and the default (None) refuses it and says to write the date
    year-first.  Year-first input is never ambiguous and never consults it.
    """
    if raw is None:
        return ParsedDateTime(None, None, '', 'no date given')
    s = _TZ_RE.sub('', str(raw).strip()).strip()
    if not s:
        return ParsedDateTime(None, None, '', 'no date given')

    date_part, time_part = _split_date_time(s)
    date, precision, err = _read_date(date_part, dayfirst)
    if err:
        return ParsedDateTime(None, None, '', err)

    if not time_part:
        return ParsedDateTime(date, None, precision, '')

    tod = parse_time(time_part)
    if tod is None:
        return ParsedDateTime(None, None, '', f"{time_part!r} is not a valid 24-hour time")
    # A time implies a full date; without one we would be timing an invented day.
    if precision != 'day':
        return ParsedDateTime(None, None, '', f"{date_part!r} needs a full year-month-day "
                                              "to carry a time")
    return ParsedDateTime(date, tod, 'second' if tod[-2:] != '00' else 'minute', '')


def parse_date(raw, *, dayfirst: Optional[bool] = None) -> Optional[datetime.date]:
    """Just the date, or None if it won't parse. Any time given is ignored."""
    return parse_datetime(raw, dayfirst=dayfirst).date


def format_datetime(parsed: ParsedDateTime) -> str:
    """Render a parse back out at the precision it was given."""
    if parsed.date is None:
        return ''
    if parsed.precision == 'year':
        return f"{parsed.date.year:04d}"
    if parsed.precision == 'month':
        return f"{parsed.date.year:04d}-{parsed.date.month:02d}"
    if parsed.time:
        return f"{parsed.date.isoformat()} {parsed.time}"
    return parsed.date.isoformat()


def parse_date_parts(raw, *, dayfirst: Optional[bool] = None) -> Optional[tuple]:
    """``(year, month, day)`` for a typed date, or None — the shape the calendar
    and date/time widgets work in.  A year- or month-only input completes to the
    1st, as those widgets have always done."""
    d = parse_datetime(raw, dayfirst=dayfirst).date
    return (d.year, d.month, d.day) if d else None
