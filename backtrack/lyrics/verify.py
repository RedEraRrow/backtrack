"""Checking timed lyrics against the markdown transcript: the word streams, split
candidates, the match-up report, and splitting a segment at word boundaries."""
from __future__ import annotations
import os
from backbone.ui import Colors as C
from backtrack.lyrics.text import (align_tokens as _align_tokens,
                                    pair_tokens as _pair_tokens,
                                    ends_a_thought as _ends_a_thought)
from backtrack.lyrics.md_overlay import (
    _norm_words, _spoken_text, _inline_stage_dirs, line_speaker as _line_speaker,
)
from backbone import timefmt




# The MD side of `_word_streams`, memoised on the script's mtime+size. Verify and
# speaker-split both ask for it, and each ran the whole read-and-parse again, and on
# a long episode that is the pause you feel before the report appears. The JSON
# side is not cached: `segs` is the live, edited state.
_MD_TOKS_CACHE: dict[str, tuple[tuple, list, dict]] = {}


def _md_word_stream(md_path: str) -> tuple[list[tuple], dict]:
    """The MD script's two halves, read and parsed once.

    Returns (md_toks, line_dirs):
      md_toks:   (token, line_id, speaker, word_index, raw_word): every token,
                  carrying WHICH WORD of its line it came from. A word can yield
                  two tokens ("no-one") or none ("..."), so the word index has to
                  be recorded as the stream is built; a direction's position is a
                  word offset, and only this makes the two commensurable.
      line_dirs: line_id → (n_words, [(word_offset, dir_text, after_punctuation),
                  ...]): the inline directions on that line, how long the line is,
                  and whether each one follows a mark the script ended a thought on,
                  which is what decides whether cutting there is safe to do in
                  bulk or only worth suggesting.

    `line_id` is the 0-based dialogue-line index: the SAME id space as the
    overlay's `line_ref`, so a computed split can pin each piece directly.
    """
    from backtrack.lyrics.formats import _parse_markdown_dialogue
    try:
        st = os.stat(md_path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    hit = _MD_TOKS_CACHE.get(md_path)
    if hit is not None and key is not None and hit[0] == key:
        return hit[1], hit[2]

    with open(md_path, encoding='utf-8') as fh:
        dl = _parse_markdown_dialogue(fh.read())

    md_toks: list[tuple] = []
    line_dirs: dict = {}
    lid = 0
    for item in dl:
        if item.is_empty() or item.is_stage_direction():
            continue
        spk = _line_speaker(item)          # named exactly as the overlay names it
        words = _spoken_text(item.text).split()
        for wi, rw in enumerate(words):
            for tok in _norm_words(rw):
                md_toks.append((tok, lid, spk, wi, rw))
        # Record with each direction whether the script has punctuated its way out
        # of the word before it. A cut there is a cut the script already made; one
        # anywhere else would break a sentence in half.
        line_dirs[lid] = (len(words),
                          [(pos, text, pos > 0 and _ends_a_thought(words[pos - 1]))
                           for pos, text in _inline_stage_dirs(item.text)])
        lid += 1

    if key is not None:
        _MD_TOKS_CACHE[md_path] = (key, md_toks, line_dirs)
    return md_toks, line_dirs


def _word_streams(segs: list, md_path: str):
    """Shared word-level alignment core for verification and speaker-splitting.

    Returns (js_toks, md_toks, ops):
      js_toks: (token, seg_index, word_index, start)             spoken words
      md_toks: (token, line_id, speaker, md_word_index, raw)     MD dialogue words
      ops:     `lyrics_text.align_tokens` opcodes over the two streams
    Both streams use `_norm_words`/`_spoken_text` so punctuation is ignored,
    hyphens are spaces, and inline stage directions never count as dialogue; the
    alignment then reconciles the spellings the two conventions differ on.
    line_id is the 0-based dialogue-line index: the SAME id space as the overlay's
    `line_ref`, so a computed split can pin each piece directly.
    """
    md_toks, _dirs = _md_word_stream(md_path)

    js_toks: list[tuple] = []
    js_raw:  list[str] = []
    for si, s in enumerate(segs):
        if s.get('kind') in ('dead_air', 'stage_dir'):
            continue
        ws = s.get('words')
        if ws:
            for wi, w in enumerate(ws):
                for tok in _norm_words(w.get('word', '')):
                    js_toks.append((tok, si, wi, w.get('start')))
                    js_raw.append(w.get('word', ''))
        else:
            # Word-less segment (SYLT / USLT, or one imported without timings):
            # fall back to its text, exactly as `build_md_overlay` does. Skipping it
            # here made verify and the overlay disagree about which segments exist,
            # so a split computed from one did not line up with the other.
            for wi, rw in enumerate(s.get('text', '').split()):
                for tok in _norm_words(rw):
                    js_toks.append((tok, si, wi, None))
                    js_raw.append(rw)

    ops = _align_tokens([j[0] for j in js_toks], [m[0] for m in md_toks],
                        js_raw, [m[4] for m in md_toks])
    return js_toks, md_toks, ops


def _split_candidates(segs: list, md_path: str) -> tuple[list[dict], list[tuple], list[tuple]]:
    """Find every dialogue segment the script says should be more than one beat,
    and the word-index boundaries to cut it at.

    Two things ask for a cut, and both are the script drawing a line the transcript
    ran through:

      • an MD LINE CHANGE inside the segment: Whisper merged consecutive script
        lines (MARTIN's "Dash away ..." + DOUGLAS & MARTIN's "... dash away, dash
        away, all!"), so one segment carries two speakers;
      • an INLINE STAGE DIRECTION inside it: `*(Australian accent)* Yip! *(Normal
        voice)* Mrs Badcrumble, ...` is one MD line but four beats, and a direction
        with no boundary to sit on has nowhere to go but after the whole segment,
        where it reads as a note on somebody else's words.

    A direction only earns a cut in BULK where the script has already punctuated its
    way out of the word before it.  One that interrupts a sentence, `Captain *(he
    assumes a French accent)* Martin duCref`, may well deserve its own beat, but
    that is a judgement about the performance, not something to do to 300 segments
    unattended, so it comes back as a suggestion instead.  A speaker change is never
    mid-sentence and is always cut.

    Returns (candidates, unplaced, suggested):
      candidates: {seg, boundaries, line_refs, runs}: cut points, and the line each
                   resulting piece pins to, so splitting lands one segment per
                   script beat, the clean baseline to word-split from.
      unplaced:   (line_id, word_offset, dir_text) for a direction sitting mid-line
                   whose word never aligned to the transcript, so there is no
                   boundary to cut at.  Reported rather than guessed: snapping to a
                   nearby word would put the direction somewhere the script does not.
      suggested:  (line_id, word_offset, dir_text) for a cut that IS placeable but
                   falls mid-sentence, offered for you to make by hand.
    """
    import bisect
    from collections import defaultdict
    js, md, ops = _word_streams(segs, md_path)
    md_toks, line_dirs = _md_word_stream(md_path)

    # Which words of each line carry a comparison token at all.  '...' and '♪' are
    # words of the script but none of the transcript, so a direction standing
    # against one, `Well ... *(he sighs)* ... we've got`, has to cut at the next
    # word that IS compared.  Stepping over them is not snapping: they are not
    # speech, so nothing spoken ends up on the wrong side of the cut.
    tokenized: dict = defaultdict(list)
    for _t, _ln, _s, _mw, _rw in md_toks:
        if not tokenized[_ln] or tokenized[_ln][-1] != _mw:
            tokenized[_ln].append(_mw)

    def _cut_at(ln: int, pos: int) -> int | None:
        """The word a direction at `pos` on line `ln` should cut before."""
        toks = tokenized.get(ln) or []
        i = bisect.bisect_left(toks, pos)
        return toks[i] if i < len(toks) else None

    dir_cuts: dict = {}                            # line_id → {md word index to cut at}
    want: list = []                                # (line, cut_word, dir_text) wanted
    mid_sentence: list = []                        # placeable, but not in bulk
    for ln, (n_words, dirs) in line_dirs.items():
        for pos, text, punctuated in dirs:
            if not 0 < pos < n_words:
                continue                           # opens or closes the line: no cut
            at = _cut_at(ln, pos)
            if at is None:
                continue                           # only unspoken words follow it
            if not punctuated:
                mid_sentence.append((ln, at, text))
                continue                           # suggest it; do not do it
            dir_cuts.setdefault(ln, set()).add(at)
            want.append((ln, at, text))
    jline: dict[int, tuple] = {}     # js token index → (line_id, speaker, md_word_index)
    for jt, mt, _tag in _pair_tokens(ops):
        jline.setdefault(jt, md[mt][1:4])

    per_seg: dict[int, list] = defaultdict(list)   # si → [(word_index, line, spk, md_word)]
    placed: set = set()                            # (line, md_word) the transcript kept
    for ti, (tok, si, wi, st) in enumerate(js):
        if ti in jline:
            ln, sp, mw = jline[ti]
            per_seg[si].append((wi, ln, sp, mw))
            placed.add((ln, mw))

    out = []
    for si in sorted(per_seg):
        # word index where a piece starts → the MD line that piece belongs to
        cuts: dict[int, tuple] = {}
        prev_line = None
        for wi, ln, sp, mw in per_seg[si]:
            if ln != prev_line:                    # a new script line starts here
                cuts.setdefault(wi, (ln, sp))
                prev_line = ln
            elif mw in dir_cuts.get(ln, ()):
                cuts.setdefault(wi, (ln, sp))      # a direction opens here
        starts = sorted(cuts)
        if len(starts) >= 2:
            out.append({'seg': si,
                        'boundaries': starts[1:],
                        'line_refs':  [cuts[w][0] for w in starts],
                        'runs':       [(cuts[w][0], cuts[w][1], w) for w in starts]})

    unplaced  = [(ln, at, text) for ln, at, text in want if (ln, at) not in placed]
    suggested = [(ln, at, text) for ln, at, text in mid_sentence if (ln, at) in placed]
    return out, unplaced, suggested


def _verify_matchup(segs: list, md_path: str) -> dict:
    """Full word-level verification of the JSON↔MD matchup.  Diffs the entire
    spoken word stream against the entire MD dialogue word stream (normalised:
    punctuation ignored, hyphens→spaces).  Independent of the segment alignment,
    so it surfaces every word the alignment missed (EXTRA / MISSING / CHANGED),
    plus SPLIT segments that merge two speakers into one.

    A spelling the two conventions simply write differently ("take-off" against
    "takeoff", "twenty-five" against "25") is matched rather than reported twice as
    a missing word and an extra one; an elision ("'cause" / "because") is matched for
    timing but still shown, because that one IS a difference in the script.

    Returns {'lines': [str], 'summary': {...}}.
    """
    js_toks, md_toks, ops = _word_streams(segs, md_path)

    def _near_time(i: int) -> float | None:
        """Timestamp at/near js_toks[i], falling back to the nearest preceding timed token."""
        if i < len(js_toks) and js_toks[i][3] is not None:
            return js_toks[i][3]
        for j in range(min(i, len(js_toks)) - 1, -1, -1):
            if js_toks[j][3] is not None:
                return js_toks[j][3]
        return None

    def _row(t, sym, label, col, loc, detail):
        """One aligned, colour-coded verification-report row: timestamp, badge, location, detail."""
        # Aligned, colour-coded row: dim timestamp · coloured badge · dim location
        # · the actual WORDS bright, so the eye lands on what differs.
        ts    = timefmt.clock(t) if t is not None else "  --:--  "
        badge = f"{col}{sym} {label:<7}{C.RESET}"          # 9 visible cols
        locf  = f"{C.DIM}{(loc or '')[:16]:<16}{C.RESET}"  # 16 visible cols
        return f"{C.DIM}{ts}{C.RESET}  {badge}  {locf}  {detail}"

    lines: list[str] = []
    n_extra = n_missing = n_changed = n_equal = n_elision = 0
    for tag, i1, i2, k1, k2 in ops:
        heard  = " ".join(js_toks[x][0] for x in range(i1, i2))
        script = " ".join(md_toks[x][0] for x in range(k1, k2))
        t = _near_time(i1)
        if tag in ('equal', 'boundary', 'number'):
            # The two conventions spell it differently and mean the same thing, so
            # it is matched and pinned; nothing to report.
            n_equal += (k2 - k1); continue
        if tag == 'elision':
            # Matched for timing (the word IS said), but the script and the
            # transcript disagree about how to write it, and that is worth seeing.
            n_elision += (k2 - k1)
            detail = f"{C.DIM}{heard}{C.RESET}  {C.ACCENT}→{C.RESET}  {C.PRIMARY}{script}{C.RESET}"
            lines.append(_row(t, '≈', 'ELIDED', C.ACCENT, f"L{md_toks[k1][1]}", detail))
        elif tag == 'delete':
            n_extra += (i2 - i1)
            lines.append(_row(t, '+', 'EXTRA', C.CYAN, '', f"{C.PRIMARY}{heard}{C.RESET}"))
        elif tag == 'insert':
            n_missing += (k2 - k1)
            loc = f"{md_toks[k1][2] or '?'} · L{md_toks[k1][1]}"
            lines.append(_row(t, '-', 'MISSING', C.YELLOW, loc, f"{C.PRIMARY}{script}{C.RESET}"))
        else:
            n_changed += max(i2 - i1, k2 - k1)
            detail = f"{C.DIM}{heard}{C.RESET}  {C.ACCENT}→{C.RESET}  {C.PRIMARY}{script}{C.RESET}"
            lines.append(_row(t, '~', 'CHANGED', C.MAGENTA, f"L{md_toks[k1][1]}", detail))

    split, unplaced, suggested = _split_candidates(segs, md_path)
    split_lines = []
    for c in split:
        # Header row, then one indented row per MD line showing the ACTUAL words
        # from that line inside the merged segment (the context, not just names).
        split_lines.append(_row(segs[c['seg']].get('start'), '⇄', 'SPLIT', C.GREEN,
                                f"seg {c['seg']}", ""))
        ws   = segs[c['seg']].get('words', [])
        cuts = [0] + list(c['boundaries']) + [len(ws)]
        for i in range(len(cuts) - 1):
            chunk = ws[cuts[i]:cuts[i + 1]]
            spk   = (c['runs'][i][1] if i < len(c['runs']) else '') or '?'
            txt   = " ".join(w.get('word', '') for w in chunk).strip()
            if not txt:
                continue
            split_lines.append(
                f"           {C.CYAN}{spk[:17]:<17}{C.RESET} {C.PRIMARY}{txt}{C.RESET}")

    for ln, pos, text in suggested:
        # Placeable, but the script has not finished a thought there. Worth a beat
        # of its own sometimes; never worth doing to every segment unattended.
        split_lines.append(_row(None, '·', 'MID-LINE', C.DIM, f"L{ln} w{pos}",
                                f"{C.DIM}({text}){C.RESET}"))

    for ln, pos, text in unplaced:
        # The script puts a direction between two words the transcript does not
        # have a boundary for: it dropped or reworded the word it sits against.
        split_lines.append(_row(None, '✦', 'UNPLACED', C.YELLOW, f"L{ln} w{pos}",
                                f"{C.DIM}({text}){C.RESET}"))

    total_js, total_md = len(js_toks), len(md_toks)
    rate = (100 * n_equal // max(1, total_md))
    summary = {'json_words': total_js, 'md_words': total_md, 'matched': n_equal,
               'extra': n_extra, 'missing': n_missing, 'changed': n_changed,
               'multi_speaker': len(split), 'unplaced_dirs': len(unplaced),
               'elided': n_elision, 'suggested': len(suggested), 'match_pct': rate,
               'discrepancies': len(lines) + len(split_lines)}
    header = [
        f"{C.BOLD}VERIFY · JSON ↔ MD word matchup{C.RESET}"
        f"   {C.DIM}(punctuation ignored · hyphens = spaces){C.RESET}",
        f"{C.BOLD}{rate}%{C.RESET} matched  {C.DIM}({n_equal}/{total_md} words){C.RESET}    "
        f"{C.YELLOW}- {n_missing} missing{C.RESET}   {C.CYAN}+ {n_extra} extra{C.RESET}   "
        f"{C.MAGENTA}~ {n_changed} changed{C.RESET}   {C.GREEN}⇄ {len(split)} to split{C.RESET}"
        + (f"   {C.ACCENT}≈ {n_elision} elided{C.RESET}" if n_elision else "")
        + (f"   {C.DIM}· {len(suggested)} mid-line{C.RESET}" if suggested else "")
        + (f"   {C.YELLOW}✦ {len(unplaced)} unplaced{C.RESET}" if unplaced else ""),
        "",
    ]
    body = list(lines)
    if split_lines:
        if lines:
            body.append("")
        body.append(f"{C.BOLD}Segments the script splits into more than one beat"
                    f"{C.RESET}  {C.DIM}: a new speaker, or a stage direction, part "
                    f"way through{C.RESET}")
        body += split_lines
    if not body:
        header.append(f"{C.GREEN}✔ every spoken word matches, and every segment is "
                      f"one script beat.{C.RESET}")
    return {'lines': header + body, 'summary': summary}


def _make_dead_air(start: float, end: float, label: str = "") -> dict:
    """Build a dead-air segment dict spanning start-end, optionally labelled."""
    return {"kind": "dead_air", "text": label,
            "start": round(start, 3), "end": round(end, 3), "words": []}


def _split_seg_at(seg: dict, boundaries: list, line_refs: list) -> list[dict]:
    """Split a dialogue seg into pieces at the given word-index boundaries; each
    piece takes its own word timings and is pinned to the matching line_ref.
    Words keep their wids (just re-grouped), so identity survives the split."""
    ws   = seg.get('words', [])
    cuts = [0] + list(boundaries) + [len(ws)]
    pieces = []
    for idx in range(len(cuts) - 1):
        chunk = ws[cuts[idx]:cuts[idx + 1]]
        if not chunk:
            continue
        p = {'text':  ' '.join(w.get('word', '') for w in chunk),
             'start': chunk[0].get('start'), 'end': chunk[-1].get('end'),
             'words': chunk}
        lref = line_refs[idx] if idx < len(line_refs) else None
        if lref is not None:
            p['line_ref'] = lref
        pieces.append(p)
    return pieces or [seg]
