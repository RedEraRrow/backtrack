"""One place for lyric text handling: normalising, stripping, and locating.

Matching a markdown script against a Whisper transcript, timing a sentence, and
deciding which line is on screen all rest on the same three questions — what
counts as the same word, what counts as spoken, and which line covers a moment.
Each used to be answered separately in `lyrics.py`, `lyrics_editor.py` and
`md_overlay.py`, with normalisers that disagreed about Unicode and punctuation,
so a word could match during alignment and fail during verification.
"""
from __future__ import annotations

import bisect
import re
import unicodedata


# --- what counts as the same word -----------------------------------------

def norm(t: str) -> str:
    """Canonical match normalization: Unicode-fold (NFKD), lowercase, joiners
    (hyphens / periods / slashes)→spaces, other punctuation dropped.  This is the
    single basis for ALL JSON↔MD comparison (alignment and the verify report) so
    the two never disagree about what 'matches'.  Treating '.' and '/' as word
    boundaries makes a dotted abbreviation match its spoken-out letters
    (C.P.L. → "c p l" == "C P L", G.P → "g p" == "G P").  Folding keeps non-ASCII
    letters (accented Latin → base letter, Cyrillic/CJK preserved) instead of
    deleting them, which previously made non-English lyrics vanish from the stream.
    """
    t = unicodedata.normalize('NFKD', (t or "")).lower()
    t = re.sub(r'[-./]', ' ', t)
    return re.sub(r'[^\w ]', '', t).strip()


def norm_words(t: str) -> list[str]:
    """`norm` split into comparison tokens (a hyphenated word yields two)."""
    return norm(t).split()


def matchable(word: str) -> str:
    """One word reduced to bare letters and digits, for word-by-word matching.

    The same NFKD fold as `norm` — so the two can't disagree about accents or
    scripts — but joiners close up instead of splitting, because this compares a
    single token against a single transcript token: "C.P.L." meets "cpl", and
    "don't" meets "dont", rather than becoming several tokens to re-align.
    """
    decomposed = unicodedata.normalize('NFKD', (word or "").lower())
    return ''.join(c for c in decomposed if unicodedata.category(c)[0] in ('L', 'N'))


def norm_line(s: str) -> str:
    """A whole line flattened for line-to-line comparison: whitespace collapsed
    and lowercased, punctuation left alone (two renderings of the same line
    differ in spacing and case, not in words)."""
    return re.sub(r'\s+', ' ', (s or "").strip().lower())


# --- what counts as spoken -------------------------------------------------

# Shared patterns so `spoken_text` (what counts as dialogue) and
# `inline_stage_dirs` (where a mid-line direction sits) can never disagree.
_LINK_RE  = re.compile(r'\[([^\]]*)\]\([^)]*\)')      # [text](url)
# An editorial note about how a word is said — *[He pronounces 'Flos' as
# 'Floss.']* — is about the dialogue, not part of it, so it never reaches the
# spoken stream. <ins> marks emphasis inside a word (*<ins>6</ins>33 Squadron*)
# and has to come off before tokenizing, or "633" arrives as "ins6 ins33".
_NOTE_RE  = re.compile(r'\*\[[^\]]*\]\*')
_INS_RE   = re.compile(r'</?ins>')
# A stage direction in either shape the scripts use: a parenthetical, optionally
# emphasised — *(sighs)* / (aside) — or a whole emphasised span that contains a
# parenthetical, *She pauses (softly)*, where the emphasis opens before the
# bracket. The second shape was previously understood by the timing cleaner but
# not by the matcher, so its words counted as dialogue when aligning and not when
# timing; one pattern is what keeps those two answers the same.
# The parenthetical may itself contain one — *(A ringtone sounds ('Questa o
# quella' from Verdi's Rigoletto), then a beep.)* — so a bare [^)]* stops at the
# inner bracket and leaves the rest of the direction to be read as dialogue.
_PAREN = r'\((?:[^()]|\([^()]*\))*\)'
_STAGE_RE = re.compile(rf'\*(?P<emph>[^*()]+{_PAREN})\*'
                       rf'|\*?\((?P<dir>(?:[^()]|\([^()]*\))*)\)\*?')
_EMPHASIS_RE = re.compile(r'[*_`~]')


def spoken_text(t: str) -> str:
    """Keep only actually-spoken words from an MD dialogue line: drop inline stage
    directions like *(sighs)* / (aside) and reduce [label](url) links to their
    label.  Used for BOTH matching and verification so a stage direction is never
    mistaken for dialogue."""
    if not t:
        return t
    t = _LINK_RE.sub(r'\1', t)    # [text](url) → text
    t = _NOTE_RE.sub(' ', t)      # *[editorial note]* → removed
    t = _INS_RE.sub('', t)        # <ins>6</ins>33 → 633
    t = _STAGE_RE.sub(' ', t)     # *(stage dir)* / (aside) → removed
    return t


def clean_for_timing(text: str) -> str:
    """`spoken_text` reduced further to a bare word run, for word-count timing.

    Emphasis markers go too — an italicised word is spoken like any other, but a
    stray `*` left behind by a direction that was wrapped in emphasis would be
    counted as a word — and runs of whitespace collapse to one.
    """
    return re.sub(r'\s+', ' ', _EMPHASIS_RE.sub('', spoken_text(text or ""))).strip()


def inline_stage_dirs(t: str) -> list[tuple[int, str]]:
    """Mid-line stage directions embedded in a dialogue line, e.g.
    'I never thought *(she pauses)* it would end'.  Returns
    [(n_spoken_words_before, dir_text), ...] using the SAME stripping as
    `spoken_text`, so the counts line up with the md_words stream: a direction
    sitting before the k-th spoken word of the line reports n == k."""
    if not t:
        return []
    t = _LINK_RE.sub(r'\1', t)    # links first, so their (url) isn't taken for a dir
    t = _NOTE_RE.sub(' ', t)      # same removals as spoken_text, so the counts agree
    t = _INS_RE.sub('', t)
    out: list[tuple[int, str]] = []
    last = count = 0
    for m in _STAGE_RE.finditer(t):
        count += len(t[last:m.start()].split())
        d = (m.group('dir') if m.group('dir') is not None else m.group('emph') or "").strip()
        if d:
            out.append((count, d))
        last = m.end()
    return out


def strip_markdown(text: str) -> str:
    """Strip markdown emphasis/code markers (*_`~) for plain-text display."""
    return _EMPHASIS_RE.sub('', text)


# --- which line is on screen ----------------------------------------------

def find_current_line(line_times: list[tuple[float, float]], elapsed: float) -> int:
    """Binary-search `line_times` for the line covering `elapsed`, clamped to range.

    Serves SYLT lines, USLT lines and dialogue chunks alike — they are all a
    sorted run of (start, end) windows, and the answer must not depend on which
    view is asking.
    """
    ends = [t[1] for t in line_times]
    idx = bisect.bisect_right(ends, elapsed)
    return min(idx, max(0, len(line_times) - 1))
