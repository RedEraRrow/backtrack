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

# Apostrophes that Unicode files as LETTERS: the Hawaiian ʻokina and the modifier
# / curly quotes the scripts use for elision.  `\w` and category 'L' both match
# them, so without this they survive normalization and "Molokaʻi" could never meet
# a transcript's "Molokai".  They are punctuation here, and go the same way as a
# plain "'".
_APOSTROPHE_LIKE = '\u02b9\u02bb\u02bc\u02bd\u02bf\u2018\u2019\u201b'
_DROP_APOSTROPHE = {ord(c): None for c in _APOSTROPHE_LIKE}


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
    t = t.translate(_DROP_APOSTROPHE)
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
    return ''.join(c for c in decomposed
                   if unicodedata.category(c)[0] in ('L', 'N')
                   and c not in _APOSTROPHE_LIKE)


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
# A stage direction is a parenthetical, optionally emphasised: *(sighs)* / (aside).
# It may contain a bracket of its own — *(A ringtone sounds ('Questa o quella' from
# Verdi's Rigoletto), then a beep.)* — so a bare [^)]* would stop at the inner one
# and leave the rest of the direction to be read as dialogue.
#
# One emphasised span holding BOTH a direction and dialogue, *She pauses (softly)*,
# is not recognised, deliberately: the pattern that used to read it treated the
# whole span as the direction and silently deleted the words, which is how "Patek
# Philippe" once vanished from Limerick.  A direction is its own thought and gets
# its own emphasis — *She pauses* *(softly)* — see docs/script-etiquette.md.
_PAREN_BODY = r'(?:[^()]|\([^()]*\))*'
_PAREN      = rf'\({_PAREN_BODY}\)'
_STAGE_RE = re.compile(rf'(?P<open>\*?)\((?P<dir>{_PAREN_BODY})\)(?P<close>\*?)')
_EMPHASIS_RE = re.compile(r'[*_`~]')
_LETTER_RE   = re.compile(r'[^\W\d_]')

# How many letters a BARE parenthetical needs before it counts as a direction
# rather than the script's own punctuation.  Ariane DeVere's transcripts use "(!)"
# for a sarcastic line and "(a)" / "(b)" to letter a list — both are read aloud or
# printed as written, and neither is a note to the reader.  Two letters clears
# them while keeping every real aside, the shortest of which is "(Ding)".  An
# author-marked *(dir)* is always a direction, however short.
_MIN_BARE_DIR_LETTERS = 2

# A standalone direction: a whole line that is nothing but a parenthetical, or an
# editorial note in brackets.  Built from the SAME _PAREN as the inline matcher so
# a nested bracket — *(A ringtone sounds ('Questa o quella' from Verdi's
# Rigoletto), then a beep.)* — is one direction to both, not a direction to one
# and stray dialogue to the other.
_STANDALONE_DIR_RE = re.compile(rf'\*+\((?P<paren>{_PAREN_BODY})\)\*+'
                                rf'|\((?P<bare>{_PAREN_BODY})\)'
                                rf'|\*\[(?P<note>[^\]]*)\]\*')


def _dir_text(m: re.Match) -> str:
    """The direction a `_STAGE_RE` match carries — '' when the match is not one.

    The pattern deliberately matches EVERY parenthetical so one scan can find both
    the directions and the text between them; this is where a match is judged.
    """
    body = (m.group('dir') or '').strip()
    if m.group('open') and m.group('close'):
        return body                                    # *(dir)* — the author said so
    if len(_LETTER_RE.findall(body)) >= _MIN_BARE_DIR_LETTERS:
        return body                                    # (In Spanish accent)
    return ''                                          # (!) / (a) — spoken punctuation


def standalone_stage_dir(line: str) -> str | None:
    """The direction a whole MD line consists of, or None if it is not one.

    `_parse_markdown_dialogue` asks this before it looks for a `Speaker:` header,
    so a direction that happens to contain a colon — *(Immediately: bing bong.)* —
    is never mistaken for dialogue.
    """
    m = _STANDALONE_DIR_RE.fullmatch((line or "").strip())
    if not m:
        return None
    d = (m.group('paren') or m.group('bare') or m.group('note') or "").strip()
    return d or None


def _scan(t: str) -> tuple[str, list[tuple[int, str]]]:
    """Split an MD dialogue line into (spoken text, inline directions).

    ONE pass, so the word offsets reported for the directions are literally word
    offsets into the string returned beside them.  `spoken_text` and
    `inline_stage_dirs` used to strip the line separately and count words in
    different strings, which drifted whenever a removal left punctuation behind.
    """
    if not t:
        return t or "", []
    t = _LINK_RE.sub(r'\1', t)   # [text](url) → text, before its (url) reads as a dir
    t = _NOTE_RE.sub(' ', t)      # *[editorial note]* → removed
    t = _INS_RE.sub('', t)        # <ins>6</ins>33 → 633
    out: list[str] = []
    at:  list[tuple[int, str]] = []       # (char offset into the spoken text, dir)
    last = 0
    for m in _STAGE_RE.finditer(t):
        d = _dir_text(m)
        if not d:                 # not a direction — it stays in the spoken text
            continue
        out.append(t[last:m.start()])
        # An emphasis marker on only ONE side of the direction is opening or
        # closing a span around the words either side of it, so it stays put: drop
        # it and the rest of the line renders as one long unterminated italic.
        keep = (m.group('open') or '') + (m.group('close') or '')
        out.append(keep if len(keep) == 1 else '')
        at.append((sum(len(p) for p in out), d))
        out.append(' ')
        last = m.end()
    out.append(t[last:])
    spoken = "".join(out)
    return spoken, [(len(spoken[:o].split()), d) for o, d in at]


def spoken_text(t: str) -> str:
    """Keep only actually-spoken words from an MD dialogue line: drop inline stage
    directions like *(sighs)* / (aside) and reduce [label](url) links to their
    label.  Used for BOTH matching and verification so a stage direction is never
    mistaken for dialogue."""
    return _scan(t)[0]


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
    [(n_spoken_words_before, dir_text), ...] counted in the string `spoken_text`
    returns, so the offsets index the md_words stream directly: a direction sitting
    before the k-th spoken word of the line reports n == k."""
    return _scan(t)[1]


# What a script writes at the end of a thought.  A cut is only safe where the
# script itself already broke: sentence and clause marks, the dashes and ellipses
# these transcripts use for an interruption or a trailing-off, and the ♪ that closes
# a sung phrase.  Trailing quotes, brackets and emphasis markers come after the mark
# and do not hide it.
_TERMINATOR_RE = re.compile(r'(?:[.!?;:,\u2026\u266a]|--|\.\.\.)[\"\'\u2019\u201d*_`~)\]]*$')


def ends_a_thought(word: str) -> bool:
    """Whether the script has punctuated its way out of this word — i.e. whether a
    segment may be cut straight after it without landing mid-sentence."""
    return bool(_TERMINATOR_RE.search((word or "").strip()))


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


# --- matching across the two conventions -----------------------------------

# A script and a transcript spell the same speech differently, and three of those
# differences are conventions rather than disagreements.  Whisper writes what it
# hears with its own habits: it closes hyphens the script opens ("take-off" /
# "takeoff"), writes numbers as digits where the script writes words ("twenty-five"
# / "25"), and expands the elisions the script preserves ("'cause" / "because").
# Left alone each one reads as a missing word AND an extra word, which is both a
# false discrepancy in the report and — worse — a hole in the timing, because the
# words either side of it never get pinned to a segment.
#
# These reconcile a run of one stream against a run of the other.  Every one is a
# decision about spelling, never about content: nothing here can make two different
# words match.

_NUM_UNIT = {'zero': 0, 'nought': 0, 'one': 1, 'two': 2, 'three': 3, 'four': 4,
             'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9}
_NUM_TEEN = {'ten': 10, 'eleven': 11, 'twelve': 12, 'thirteen': 13, 'fourteen': 14,
             'fifteen': 15, 'sixteen': 16, 'seventeen': 17, 'eighteen': 18,
             'nineteen': 19}
_NUM_TENS = {'twenty': 20, 'thirty': 30, 'forty': 40, 'fifty': 50, 'sixty': 60,
             'seventy': 70, 'eighty': 80, 'ninety': 90}
_NUM_WORD = {**_NUM_UNIT, **_NUM_TEEN, **_NUM_TENS}
_NUM_SCALE = {'thousand': 1000, 'million': 1000000, 'billion': 1000000000}
# 'oh' is a number only as a spoken digit ("flight level two-five-oh"), and in these
# scripts it is overwhelmingly the interjection — 734 of them.  Reading it as 0
# would match "Oh, dear" against any stray zero, so it is left out entirely.


def _number_value(toks: list[str]) -> int | None:
    """The number a run of tokens spells out, or None if it is not one.

    Digits and words both, so the comparison is against the value rather than the
    spelling: "25" and "twenty five" are the same number, and so are "7000" and
    "seven thousand".

    English grammar is enforced, not just arithmetic.  "five four three two one" is
    a countdown, not fifteen; "eleven thirty" is a time, not forty-one.  Adding the
    words up would match either against a digit that has nothing to do with it, so
    a sequence that no one would read as a single number returns None and the two
    streams pair off word by word instead.
    """
    if not toks:
        return None
    if len(toks) == 1 and toks[0].isdigit():
        return int(toks[0])
    total = cur = 0
    prev = None       # what the last token was: unit / teen / tens / hundred / scale
    for t in toks:
        if t == 'and':
            if prev not in ('hundred', 'scale'):
                return None                     # "and" only joins a hundred or a scale
            continue
        if t in _NUM_UNIT:
            if prev not in (None, 'tens', 'hundred', 'scale'):
                return None                     # "five four" is two numbers, not one
            cur += _NUM_UNIT[t]; prev = 'unit'
        elif t in _NUM_TEEN:
            if prev not in (None, 'hundred', 'scale'):
                return None                     # "eleven thirty" is a time
            cur += _NUM_TEEN[t]; prev = 'teen'
        elif t in _NUM_TENS:
            if prev not in (None, 'hundred', 'scale'):
                return None
            cur += _NUM_TENS[t]; prev = 'tens'
        elif t == 'hundred':
            if prev not in ('unit', 'teen', 'tens'):
                return None
            cur *= 100; prev = 'hundred'
        elif t in _NUM_SCALE:
            if prev is None:
                return None
            total += (cur or 1) * _NUM_SCALE[t]; cur = 0; prev = 'scale'
        else:
            return None
    return total + cur if prev else None


def _is_elision(a: str, b: str, a_raw: str, b_raw: str) -> bool:
    """Whether one token is the other with its head dropped — 'cause / because.

    The apostrophe in the RAW word is what says so.  Judging on the letters alone
    would pair "is" with "his" and "at" with "that", which are different words;
    the script's own punctuation is the only reliable signal that a head is missing.
    """
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    raw = a_raw if len(a) < len(b) else b_raw
    if short == long_ or len(short) < 2:
        return False
    if not (raw or "").lstrip('*_"').startswith(("'", '\u2019')):
        return False
    # A shared TAIL rather than a clean suffix, because the scripts spell the
    # shortened form as it sounds: "'scuse" against "excuse" keeps "cuse" and loses
    # the x.  The apostrophe above is what makes this safe — without it the same
    # test would pair "is" with "his".
    n = 0
    while n < len(short) and short[-1 - n] == long_[-1 - n]:
        n += 1
    return n >= 2 and n * 2 >= len(short)


# Longest run either side a reconciliation may cover.  Ten, because the scripts
# hyphenate chants and stammers as one written word that Whisper hears as many —
# "no-no-no-no-no-no-no-no-no", "fa-la-la-la-laa", spelling out "Q-I-K-I-Q-T".
# Nothing can match falsely at any length: a reconciliation is exact string or
# exact numeric equality, never a resemblance.
_RESCUE_SPAN = 10
# Past this the gap is a real divergence, not a spelling, and the search over it
# would cost more than the answer is worth.
_GAP_LIMIT = 2500


def _reconcile(js: list[str], md: list[str], js_raw: list[str], md_raw: list[str]) -> str | None:
    """How a run of transcript tokens and a run of script tokens say the same thing,
    or None if they do not.  One of 'equal', 'boundary', 'number', 'elision'."""
    if js == md:
        return 'equal'
    if "".join(js) == "".join(md):
        return 'boundary'                 # take-off / takeoff, no-one / noone
    jn, mn = _number_value(js), _number_value(md)
    if jn is not None and jn == mn:
        return 'number'                   # 25 / twenty five
    if len(js) == len(md) == 1 and _is_elision(js[0], md[0], js_raw[0], md_raw[0]):
        return 'elision'                  # because / 'cause
    return None


def align_tokens(js: list[str], md: list[str],
                 js_raw: list[str] | None = None,
                 md_raw: list[str] | None = None) -> list[tuple]:
    """Align a transcript token stream against a script one.

    Returns opcodes in difflib's shape — (tag, i1, i2, j1, j2) over js and md — but
    with two additions:

      • runs that difflib called a replacement because the two conventions spell
        the same thing differently are re-tagged 'boundary', 'number' or 'elision'
        instead, so the words either side of them stay pinned;
      • those runs may be n:m rather than 1:1, because one written word can be two
        spoken ones.  A consumer that needs a per-token pairing should use
        `pair_tokens`, which flattens this.

    difflib supplies the anchors; each gap between them is then re-walked with an
    exact local search, so a convention difference sitting next to a real one is
    still found instead of being swallowed by the surrounding replacement.
    """
    import difflib
    js_raw = js_raw if js_raw is not None else js
    md_raw = md_raw if md_raw is not None else md
    sm = difflib.SequenceMatcher(None, js, md, autojunk=False)
    out: list[tuple] = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            out.append(('equal', i1, i2, j1, j2))
        else:
            out.extend(_walk_gap(js, md, js_raw, md_raw, i1, i2, j1, j2))
    return _coalesce(_rewalk_between_gaps(_coalesce(out), js, md, js_raw, md_raw))


def _rewalk_between_gaps(ops, js, md, js_raw, md_raw) -> list[tuple]:
    """Re-walk the whole stretch between two gaps, rather than each gap alone.

    difflib anchors on any run of identical tokens, and these scripts repeat
    themselves — "five point nine, well, five point eight, no-o-o, five point nine,
    say five point eight five".  It will happily latch the LAST "five point" it
    heard onto the FIRST one in the script, stranding everything between as a
    deletion and an insertion that re-walking either half cannot fix, because the
    words each one needs are on the far side of the other's anchor.

    Swallowing the anchors and searching the span at once cannot do worse: the
    search scores an identical pair at zero cost too, so difflib's answer is still
    available to it and it departs only for something strictly cheaper.  That makes
    the cell budget the only thing worth gating on, and there is no attempt to guess
    in advance which anchors are real — a wrong guess costs a search, never an
    answer.  Bounded passes, because a re-walk can expose another stranded run
    behind the one it just resolved.
    """
    GAP = ('replace', 'delete', 'insert')
    for _pass in range(3):
        changed = False
        out: list[tuple] = []
        i = 0
        while i < len(ops):
            if ops[i][0] not in GAP:
                out.append(ops[i]); i += 1; continue
            last = i                          # furthest gap this region can reach
            for k in range(i + 1, len(ops)):
                if ops[k][0] not in GAP:
                    continue
                if (ops[k][2] - ops[i][1]) * (ops[k][4] - ops[i][3]) > _GAP_LIMIT:
                    break                     # too wide to search — leave it split
                last = k
            if last > i:
                out.extend(_walk_gap(js, md, js_raw, md_raw,
                                     ops[i][1], ops[last][2], ops[i][3], ops[last][4]))
                changed = True
                i = last + 1
                continue
            out.append(ops[i]); i += 1
        ops = _coalesce(out)
        if not changed:
            break
    return ops


def _walk_gap(js, md, js_raw, md_raw, i1, i2, j1, j2) -> list[tuple]:
    """Re-align one run difflib could not match, allowing n:m reconciliations.

    A shortest-edit search over the gap: every way of pairing up to `_RESCUE_SPAN`
    tokens each side is tried, a reconciliation costs nothing and anything else
    costs what it drops.  Gaps are a handful of tokens, so the exact answer is
    cheaper than the heuristics that would approximate it.
    """
    n, m = i2 - i1, j2 - j1
    if not n or not m:                            # pure insert / delete
        return [('delete' if m == 0 else 'insert', i1, i2, j1, j2)]
    if n * m > _GAP_LIMIT:
        return [('replace', i1, i2, j1, j2)]

    INF = float('inf')
    # best[a][b] = (cost, back-pointer) for js[i1:i1+a] against md[j1:j1+b]
    best = [[(INF, None)] * (m + 1) for _ in range(n + 1)]
    best[0][0] = (0, None)
    for a in range(n + 1):
        for b in range(m + 1):
            cost, _bp = best[a][b]
            if cost == INF:
                continue
            if a < n:                             # drop a transcript token
                if cost + 1 < best[a + 1][b][0]:
                    best[a + 1][b] = (cost + 1, (a, b, 'delete'))
            if b < m:                             # drop a script token
                if cost + 1 < best[a][b + 1][0]:
                    best[a][b + 1] = (cost + 1, (a, b, 'insert'))
            for da in range(1, min(_RESCUE_SPAN, n - a) + 1):
                for db in range(1, min(_RESCUE_SPAN, m - b) + 1):
                    kind = _reconcile(js[i1 + a:i1 + a + da], md[j1 + b:j1 + b + db],
                                      js_raw[i1 + a:i1 + a + da], md_raw[j1 + b:j1 + b + db])
                    if kind and cost < best[a + da][b + db][0]:
                        best[a + da][b + db] = (cost, (a, b, kind))

    path: list[tuple] = []
    a, b = n, m
    while (a, b) != (0, 0):
        cost, bp = best[a][b]
        if bp is None:                            # unreachable — fall back whole
            return [('replace', i1, i2, j1, j2)]
        pa, pb, kind = bp
        path.append((kind, i1 + pa, i1 + a, j1 + pb, j1 + b))
        a, b = pa, pb
    return path[::-1]


def _coalesce(ops: list[tuple]) -> list[tuple]:
    """Merge neighbouring 'equal' runs, and a delete beside an insert into the
    'replace' the report expects, so the output reads like difflib's.

    Reconciliations are deliberately NOT merged into each other.  Two adjacent ones
    are two separate answers — "eleven thirty" against "11 30" is a pair of numbers,
    not one four-token blur — and merging them would throw away the word-for-word
    pairing that the timing is hung on.
    """
    out: list[tuple] = []
    for op in ops:
        if not out:
            out.append(op); continue
        tag, i1, i2, j1, j2 = op
        ptag, pi1, pi2, pj1, pj2 = out[-1]
        if pi2 == i1 and pj2 == j1:
            if ptag == tag == 'equal':
                out[-1] = (tag, pi1, i2, pj1, j2); continue
            if {ptag, tag} <= {'delete', 'insert', 'replace'}:
                out[-1] = ('replace', pi1, i2, pj1, j2); continue
        out.append(op)
    return out


MATCHED = ('equal', 'boundary', 'number', 'elision')


def pair_tokens(ops: list[tuple], placement: bool = False) -> list[tuple[int, int, str]]:
    """Flatten `align_tokens` output to (js_index, md_index, tag) pairings.

    Runs of equal length pair off position by position.  An n:m reconciliation has
    no per-token truth to report, so every token in it pairs with the FIRST of its
    opposite run: the whole run is one place in the audio, and pinning it there is
    what keeps the timing continuous.

    `placement` also pairs the words the two streams DISAGREE about.  They are not
    matches and never count as any, but a 'replace' still says where in the speech
    the script's word sits — "'e" against a heard "he" is the same moment, whatever
    the spelling — and a consumer placing words on a timeline wants that.  Without
    it an unmatched word falls back to riding with its neighbours on the line, which
    puts it in the wrong segment whenever it happens to open one.  A consumer
    choosing where to CUT should leave this off: knowing roughly where a word is is
    not the same as trusting it enough to split on.
    """
    tags = MATCHED + ('replace',) if placement else MATCHED
    out: list[tuple[int, int, str]] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag not in tags:
            continue
        if i2 - i1 == j2 - j1:
            out += [(i1 + o, j1 + o, tag) for o in range(i2 - i1)]
        else:
            out += [(i, j1, tag) for i in range(i1, i2)]
            out += [(i1, j, tag) for j in range(j1 + 1, j2)]
    return out
