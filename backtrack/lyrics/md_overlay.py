"""Shared MD↔JSON alignment: the one place that decides how a markdown
script (speakers, stage directions, emphasis, punctuation) is overlaid onto a
timed transcript's segments.

Both the lyrics editor (`backtrack.lyrics.editor`) and the playback lyric
display (`backtrack.lyrics.formats`) build their view from `build_md_overlay`, so the
two can never disagree about which speaker owns a line, where a stage direction
sits, or what a segment's text actually reads.

This module deliberately has no module-level dependency on `backtrack.lyrics.formats`
(the markdown parser is imported lazily inside `build_md_overlay`) so the
playback side can import it at module scope without a cycle.
"""
from __future__ import annotations
from backtrack.lyrics import text as _lt
from backtrack import tuning as tune


# Text handling lives in lyrics_text so the editor, the player and this module
# can't drift apart on what a word or a stage direction is. Re-exported under the
# names this module has always published.
_norm = _lt.norm
_norm_words = _lt.norm_words
_spoken_text = _lt.spoken_text
_inline_stage_dirs = _lt.inline_stage_dirs


_SD_SCOPES = ('inline', 'tone', 'external')


def line_speaker(item) -> str:
    """The banner text for one parsed MD dialogue line ('' when unlabelled).

    The overlay and the editor's verify stream both name lines, and named them
    two different ways; one helper is what keeps a split, a pin and a report row
    all talking about the same speaker."""
    return " & ".join(s.strip() for s in item.speakers) if item.speakers else ""


def _sd_scope(seg: dict) -> str:
    """The direction's kind (see _SD_SCOPES). Migrates older segs that used the
    'speaker' scope or a separate `tone` flag."""
    scope = seg.get('scope')
    if scope in _SD_SCOPES:
        return scope
    return 'tone' if seg.get('tone') else 'inline'


def _is_framed(seg: dict) -> bool:
    """A beat that stands alone as its own framed section (dotted rules, no
    speaker): dead air (silence, belongs to nobody) or an external stage
    direction (scene/sound, or another person's aside)."""
    k = seg.get('kind')
    return k == 'dead_air' or (k == 'stage_dir' and _sd_scope(seg) == 'external')


def mid_line_direction(overlay: list | None, si: int) -> dict | None:
    """The mid-line direction shown just above segment `si`, the one the lyric
    editor's kind key flips between tone and action; None if there isn't one."""
    return next((ov for ov in overlay or [] if ov['kind'] == 'stage_dir'
                 and ov['before_si'] == si and ov.get('tone') is not None), None)


def _is_note(raw: str) -> bool:
    """True for a music-note token: '♪', or '♪.' where the script punctuated it.
    It carries no comparison token of its own, so it is only ever placed by the
    rule that keeps an opening ♪ with the phrase it introduces."""
    return '\u266a' in raw and not _norm_words(raw)


def _reading_time(text: str) -> float:
    """A sensible on-screen read time for a note, floored so even a one-word one
    lingers long enough to register. Unbounded above: a long transcriber's note
    takes as long as it takes, and the caller decides whether it has the room."""
    words = len((text or '').split())
    return round(max(tune.LYRIC_READ_MIN_S, words / tune.LYRIC_READ_WPS), 3)


def build_md_overlay(segs: list[dict], md_path: str, kinds: dict | None = None) -> tuple[list, dict, dict]:
    """Overlay the MD script onto the JSON segments via ONE word-level alignment.

    The JSON and MD are the same spoken words (modulo punctuation / hyphens / ♪).
    So we align the two word streams once (`align_tokens`) and then, line by line, hand
    each MD line's words to whichever segment its words lined up with:
      • one segment covers the line  → it shows the whole line (with punctuation),
      • several segments cover it     → each shows its own run of the line's words.
    A segment whose words span two MD lines shows both (and is a split candidate).

    Returns (overlay, quality, links):
      overlay: [{kind:'speaker'|'stage_dir', text, before_si, stage?, line?,
                 tone?, within?}],
                display only.  A speaker banner's `line` is the MD line it labels
                (None for a banner raised over a stage direction), so a consumer
                can scope the speaker to exactly the segs `links` pins to it
                instead of carrying it forward indefinitely.
                A mid-line direction also carries its `line`, whether it is
                `tone` (how the words after it are spoken, by
                `text.direction_kind` and the editor's `kinds` overrides) and,
                when it falls inside one segment, `within`: [(si, k, gap_from,
                at)], the cut before that segment's k-th MD word, the end of the
                word before and the start of the word after.
      quality: si → {score, md_text} for every dialogue seg the MD explains.
      links:   si → line_id, the seg's MD line, recorded by the caller as line_ref;
               a committed mid-line direction's too.
    """
    import bisect
    from collections import Counter, OrderedDict, defaultdict
    from backtrack.lyrics.formats import _parse_markdown_dialogue

    with open(md_path, encoding='utf-8') as fh:
        dl = _parse_markdown_dialogue(fh.read())

    # ── MD structure: dialogue lines (raw words) + standalone stage directions ──
    md_words: list = []                # (raw_word, line_id) for every dialogue word
    line_meta: dict = {}               # line_id → {'speaker', 'stage'}
    stage_dirs: list = []              # standalone directions: {'text', 'before_line'}
    inline_dirs: list = []             # mid-line directions: {'text', 'line', 'pos'}
    lid = 0
    for item in dl:
        if item.is_empty():
            continue
        if item.is_stage_direction():
            stage_dirs.append({'text': item.stage_dir, 'before_line': lid})
            continue
        spk = line_speaker(item)
        if item.stage_dir and not item.speakers:      # a bare (aside) with no speaker
            stage_dirs.append({'text': item.stage_dir, 'before_line': lid})
            banner_stage = ""
        else:
            banner_stage = item.stage_dir              # inline dir → shown on the banner
        n_words = 0
        for rw in _spoken_text(item.text).split():
            md_words.append((rw, lid))
            n_words += 1
        for pos, dtext in _inline_stage_dirs(item.text):
            if pos == 0 and n_words and _lt.direction_kind(dtext, kinds) == 'tone':
                # A manner note opening the words is the line's own aside.
                banner_stage = "; ".join(x for x in (banner_stage, dtext) if x)
                continue
            inline_dirs.append({'text': dtext, 'line': lid, 'pos': pos})
        line_meta[lid] = {'speaker': spk, 'stage': banner_stage}
        lid += 1

    def _score(a: str, b: str) -> float:
        """Word-overlap score between two normalised strings, counting repeats.

        A multiset, not a set: '♪ Three men went to mow, went to mow a meadow ♪'
        is half repeated words, and collapsing them scored a segment that had
        dropped half the line as a perfect match."""
        wa, wb = Counter(a.split()), Counter(b.split())
        n = sum((wa & wb).values())
        return n / max(sum(wa.values()), sum(wb.values())) if wa and wb else 0.0

    norm_segs = [_norm(s.get('text', '')) for s in segs]

    # ── ONE alignment: JSON content tokens ↔ MD content tokens ─────────────────
    dia = [si for si in range(len(segs))
           if segs[si].get('kind') not in ('dead_air', 'stage_dir')]
    js_words: list = []          # (raw_word, seg_index, word_index)
    for si in dia:
        ws = segs[si].get('words')
        if ws:
            for wi, w in enumerate(ws):
                js_words.append((w.get('word', ''), si, wi))
        else:                    # word-less seg (SYLT/USLT): fall back to its text
            for wi, rw in enumerate(segs[si].get('text', '').split()):
                js_words.append((rw, si, wi))
    js_ctoks = [(t, ji) for ji, (rw, _, _) in enumerate(js_words) for t in _norm_words(rw)]
    md_ctoks = [(t, mi) for mi, (rw, _) in enumerate(md_words) for t in _norm_words(rw)]
    # `align_tokens` rather than difflib direct: the transcript and the script spell
    # numbers, hyphens and elisions differently, and a run left unmatched over a
    # spelling is a run of words with no segment: the gap in the timing the reader
    # actually sees.  See `lyrics_text.align_tokens`.
    ops = _lt.align_tokens([t for t, _ in js_ctoks], [t for t, _ in md_ctoks],
                           [js_words[ji][0] for _, ji in js_ctoks],
                           [md_words[mi][0] for _, mi in md_ctoks])
    md_seg: list = [None] * len(md_words)     # seg each MD word aligned to (content only)
    md_ref: list = [None] * len(md_words)     # (seg, word index) it aligned to
    # `placement=True`: a word the transcript heard differently still belongs to the
    # segment it was heard in.  Left out, it falls back to riding with the words
    # before it on its line and lands a segment early whenever it opens one.
    for jt, mt, _tag in _lt.pair_tokens(ops, placement=True):
        _rw, _si, _wi = js_words[js_ctoks[jt][1]]
        md_seg[md_ctoks[mt][1]] = _si
        md_ref[md_ctoks[mt][1]] = (_si, _wi)

    # ── Resolve every MD word to a seg, one line at a time ─────────────────────
    line_mis: dict = OrderedDict()            # line_id → [md_word_index...] in order
    for mi, (rw, l) in enumerate(md_words):
        line_mis.setdefault(l, []).append(mi)

    seg_of_md: list = [None] * len(md_words)
    for l, mis in line_mis.items():
        here = list(dict.fromkeys(md_seg[mi] for mi in mis if md_seg[mi] is not None))
        if len(here) == 1:
            for mi in mis:                    # whole line → its one seg (incl. gaps/♪)
                seg_of_md[mi] = here[0]
        elif len(here) >= 2:
            # split line → each word to its aligned seg.  A music note ♪ opens the
            # phrase it introduces, so it rides with the NEXT aligned seg (not the
            # previous one, which would strand the opening ♪ on the line before the
            # song); other unaligned tokens (/, gaps) ride with the current seg.
            nxt = [None] * len(mis)
            _s = None
            for k in range(len(mis) - 1, -1, -1):
                if md_seg[mis[k]] is not None:
                    _s = md_seg[mis[k]]
                nxt[k] = _s
            cur = here[0]
            for k, mi in enumerate(mis):
                if md_seg[mi] is not None:
                    cur = md_seg[mi]
                    seg_of_md[mi] = cur
                elif _is_note(md_words[mi][0]) and nxt[k] is not None:
                    seg_of_md[mi] = nxt[k]
                else:
                    seg_of_md[mi] = cur
        else:
            # no seg aligned to this line: recover a seg the alignment dropped
            # (e.g. reordered) via its line_ref pin, if its words are in the line.
            lw = set(_norm(" ".join(md_words[mi][0] for mi in mis)).split())
            for si in dia:
                sw = set(norm_segs[si].split())
                if segs[si].get('line_ref') == l and sw and len(sw & lw) / len(sw) >= 0.5:
                    for mi in mis:
                        seg_of_md[mi] = si
                    break

    # ── Per-seg MD text + score, and the lines each seg covers ─────────────────
    seg_mis: dict = defaultdict(list)         # seg → [md_word_index...] (MD order)
    seg_lines: dict = defaultdict(list)       # seg → [line_id...] distinct, in order
    for mi, si in enumerate(seg_of_md):
        if si is None:
            continue
        seg_mis[si].append(mi)
        l = md_words[mi][1]
        if not seg_lines[si] or seg_lines[si][-1] != l:
            seg_lines[si].append(l)

    quality: dict = {}
    for si in dia:
        mis = seg_mis.get(si)
        if mis:
            md_text = " ".join(md_words[mi][0] for mi in mis)
            quality[si] = {'score': _score(norm_segs[si], _norm(md_text)), 'md_text': md_text}
        elif norm_segs[si].split():
            # Real speech with no MD behind it. Scored at 0 whatever its length:
            # a two-word segment the script does not account for is exactly as much
            # of a discrepancy as a twenty-word one, and was silently unflagged.
            quality[si] = {'score': 0.0, 'md_text': ''}

    # ── Mid-line stage directions → anchored floating overlays ─────────────────
    # Each was recorded with the number of spoken words that precede it on its MD
    # line.  When the line was split across segments there is a real seg boundary
    # at that position, so anchor the direction there.  Otherwise (the whole line is
    # one segment, so its text can't be broken mid-flow) drop it just after the
    # segment holding the preceding words.  They join stage_dirs so the same
    # commit/reconcile path applies.
    placed_on_line: set = set()               # (line_id, text) already given a spot

    def _cut(si: int, mis: list, pos: int) -> tuple | None:
        """(si, k, gap_from, at) for a cut before the line's pos-th word inside
        segment si, timed by the nearest aligned words either side; None when
        the segment has no word timings to cut by."""
        ws = segs[si].get('words') or []
        before = next((md_ref[m] for m in reversed(mis[:pos]) if md_ref[m] and md_ref[m][0] == si), None)
        after = next((md_ref[m] for m in mis[pos:] if md_ref[m] and md_ref[m][0] == si), None)
        if not (ws and before and after and before[1] < after[1]):
            return None
        gap_from, at = ws[before[1]].get('end'), ws[after[1]].get('start')
        if gap_from is None or at is None:
            return None
        return (si, seg_mis[si].index(mis[pos]), float(min(gap_from, at)), float(at))
    for d in inline_dirs:
        mis     = line_mis.get(d['line'], [])
        pos     = d['pos']
        prev_si = seg_of_md[mis[pos - 1]] if 0 < pos <= len(mis) else None
        next_si = seg_of_md[mis[pos]]     if pos < len(mis)      else None
        # Carry the line's speaker on every direction so the emit step can attribute
        # it, e.g. `MARTIN: *(exasperated noise)*` between two ARTHUR turns gets a
        # MARTIN banner. (For a direction on its own line's own speaker, the context
        # check below suppresses a redundant banner.)
        _spk = line_meta.get(d['line'], {}).get('speaker', '')
        # Tone only with words still to say: `Cremona *(intrigue)*.` has a full stop left, no words.
        words_after = any(_norm_words(md_words[m][0]) for m in mis[pos:])
        mid = {'line': d['line']}
        if 0 < pos:
            mid['tone'] = _lt.direction_kind(d['text'], kinds) == 'tone'
            if not words_after or _lt.points_back(d['text']):
                mid['back'] = True           # about the words before it
        if next_si is not None and next_si != prev_si:
            placed_on_line.add((d['line'], d['text']))
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': next_si, 'speaker': _spk, **mid})
        elif prev_si is not None and next_si == prev_si and _cut(prev_si, mis, pos):
            # Inside one segment: the player splits it there (`within`).
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': prev_si + 1, 'speaker': _spk,
                               'within': [_cut(prev_si, mis, pos)], **mid})
        elif prev_si is not None:
            # No boundary here to hang it on, so it falls PAST the segment holding
            # its line.  When the same direction is already placed on this line
            # that is a repeat landing on somebody else's words: `*(Ding)* Ding!
            # *(Ding)* Ding!` kept whole by the transcript would stamp a (Ding) on
            # the reply that follows.  One is all the line can carry.
            if (d['line'], d['text']) in placed_on_line:
                continue
            placed_on_line.add((d['line'], d['text']))
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': prev_si + 1, 'speaker': _spk, **mid})
        else:
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'speaker': _spk, **mid})

    # ── Emit overlay: speaker banner per MD line + standalone stage directions ──
    line_first_seg: dict = {}                 # line_id → first seg (in order) covering it
    for si in dia:
        for l in seg_lines.get(si, []):
            line_first_seg.setdefault(l, si)

    items: list = []                          # (before_si, order, item); order 0=stage 1=speaker
    links: dict = {}
    seen_line: set = set()
    seg_line_words: dict = defaultdict(Counter)   # seg → line_id → how many of its words
    for mi, si in enumerate(seg_of_md):
        if si is not None:
            seg_line_words[si][md_words[mi][1]] += 1
    for si in dia:
        lls = seg_lines.get(si, [])
        if lls:
            # The DOMINANT line, not merely the first: a segment that ends with two
            # words of the next script line still belongs to the line it spoke most
            # of, and `line_ref` is what a later split and rebuild pin themselves to.
            counts = seg_line_words[si]
            links[si] = max(lls, key=lambda l: (counts[l], -lls.index(l)))
        for l in lls:
            if l in seen_line:
                continue
            seen_line.add(l)
            spk = line_meta.get(l, {}).get('speaker', '')
            if spk:
                items.append((si, 1, {'kind': 'speaker', 'text': spk,
                                      'stage': line_meta[l].get('stage', ''),
                                      'line': l, 'before_si': si}))

    covered = sorted(line_first_seg)

    def _place(sd: dict) -> int:
        """The segment a direction is shown before: a mid-line one's boundary,
        else the first segment of its line (no segment: the next line's, else the end)."""
        if sd.get('anchor_si') is not None:
            return min(sd['anchor_si'], len(segs))
        L = sd['before_line']
        if L in line_first_seg:
            return line_first_seg[L]
        k = bisect.bisect_left(covered, L)
        return line_first_seg[covered[k]] if k < len(covered) else len(segs)

    # Standalone stage directions: a committed stage_dir seg / labelled dead_air
    # claims the matching one by text so it isn't shown both as a seg AND a §.
    # The nearest of that text: a script that says "He sighs." twice has two,
    # and the first committed one is not necessarily the first written.
    materialized: set = set()
    mat_seg: dict = {}                        # stage_dir idx → committed seg index
    for _si, _seg in enumerate(segs):
        if _seg.get('kind') not in ('stage_dir', 'dead_air'):
            continue
        sn = _norm(_seg.get('_md_text') or _seg.get('text', ''))
        if not sn:
            continue
        same = [idx for idx, sd in enumerate(stage_dirs) if idx not in materialized and _norm(sd['text']) == sn]
        if same:
            idx = min(same, key=lambda i: abs(_place(stage_dirs[i]) - _si))
            materialized.add(idx)
            mat_seg[idx] = _si

    # Speaker in effect just before a seg position: the last dialogue line above
    # it. Used to decide when a direction needs its own banner (a different
    # speaker) vs. when the surrounding speaker already covers it.
    _dia_spk = sorted((si, line_meta.get(links.get(si), {}).get('speaker', '')) for si in dia)
    def _ctx_spk(pos: int) -> str:
        spk = ''
        for si, s in _dia_spk:
            if si < pos:
                spk = s or spk
            else:
                break
        return spk

    # The banner already queued AT a position.  `_ctx_spk` only sees segments
    # strictly before it, so a direction anchored at the start of its own speaker's
    # segment could never tell that the very next row names that speaker anyway,
    # and raised a second identical banner above it every time.
    _banner_at: dict = {si: it['text'] for si, _o, it in items if it['kind'] == 'speaker'}

    def _needs_banner(spk: str, pos: int) -> bool:
        """Whether a direction at `pos` has to name its speaker, or whether the
        dialogue around it already does."""
        return bool(spk) and spk != _ctx_spk(pos) and spk != _banner_at.get(pos)

    for idx, sd in enumerate(stage_dirs):
        _spk = sd.get('speaker', '')
        if idx in materialized:
            # The direction shows as its own committed seg, but if it belongs to a
            # DIFFERENT speaker than the surrounding dialogue (a wordless
            # `MARTIN: *(noise)*` committed between ARTHUR lines), still banner it.
            _msi = mat_seg.get(idx)
            if _msi is not None and 'line' in sd:
                links[_msi] = sd['line']      # a mid-line direction: its line's, like its words
            if _msi is not None and _needs_banner(_spk, _msi):
                items.append((_msi, -1, {'kind': 'speaker', 'text': _spk,
                                         'stage': '', 'line': None, 'before_si': _msi}))
            continue
        bsi = _place(sd)
        # An inline direction belongs to its speaker's line, so it sits AFTER the
        # banner (order 2), e.g. a line that opens with *(a noise)*: the noise
        # reads under MARTIN, not orphaned above the MARTIN banner. A standalone
        # direction sits above the banner (order 0).
        order = 2 if sd.get('anchor_si') is not None else 0
        # Banner the direction's speaker above it when that speaker differs from
        # the surrounding dialogue (suppresses a redundant banner on its own line).
        if _needs_banner(_spk, bsi):
            items.append((bsi, -1, {'kind': 'speaker', 'text': _spk,
                                    'stage': '', 'line': None, 'before_si': bsi}))
        item = {'kind': 'stage_dir', 'text': sd['text'], 'before_si': bsi}
        item.update({k: sd[k] for k in ('line', 'tone', 'back', 'within', 'speaker') if k in sd and 'line' in sd})
        items.append((bsi, order, item))

    # order at a tie: 0 standalone stage dir → 1 speaker banner → 2 inline stage dir
    items.sort(key=lambda t: (t[0], t[1]))
    # Two identical beats at one anchor say nothing the first does not.  A line the
    # transcript kept whole (`*(Ding)* Ding! *(Ding)* Ding! *(Ding)* Ding!`) has no
    # seg boundary to hang the repeats on, so they all fall to the same place and
    # stack up as three identical § rows against the line after.
    # A repeat cut inside a segment still splits it there: its cut joins the first's.
    overlay: list = []
    seen: dict = {}
    for si, _order, it in items:
        key = (si, it['kind'], it['text'])
        if key in seen:
            if it.get('within') and 'within' in seen[key]:
                seen[key]['within'] = seen[key]['within'] + it['within']
            continue
        seen[key] = it
        overlay.append(it)
    return overlay, quality, links
