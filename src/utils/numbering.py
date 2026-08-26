"""Number rendering shared by the patterning tools: arabic, roman, or written out.

One place owns the three styles so a file-name pattern (``%track:r%``), a bulk
range template (``Act {r}``) and the playback panel's movement numeral all agree.
"""
from __future__ import annotations

# Value → symbol, largest first: the standard subtractive-notation table.
_ROMAN: tuple[tuple[int, str], ...] = (
    (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
    (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
    (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
)

_ONES = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
         'nine', 'ten', 'eleven', 'twelve', 'thirteen', 'fourteen', 'fifteen',
         'sixteen', 'seventeen', 'eighteen', 'nineteen')
_TENS = ('', '', 'twenty', 'thirty', 'forty', 'fifty', 'sixty', 'seventy',
         'eighty', 'ninety')

# Recognised number styles, and the case modifiers each accepts.
STYLES: dict[str, str] = {
    'n': 'Arabic (3)',
    'r': 'Roman (III)',
    'en': 'Written out (Three)',
}
CASES: str = "l = lower, u = UPPER, t = Title Case"


def roman(num: int) -> str:
    """Integer → Roman numeral ('' for anything below 1, which has no numeral)."""
    try:
        n = int(num)
    except (TypeError, ValueError):
        return ''
    if n < 1:
        return ''
    out: list[str] = []
    for value, symbol in _ROMAN:
        count, n = divmod(n, value)
        out.append(symbol * count)
    return ''.join(out)


def in_words(num: int) -> str:
    """Integer → English words, lower case ('twenty-one', 'one hundred and five').

    Covers 0–999,999; anything outside that falls back to the digits, since a
    pattern is better off showing a number than nothing.
    """
    try:
        n = int(num)
    except (TypeError, ValueError):
        return str(num)
    if n < 0:
        return f"minus {in_words(-n)}"
    if n >= 1_000_000:
        return str(n)

    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + (f"-{_ONES[ones]}" if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        out = f"{_ONES[hundreds]} hundred"
        return f"{out} and {in_words(rest)}" if rest else out
    thousands, rest = divmod(n, 1000)
    out = f"{in_words(thousands)} thousand"
    if not rest:
        return out
    # "two thousand and five", but "two thousand one hundred and five".
    joiner = " and " if rest < 100 else " "
    return out + joiner + in_words(rest)


# --- reading numbers back ------------------------------------------------

_ROMAN_VALUES = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}

_WORD_VALUES: dict[str, int] = {w: i for i, w in enumerate(_ONES)}
_WORD_VALUES.update({w: i * 10 for i, w in enumerate(_TENS) if w})
_WORD_SCALES = {'hundred': 100, 'thousand': 1000}

# Regex fragments for matching a number written in each style, for pattern
# templates that parse names ("Act III - Title", "Series Three, Episode Four").
# Case-insensitive in-place so they can drop into a larger pattern unchanged.
ROMAN_FRAGMENT = r'(?i:[mdclxvi]+)'
WORD_FRAGMENT = (r'(?i:(?:' + '|'.join(
    sorted(list(_ONES) + [t for t in _TENS if t] + list(_WORD_SCALES) + ['and'],
           key=len, reverse=True)) + r')(?:[- ](?:' + '|'.join(
    sorted(list(_ONES) + [t for t in _TENS if t] + list(_WORD_SCALES) + ['and'],
           key=len, reverse=True)) + r'))*)')


def from_roman(text: str) -> int | None:
    """Roman numeral → int, or None if `text` isn't one ('XIV' → 14)."""
    t = str(text).strip().upper()
    if not t or any(c not in _ROMAN_VALUES for c in t):
        return None
    total = 0
    for i, c in enumerate(t):
        v = _ROMAN_VALUES[c]
        # A smaller symbol before a larger one is subtractive (IV, IX, XL…).
        total += -v if (i + 1 < len(t) and v < _ROMAN_VALUES[t[i + 1]]) else v
    if total < 1 or roman(total) != t:
        return None            # not canonical ('IIII', 'VX') — treat as text
    return total


def from_words(text: str) -> int | None:
    """English words → int, or None if unparseable ('twenty-one' → 21)."""
    tokens = [w for w in str(text).lower().replace('-', ' ').split() if w != 'and']
    if not tokens:
        return None
    total = current = 0
    for w in tokens:
        if w in _WORD_SCALES:
            scale = _WORD_SCALES[w]
            # "two hundred" scales what's pending; "thousand" banks it.
            current = max(current, 1) * scale
            if scale >= 1000:
                total += current
                current = 0
        elif w in _WORD_VALUES:
            current += _WORD_VALUES[w]
        else:
            return None
    return (total + current) or (0 if 'zero' in tokens else None)


def parse(text) -> int | None:
    """Read a number written in any of the three styles: '4', 'IV' or 'four'."""
    t = str(text).strip()
    if not t:
        return None
    if t.lstrip('-').isdigit():
        return int(t)
    return from_roman(t) if from_roman(t) is not None else from_words(t)


def apply_case(text: str, case: str) -> str:
    """Apply a case modifier: 'l' lower, 'u' upper, 't' Title Case.

    Anything else (including an empty modifier) capitalises the first letter
    only — 'Twenty-one' rather than 'Twenty-One', which reads better mid-title.
    """
    if not text:
        return text
    if case == 'l':
        return text.lower()
    if case == 'u':
        return text.upper()
    if case == 't':
        return '-'.join(w.capitalize() for w in text.split('-')) if '-' in text \
            else ' '.join(w.capitalize() for w in text.split(' '))
    return text[0].upper() + text[1:]


def render(value, style: str = 'n', spec: str = '') -> str:
    """Render `value` in one of the three number styles.

    ``style`` is 'n' (arabic), 'r' (roman) or 'en' (written out); ``spec`` is a
    case modifier for 'r'/'en', or a `format()` spec for 'n' (so ``{n:02d}``
    padding still works). A value that isn't a number comes back unchanged, and
    a number with no Roman form (0 or less) falls back to its digits rather than
    vanishing from the pattern.
    """
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return str(value)

    if style == 'r':
        return apply_case(roman(n), spec) or str(n)
    if style == 'en':
        return apply_case(in_words(n), spec or 'c')
    try:
        return format(n, spec) if spec else str(n)
    except (ValueError, TypeError):
        return str(n)
