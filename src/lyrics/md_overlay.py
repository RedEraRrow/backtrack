"""Shared MD↔JSON alignment: the single source of truth for how a markdown
script (speakers, stage directions, emphasis, punctuation) is overlaid onto a
timed transcript's segments.

Both the lyrics editor (`src.lyrics.lyrics_editor`) and the playback lyric
display (`src.lyrics.lyrics`) build their view from `build_md_overlay`, so the
two can never disagree about which speaker owns a line, where a stage direction
sits, or what a segment's text actually reads.

This module deliberately has no module-level dependency on `src.lyrics.lyrics`
(the markdown parser is imported lazily inside `build_md_overlay`) so the
playback side can import it at module scope without a cycle.
"""
from __future__ import annotations
import re
import unicodedata


def _norm(t: str) -> str:
    """Canonical match normalization: Unicode-fold (NFKD), lowercase, joiners
    (hyphens / periods / slashes)→spaces, other punctuation dropped.  This is the
    single basis for ALL JSON↔MD comparison (alignment and the verify report) so
    the two never disagree about what 'matches'.  Treating '.' and '/' as word
    boundaries makes a dotted abbreviation match its spoken-out letters
    (C.P.L. → "c p l" == "C P L", G.P → "g p" == "G P").  Folding keeps non-ASCII
    letters (accented Latin → base letter, Cyrillic/CJK preserved) instead of
    deleting them, which previously made non-English lyrics vanish from the stream."""
    t = unicodedata.normalize('NFKD', (t or "")).lower()
    t = re.sub(r'[-./]', ' ', t)
    return re.sub(r'[^\w ]', '', t).strip()


def _norm_words(t: str) -> list[str]:
    """`_norm` split into comparison tokens (a hyphenated word yields two)."""
    return _norm(t).split()


# Shared patterns so `_spoken_text` (what counts as dialogue) and
# `_inline_stage_dirs` (where a mid-line direction sits) can never disagree.
_LINK_RE  = re.compile(r'\[([^\]]*)\]\([^)]*\)')      # [text](url)
_STAGE_RE = re.compile(r'\*?\(([^)]*)\)\*?')          # *(stage dir)* / (aside)


def _spoken_text(t: str) -> str:
    """Keep only actually-spoken words from an MD dialogue line: drop inline stage
    directions like *(sighs)* / (aside) and reduce [label](url) links to their
    label.  Used for BOTH matching and verification so a stage direction is never
    mistaken for dialogue."""
    if not t:
        return t
    t = _LINK_RE.sub(r'\1', t)    # [text](url) → text
    t = _STAGE_RE.sub(' ', t)     # *(stage dir)* / (aside) → removed
    return t


def _inline_stage_dirs(t: str) -> list[tuple[int, str]]:
    """Mid-line stage directions embedded in a dialogue line, e.g.
    'I never thought *(she pauses)* it would end'.  Returns
    [(n_spoken_words_before, dir_text), ...] using the SAME stripping as
    `_spoken_text`, so the counts line up with the md_words stream: a direction
    sitting before the k-th spoken word of the line reports n == k."""
    if not t:
        return []
    t = _LINK_RE.sub(r'\1', t)    # links first, so their (url) isn't taken for a dir
    out: list[tuple[int, str]] = []
    last = count = 0
    for m in _STAGE_RE.finditer(t):
        count += len(t[last:m.start()].split())
        d = (m.group(1) or "").strip()
        if d:
            out.append((count, d))
        last = m.end()
    return out


# Stage-direction kinds (cycled in-editor with `x`, persisted in the sidecar):
#   inline    — a mid-phrase beat, indented under the words (✦)
#   tone      — tonal / pronunciation note, same indent (~), e.g. drawn-out speech
#   external  — a framed section (scene/sound directions, and another person's
#               aside); who/when is left to be read from the text and context.
_SD_SCOPES = ('inline', 'tone', 'external')


def _sd_scope(seg: dict) -> str:
    """The direction's kind (see _SD_SCOPES). Migrates older segs that used the
    'speaker' scope or a separate `tone` flag."""
    scope = seg.get('scope')
    if scope in _SD_SCOPES:
        return scope
    return 'tone' if seg.get('tone') else 'inline'


def _is_framed(seg: dict) -> bool:
    """A beat that stands alone as its own framed section (dotted rules, no
    speaker): dead air (silence — belongs to nobody) or an external stage
    direction (scene/sound, or another person's aside)."""
    k = seg.get('kind')
    return k == 'dead_air' or (k == 'stage_dir' and _sd_scope(seg) == 'external')


def _reading_time(text: str) -> float:
    """A sensible on-screen read time for a short note (e.g. a tonal/pronunciation
    beat): ~2.3 words/sec, floored so even a one-word note lingers long enough."""
    words = len((text or '').split())
    return round(max(0.8, words / 2.3), 3)


def build_md_overlay(segs: list[dict], md_path: str) -> tuple[list, dict, dict]:
    """Overlay the MD script onto the JSON segments via ONE word-level alignment.

    The JSON and MD are the same spoken words (modulo punctuation / hyphens / ♪).
    So we align the two word streams once (difflib) and then, line by line, hand
    each MD line's words to whichever segment its words lined up with:
      • one segment covers the line  → it shows the whole line (with punctuation),
      • several segments cover it     → each shows its own run of the line's words.
    A segment whose words span two MD lines shows both (and is a split candidate).

    Returns (overlay, quality, links):
      overlay — [{kind:'speaker'|'stage_dir', text, before_si, stage?, line?, lean?}],
                display only.  A speaker banner's `line` is the MD line it labels
                (None for a banner raised over a stage direction), so a consumer
                can scope the speaker to exactly the segs `links` pins to it
                instead of carrying it forward indefinitely.  A direction's `lean`
                ('prev'/'next') says which side of its boundary it is ABOUT, for a
                consumer that cannot give it a beat of its own; standalone
                directions (their own MD line) lean 'next' by default.
      quality — si → {score, md_text} for every dialogue seg the MD explains.
      links   — si → line_id, the seg's MD line, recorded by the caller as line_ref.
    """
    import difflib, bisect
    from collections import OrderedDict, defaultdict
    from src.lyrics.lyrics import _parse_markdown_dialogue

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
        spk = " & ".join(s.strip() for s in item.speakers) if item.speakers else ""
        if item.stage_dir and not item.speakers:      # a bare (aside) with no speaker
            stage_dirs.append({'text': item.stage_dir, 'before_line': lid})
            banner_stage = ""
        else:
            banner_stage = item.stage_dir              # inline dir → shown on the banner
        line_meta[lid] = {'speaker': spk, 'stage': banner_stage}
        for rw in _spoken_text(item.text).split():
            md_words.append((rw, lid))
        for pos, dtext in _inline_stage_dirs(item.text):
            inline_dirs.append({'text': dtext, 'line': lid, 'pos': pos})
        lid += 1

    def _score(a: str, b: str) -> float:
        """Jaccard-like word-overlap score between two normalized strings."""
        wa, wb = set(a.split()), set(b.split())
        return len(wa & wb) / max(len(wa), len(wb)) if wa and wb else 0.0

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
        else:                    # word-less seg (SYLT/USLT) — fall back to its text
            for wi, rw in enumerate(segs[si].get('text', '').split()):
                js_words.append((rw, si, wi))
    js_ctoks = [(t, ji) for ji, (rw, _, _) in enumerate(js_words) for t in _norm_words(rw)]
    md_ctoks = [(t, mi) for mi, (rw, _) in enumerate(md_words) for t in _norm_words(rw)]
    sm = difflib.SequenceMatcher(None, [t for t, _ in js_ctoks],
                                 [t for t, _ in md_ctoks], autojunk=False)
    md_seg: list = [None] * len(md_words)     # seg each MD word aligned to (content only)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != 'equal':
            continue
        for o in range(i2 - i1):
            md_seg[md_ctoks[j1 + o][1]] = js_words[js_ctoks[i1 + o][1]][1]

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
                elif md_words[mi][0] == '♪' and nxt[k] is not None:
                    seg_of_md[mi] = nxt[k]
                else:
                    seg_of_md[mi] = cur
        else:
            # no seg aligned to this line — recover a seg the alignment dropped
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
        elif len(norm_segs[si].split()) >= 3:
            quality[si] = {'score': 0.0, 'md_text': ''}   # real speech, absent from the MD

    # ── Mid-line stage directions → anchored floating overlays ─────────────────
    # Each was recorded with the number of spoken words that precede it on its MD
    # line.  When the line was split across segments there is a real seg boundary
    # at that position — anchor the direction there.  Otherwise (the whole line is
    # one segment, so its text can't be broken mid-flow) drop it just after the
    # segment holding the preceding words.  They join stage_dirs so the same
    # commit/reconcile path applies.
    for d in inline_dirs:
        mis     = line_mis.get(d['line'], [])
        pos     = d['pos']
        prev_si = seg_of_md[mis[pos - 1]] if 0 < pos <= len(mis) else None
        next_si = seg_of_md[mis[pos]]     if pos < len(mis)      else None
        # Carry the line's speaker on every direction so the emit step can attribute
        # it — e.g. `MARTIN: *(exasperated noise)*` between two ARTHUR turns gets a
        # MARTIN banner. (For a direction on its own line's own speaker, the context
        # check below suppresses a redundant banner.)
        _spk = line_meta.get(d['line'], {}).get('speaker', '')
        # Which side of the boundary the direction is ABOUT.  One that follows
        # spoken words is describing those words ('*(She pronounces it "roth.")*'
        # after the word it refers to), so a consumer with nowhere to put a beat of
        # its own must fall back to the line just gone, not the line coming up.
        # One that opens its line describes what happens before the speaker starts.
        _lean = 'prev' if pos > 0 else 'next'
        if next_si is not None and next_si != prev_si:
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': next_si, 'speaker': _spk, 'lean': _lean})
        elif prev_si is not None:
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': prev_si + 1, 'speaker': _spk, 'lean': _lean})
        else:
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'speaker': _spk, 'lean': _lean})

    # ── Emit overlay: speaker banner per MD line + standalone stage directions ──
    line_first_seg: dict = {}                 # line_id → first seg (in order) covering it
    for si in dia:
        for l in seg_lines.get(si, []):
            line_first_seg.setdefault(l, si)

    items: list = []                          # (before_si, order, item); order 0=stage 1=speaker
    links: dict = {}
    seen_line: set = set()
    for si in dia:
        lls = seg_lines.get(si, [])
        if lls:
            links[si] = lls[0]                # dominant line → pin
        for l in lls:
            if l in seen_line:
                continue
            seen_line.add(l)
            spk = line_meta.get(l, {}).get('speaker', '')
            if spk:
                items.append((si, 1, {'kind': 'speaker', 'text': spk,
                                      'stage': line_meta[l].get('stage', ''),
                                      'line': l, 'before_si': si}))

    # Standalone stage directions: a committed stage_dir seg / labelled dead_air
    # claims the matching one by text so it isn't shown both as a seg AND a ✦.
    materialized: set = set()
    mat_seg: dict = {}                        # stage_dir idx → committed seg index
    for _si, _seg in enumerate(segs):
        if _seg.get('kind') not in ('stage_dir', 'dead_air'):
            continue
        sn = _norm(_seg.get('_md_text') or _seg.get('text', ''))
        if not sn:
            continue
        for idx, sd in enumerate(stage_dirs):
            if idx not in materialized and _norm(sd['text']) == sn:
                materialized.add(idx)
                mat_seg[idx] = _si
                break

    # Speaker in effect just before a seg position — the last dialogue line above
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

    covered = sorted(line_first_seg)
    for idx, sd in enumerate(stage_dirs):
        _spk = sd.get('speaker', '')
        if idx in materialized:
            # The direction shows as its own committed seg, but if it belongs to a
            # DIFFERENT speaker than the surrounding dialogue (a wordless
            # `MARTIN: *(noise)*` committed between ARTHUR lines), still banner it.
            _msi = mat_seg.get(idx)
            if _spk and _msi is not None and _spk != _ctx_spk(_msi):
                items.append((_msi, -1, {'kind': 'speaker', 'text': _spk,
                                         'stage': '', 'line': None, 'before_si': _msi}))
            continue
        if sd.get('anchor_si') is not None:   # mid-line dir pinned to a seg boundary
            bsi = min(sd['anchor_si'], len(segs))
            # An inline direction belongs to its speaker's line, so it sits AFTER
            # the banner (order 2) — e.g. a line that opens with *(a noise)*: the
            # noise reads under MARTIN, not orphaned above the MARTIN banner.
            order = 2
        else:
            L = sd['before_line']
            if L in line_first_seg:
                bsi = line_first_seg[L]
            else:                             # its line has no seg → next covered line, else end
                k = bisect.bisect_left(covered, L)
                bsi = line_first_seg[covered[k]] if k < len(covered) else len(segs)
            order = 0                          # a standalone direction sits above the banner
        # Banner the direction's speaker above it when that speaker differs from
        # the surrounding dialogue (suppresses a redundant banner on its own line).
        if _spk and _spk != _ctx_spk(bsi):
            items.append((bsi, -1, {'kind': 'speaker', 'text': _spk,
                                    'stage': '', 'line': None, 'before_si': bsi}))
        items.append((bsi, order, {'kind': 'stage_dir', 'text': sd['text'],
                                   'lean': sd.get('lean', 'next'), 'before_si': bsi}))

    # order at a tie: 0 standalone stage dir → 1 speaker banner → 2 inline stage dir
    items.sort(key=lambda t: (t[0], t[1]))
    overlay = [it for _, _, it in items]
    return overlay, quality, links
