#!/usr/bin/env python3
"""Time a markdown script against its audio, producing the transcript JSON the
lyrics editor and the player read.

The script is the ground truth here, not a guess at it: every spoken word is
already written down, so this only ever asks WHEN each of those words is said,
never WHICH word it was. The result is a transcript whose word stream is identical
to the script's by construction — so `build_md_overlay` has nothing to reconcile,
every segment is already one script line, and `line_ref` is known rather than
inferred.

Timing comes from a transcription of the same audio (MacWhisper, WhisperX, the
OpenAI tooling — anything that writes segments with timestamps). That transcription
is wrong about plenty of WORDS and right about WHEN, which is the half that is
wanted. It measures a boundary roughly every one and a half seconds, so the script
is pinned to a measurement that often and its error is bounded by the distance
between two of them. It cannot accumulate. This runs in about a second per episode
and needs nothing installed.

`--mfa` then refines those times to the phoneme with Montreal Forced Aligner. It is
a refinement and is allowed to fail: a passage MFA will not align — singing, or two
people at once — simply keeps the time it already had. Alignment is never the thing
holding the timeline together.

    tools/align_script.py "1-04 Douz.mp3"           # one episode, seconds
    tools/align_script.py *.mp3                     # the set
    tools/align_script.py --mfa "1-04 Douz.mp3"     # and refine it, minutes
    tools/align_script.py --validate-only *.mp3     # just the OOV report

Anchors are looked for beside the audio as `<stem>.whisper.json`, or in
`timings/<stem>.json`. `--mfa` needs Montreal Forced Aligner with the english_mfa
acoustic model and the english_uk_mfa dictionary/G2P (`mfa model download ...`).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.lyrics import lyrics_text as lt

_lt = lt
from src.lyrics.lyrics import _find_markdown_for_audio, _parse_markdown_dialogue

# MFA aligns a whole file as ONE utterance, and a 28-minute one does not align at
# all — the Viterbi search finds no path at the default beam, and widening it hits
# a database error instead. So the file is walked in windows.
#
# The window size is set by QUALITY, not by what merely completes. Measured on this
# corpus, the aligner degrades long before it refuses: at 900s it returns a full
# TextGrid containing a 60-second word, which is drift dressed up as success. At
# 300s the same audio comes back at 159 wpm with nothing over 5s.
#
#     60s  138 wpm  longest word  4.7s
#    300s  159 wpm  longest word  4.7s   <- here
#    600s  161 wpm  longest word  7.8s
#    900s  137 wpm  longest word 60.6s
# How much audio one utterance covers. The aligner is accurate on a sentence and
# drifts over a paragraph; anchors let us stay at the accurate end.
UTTERANCE_S = 25.0
PAD_S = 0.25          # a little air either side, so a word is not clipped
RETRY_SPLITS = 2      # how many times a refused utterance may be halved

ACOUSTIC = "english_mfa"
DICTIONARY = "english_uk_mfa"
G2P = "english_uk_mfa"
SAMPLE_RATE = 16000

# Emphasis and the marks that only ever sit outside a word.  Apostrophes and
# internal hyphens stay: the dictionary has "don't" and "o'clock", and a hyphen
# is a real word boundary the aligner should see, not punctuation to discard.
_EDGE = r'[*_`~"“”‘’\'()\[\]{}.,!?;:…]'


def lab_tokens(word: str) -> list[str]:
    """The aligner's tokens for one script word — usually one, sometimes none.

    A hyphenated word becomes two tokens because that is two spoken words and
    the dictionary has no hyphenated entries; "..." and "♪" become none, being
    punctuation and notation rather than speech.  The caller keeps the mapping
    back, so a word splitting in two does not desynchronise anything.
    """
    w = re.sub(r'^' + _EDGE + r'+|' + _EDGE + r'+$', '', (word or "").strip())
    w = w.replace('’', "'")
    out = []
    for part in re.split(r'[-–—/]', w):
        part = re.sub(r'^' + _EDGE + r'+|' + _EDGE + r'+$', '', part)
        if re.search(r'[^\W\d_]|\d', part):
            out.append(part)
    return out


def script_stream(md_path: str):
    """The script as (words, lines, dirs).

    words is [(raw, line_id)] in order, lines is line_id -> speaker, and dirs is
    [(line_id_it_precedes, text)] for the stage directions that stand on their own
    line. Those are beats of the script in their own right — the bing bong, the
    door, the pause — not notes about a neighbouring line, so they are carried
    through and given their own segment rather than dropped here and reconstructed
    later from whatever silence happens to be spare.
    """
    with open(md_path, encoding='utf-8') as fh:
        dl = _parse_markdown_dialogue(fh.read())
    words, lines, dirs, inline, lid = [], {}, [], {}, 0
    for item in dl:
        if item.is_empty():
            continue
        if item.is_stage_direction():
            dirs.append((lid, item.stage_dir))
            continue
        lines[lid] = " & ".join(s.strip() for s in item.speakers) if item.speakers else ""
        spoken = lt.spoken_text(item.text).split()
        # A direction inside a line is a beat too — the yawn happens between two
        # sentences, not after the whole speech. Where the script has punctuated its
        # way out of the word before it, the line is cut there and the direction
        # sits in the silence the aligner measured between them. Mid-sentence ones
        # have no such moment and stay with the line (see docs/script-etiquette.md).
        cuts = [(pos, text) for pos, text in lt.inline_stage_dirs(item.text)
                if 0 < pos < len(spoken) and lt.ends_a_thought(spoken[pos - 1])]
        if cuts:
            inline[lid] = cuts
        for raw in spoken:
            words.append((raw, lid))
        lid += 1
    return words, lines, dirs, inline


def build_corpus(mp3: Path, md: Path, corpus: Path):
    """Write the one-file MFA corpus: 16k mono wav plus its .lab transcript.

    Returns (words, lines, dirs, tok2word) — tok2word maps each .lab token back to the
    script word it came from, which is what lets a hyphenated word split for the
    aligner and still be reassembled afterwards.
    """
    corpus.mkdir(parents=True, exist_ok=True)
    words, lines, dirs, inline = script_stream(str(md))
    toks, tok2word = [], []
    for wi, (raw, _lid) in enumerate(words):
        for t in lab_tokens(raw):
            toks.append(t)
            tok2word.append(wi)
    (corpus / f"{mp3.stem}.lab").write_text(" ".join(toks) + "\n", encoding='utf-8')
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp3),
                    "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
                    str(corpus / f"{mp3.stem}.wav")], check=True)
    return words, lines, dirs, inline, tok2word


def read_textgrid_words(path: Path) -> list[tuple[float, float, str]]:
    """The non-empty intervals of a TextGrid's word tier, in time order.

    Parsed directly rather than through a library: MFA writes the long text
    format, one tier of interest, and the shape is three lines per interval.
    """
    text = path.read_text(encoding='utf-8')
    # Isolate the "words" tier; MFA also writes a "phones" tier after it.
    m = re.search(r'name\s*=\s*"words".*?(?=item\s*\[\d+\]:|\Z)', text, re.S)
    body = m.group(0) if m else text
    out = []
    for xmin, xmax, label in re.findall(
            r'xmin\s*=\s*([\d.]+)\s*\n\s*xmax\s*=\s*([\d.]+)\s*\n\s*text\s*=\s*"((?:[^"]|"")*)"',
            body):
        label = label.replace('""', '"').strip()
        if label and label not in ("<eps>", "sil", "sp", "spn"):
            out.append((float(xmin), float(xmax), label))
    return out


def to_segments_direct(words, lines, dirs, inline, times) -> tuple[list[dict], dict]:
    """Zip the aligner's word times back onto the script, one segment per line.

    The two streams are the same words in the same order, so this is a walk, not
    an alignment — which is the whole point of aligning the script rather than a
    transcription of it.
    """
    stats = {'script_words': len(words), 'words_timed': len(times)}
    # Neighbouring utterances share PAD_S of audio at their seam, so the aligner can
    # place the last word of one and the first of the next in the same moment and
    # report them a fraction out of order. They are consecutive words of one script,
    # so the order is not in doubt: hold each to starting no earlier than the last
    # one finished. Only the overlap moves, never a word into different speech.
    order = 0.0
    for wi in sorted(times):
        st, en = times[wi]
        st = max(st, order)
        times[wi] = (st, max(en, st))
        order = times[wi][1]

    # "..." and "--" are punctuation: they carry no token, so the aligner is never
    # asked about them and can never answer. Leaving them timeless would strand any
    # segment that happens to start with one, so each is pinned to the moment its
    # neighbours leave for it — a real position, zero width, in the right place.
    filled = dict(times)
    run: list[int] = []
    prev_end = 0.0
    for wi in range(len(words) + 1):
        if wi < len(words) and wi not in times:
            run.append(wi)
            continue
        if run:
            nxt = times[wi][0] if wi < len(words) else prev_end
            step = (nxt - prev_end) / (len(run) + 1)
            for k, w in enumerate(run):
                at = round(prev_end + step * (k + 1), 3)
                filled[w] = (at, at)
            run = []
        if wi < len(words):
            prev_end = times[wi][1]
    stats['interpolated'] = len(filled) - len(times)
    times = filled

    # Standalone directions are beats of the script, so they get segments of their
    # own, sitting in the silence between the lines they fall between. Emitting them
    # here rather than leaving them to be re-derived at playback is what makes them
    # a thing with a time: otherwise they have no place of their own and end up
    # printed against whichever line happens to be adjacent.
    before: dict[int, list] = {}
    for lid, text in dirs:
        before.setdefault(lid, []).append(text)

    segs: list[dict] = []
    cur: list[dict] = []
    cur_line = None

    def emit_dialogue(ws, lid):
        segs.append({'kind': 'dialogue', 'text': " ".join(w['word'] for w in ws),
                     'start': round(ws[0]['start'], 3),
                     'end': round(ws[-1]['end'], 3),
                     'words': ws,
                     'line_ref': lid,
                     'speaker': lines.get(lid, '')})

    def flush():
        """Emit this line, cut at any inline direction, with the direction between."""
        if not cur or cur_line is None:
            return
        cuts = inline.get(cur_line, [])
        at = 0
        for pos, text in cuts:
            if not 0 < pos < len(cur) or pos <= at:
                continue
            emit_dialogue(cur[at:pos], cur_line)
            a, b = cur[pos - 1]['end'], cur[pos]['start']
            segs.append({'kind': 'stage_dir', 'text': text, '_md_text': text,
                         'start': round(a, 3), 'end': round(max(a, b), 3),
                         'line_ref': cur_line, 'words': []})
            at = pos
        emit_dialogue(cur[at:], cur_line)

    def place(lid, upto):
        """Give each direction waiting before line `lid` its share of the silence."""
        texts = before.pop(lid, None)
        if not texts:
            return
        gap_from = segs[-1]['end'] if segs else 0.0
        room = max(0.0, upto - gap_from)
        # No silence to sit in (two lines butted together) means the direction has
        # no moment of its own; give it a zero-width one rather than steal from
        # either neighbour, and it still reads as its own beat.
        step = room / len(texts) if room > 0.05 else 0.0
        for k, text in enumerate(texts):
            a = gap_from + step * k
            segs.append({'kind': 'stage_dir', 'text': text, '_md_text': text,
                         'start': round(a, 3), 'end': round(a + step, 3), 'words': []})

    line_start = {}
    for wi, (raw, lid) in enumerate(words):
        line_start.setdefault(lid, times[wi][0])

    for wi, (raw, lid) in enumerate(words):
        if lid != cur_line:
            flush()
            place(lid, line_start.get(lid, times[wi][0]))
            cur, cur_line = [], lid
        t = times.get(wi)
        cur.append({'word': raw,
                    'start': round(t[0], 3) if t else None,
                    'end': round(t[1], 3) if t else None})
    flush()
    for lid in sorted(before):                  # anything after the last line
        place(lid, segs[-1]['end'] if segs else 0.0)
    stats['stage_dirs'] = sum(1 for x in segs if x['kind'] == 'stage_dir')
    stats['segments'] = len(segs)
    return segs, stats


_TS_RE = re.compile(r'(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\s*-\s*(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)')


def load_anchors(path: Path) -> list[dict]:
    """Read a transcription's segment boundaries, whichever tool wrote them.

    Only start, end and text are wanted — the words come from the script, so a
    transcript without word-level timings does the job exactly as well as one with.

    Two shapes are understood: {"segments": [{start, end, text}]} as WhisperX and
    the OpenAI tooling write it, and MacWhisper's bare list of
    {"text", "timestamp": "MM:SS.mmm-MM:SS.mmm"}.
    """
    data = json.loads(path.read_text(encoding='utf-8'))
    rows = data["segments"] if isinstance(data, dict) else data
    out = []
    for r in rows:
        text = (r.get("text") or "").strip()
        if "start" in r and "end" in r:
            out.append({"start": float(r["start"]), "end": float(r["end"]), "text": text})
            continue
        m = _TS_RE.search(r.get("timestamp") or "")
        if not m:
            continue

        def secs(h, mm, ss):
            return (int(h) * 3600 if h else 0) + int(mm) * 60 + float(ss)

        out.append({"start": secs(*m.group(1, 2, 3)),
                    "end": secs(*m.group(4, 5, 6)), "text": text})
    out.sort(key=lambda r: r["start"])
    return [r for r in out if r["end"] > r["start"] and r["text"]]


def audio_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out)


_SAME_SOUNDS = 0.80      # how alike two spellings of one stretch of speech must be


def _one_place(a_toks, md_toks, i1, i2, j1, j2) -> bool:
    """Does this disagreement still name a single moment of audio?

    Token for token it does: one heard word against one written one is the same
    instant whichever was right, and the script already owns the word.

    Unequal runs usually do not — two heard words against four written ones say
    nothing about which of the four was said when. But some are one stretch of
    speech divided differently by two writers rather than a real disagreement, and
    those read as one moment too. Joining each side up tells them apart: `alright`
    against `all right`, `dyou` against `do you`, `hes` against `he is` come back
    nearly identical, while `he was` against `douz here you are` does not.
    """
    if i2 - i1 == j2 - j1:
        return True
    return difflib.SequenceMatcher(
        None, "".join(a_toks[i1:i2]), "".join(md_toks[j1:j2])).ratio() >= _SAME_SOUNDS


def map_words_to_anchors(words, anchors, positional: bool) -> dict[int, int]:
    """Say which transcription segment each script word was spoken in.

    The script and the transcription are the same speech written down twice: the
    script correctly and the transcription approximately.  Matching them word by
    word says nothing useful about WHICH word was said — the script already knows
    that — but it does say WHEN, because the transcription carries a clock and the
    script does not.

    A pairing is only worth a time if it stands token for token. Where the two
    streams disagree over a RUN of unequal length — two heard words against four
    written ones — nothing says which of the four was said when, and parking them
    all on the nearest segment is how twenty-five words come to share one
    two-second measurement and land half a minute of speech in the wrong place.

    A one-for-one disagreement is different, and the difference is the whole reason
    this works: we never need to know WHICH word, only WHEN. The script already
    owns the word. So when the transcription heard one token where the script has
    one token, that is the same instant of audio whether it heard it correctly or
    not — `creef` for Crieff, `dews` for Douz, `uh` for er, `liters` for litres.
    Discarding those costs real measurements and buys nothing: on this episode they
    are 23 more lines, out of 414, whose start is read off a clock instead of
    guessed.

    `positional` keeps the unequal runs as well, parked by position. That is what
    `group_by_anchor` wants, because every word must land in some utterance and a
    rough bin is better than none. It is never what a timeline wants.

    Returns a SPARSE {script word index: anchor index}, strictly increasing in both
    — a word can never be placed before one the script puts ahead of it, whatever
    the matcher says.
    """
    a_toks, a_seg = [], []
    for si, a in enumerate(anchors):
        for raw in _lt.spoken_text(a.get("text", "")).split():
            for t in _lt.norm_words(raw):
                a_toks.append(t)
                a_seg.append(si)
    md_toks, md_word = [], []
    for wi, (raw, _l) in enumerate(words):
        for t in _lt.norm_words(raw):
            md_toks.append(t)
            md_word.append(wi)

    ops = _lt.align_tokens(a_toks, md_toks)
    if not positional:
        ops = [o for o in ops if o[0] != 'replace' or _one_place(a_toks, md_toks, *o[1:])]
    pairs = {}
    for ai, mi, _tag in _lt.pair_tokens(ops, placement=True):
        pairs.setdefault(md_word[mi], a_seg[ai])

    # Script order is ground truth, so the mapping must not go backwards. It can:
    # a repeated phrase ("No. No. No.") lets the matcher pair a later script word
    # with an earlier segment, and one such pairing would drag a whole passage back
    # through the audio. Any word that would move time backwards is dropped, not
    # corrected — it is one missing pin among hundreds, and the words either side
    # already say where it is.
    out, high = {}, -1
    for wi in sorted(pairs):
        if pairs[wi] >= high:
            out[wi] = high = pairs[wi]
    return out


def times_from_anchors(words, anchors):
    """Word times read straight off the transcription's clock, with no audio.

    This is the timing floor for every episode, and the reason drift is not
    possible rather than merely unlikely.

    Each transcription segment is its own small timeline: it measured when a run of
    speech started and stopped, and the script words belonging to that run are laid
    out inside it in proportion to their length.  Between two segments there is
    silence, which belongs to neither — the pause before a line is part of the
    performance, and a model that lets the next line start where the last one
    finished loses every pause in the episode and pulls every line early.

    So there is no single ruler over word boundaries. A word has a start and an end
    and they are not the same instant as its neighbours'.

    What makes drift impossible is that a segment's two ends are MEASURED, about
    once every one and a half seconds. Word placement inside a segment is an
    estimate, but the estimate is discarded at the segment's end, where the clock
    is read again. Error is bounded by the length of one segment and CANNOT
    ACCUMULATE — which is the whole difference between this and pacing a script
    evenly across a duration, a method with one measurement at each end of half an
    hour whose error therefore grows all the way to the middle.

    MFA, when it runs, refines these times to the phoneme. It does not put a line
    in a better second; it places words precisely within a second already correct.

    Returns ({word index: (start, end)} for EVERY word, [(first, last, t0, t1)]
    blocks), the blocks being what the caller reports on: the longest one is the
    longest stretch holding no measurement, and so the honest bound on how wrong a
    single line can be.
    """
    word_seg = map_words_to_anchors(words, anchors, positional=False)
    if not word_seg:
        return {}, []

    # Longer words take longer to say, so they are given more of their segment than
    # a count of words would. One subtraction per lookup once accumulated.
    cost = [0.0]
    for raw, _l in words:
        cost.append(cost[-1] + len(_lt.matchable(raw)) + 1.0)

    # One block per segment that caught any script word: the words it covers and
    # the two measured moments they sit between. Blocks are kept strictly in order
    # and non-overlapping; the transcription's own segments overlap each other
    # about 6% of the time, and a block that would start before the last one ended
    # is trimmed rather than dropped, so its measurement is still used.
    first_last: dict[int, list[int]] = {}
    for wi, si in sorted(word_seg.items()):
        if si in first_last:
            first_last[si][1] = wi
        else:
            first_last[si] = [wi, wi]

    blocks: list[list] = []
    for si in sorted(first_last):
        a, b = first_last[si]
        t0, t1 = anchors[si]["start"], anchors[si]["end"]
        if blocks:
            pa, pb, _pt0, pt1 = blocks[-1]
            if a <= pb or t0 < pt1:
                t0 = max(t0, pt1)
                a = max(a, pb + 1)
        if a > b or t1 <= t0:
            continue
        blocks.append([a, b, t0, t1])
    if not blocks:
        return {}, []

    times: dict[int, tuple[float, float]] = {}

    def spread(lo, hi, t0, t1):
        """Lay words lo..hi end to end across [t0, t1], in proportion to length."""
        if hi < lo:
            return
        span = cost[hi + 1] - cost[lo]
        if span <= 0 or t1 <= t0:
            for wi in range(lo, hi + 1):
                times[wi] = (round(t0, 3), round(t0, 3))
            return
        for wi in range(lo, hi + 1):
            s = t0 + (t1 - t0) * (cost[wi] - cost[lo]) / span
            e = t0 + (t1 - t0) * (cost[wi + 1] - cost[lo]) / span
            times[wi] = (round(s, 3), round(e, 3))

    # How fast this episode is actually spoken, measured over everything the
    # transcription did hear. Used only to give the words it did NOT hear a
    # plausible duration.
    heard_c = sum(cost[b + 1] - cost[a] for a, b, _t0, _t1 in blocks)
    heard_t = sum(t1 - t0 for _a, _b, t0, t1 in blocks)
    rate = heard_c / heard_t if heard_t > 0 else 16.0

    # Segments the script never claimed. The transcription misheard them, so they
    # name no word — but a segment exists because something was SAID then, and that
    # is exactly what is missing inside a gap.
    claimed = set(word_seg.values())
    heard_elsewhere = sorted((a["start"], a["end"])
                             for si, a in enumerate(anchors) if si not in claimed)

    def fill_gap(lo, hi, t0, t1):
        """Place words the transcription never heard, in the silence it left them.

        These are the only words with no measurement of their own, so the question
        is not where they are but how to be least wrong about it. Three things are
        known, and using all three is the difference between a line landing on its
        cue and landing three seconds off.

        Their natural duration, so they are not stretched to fill the silence: a
        missed "Oh!" is a fifth of a second whether the pause around it is one
        second or six.

        Where the script's own line breaks fall. A gap that spans one holds two
        separate pieces of speech, not one run — the tail of a line that ran on
        past what was heard, then a pause, then the head of the next. Placed as one
        run they drag each other; split, each goes where its own line says.

        And where speech actually happened. A segment the script never claimed is
        still a measurement that SOMETHING was said at that moment — "He" and "was"
        for a misheard "Here you are". Filling a gap uniformly ignores that and
        spreads words across theme music; the words belong where the sound is.

        Runs are laid down in order and never allowed to overlap what came before.
        """
        runs, start = [], lo
        for i in range(lo + 1, hi + 1):
            if words[i][1] != words[i - 1][1]:
                runs.append((start, i - 1))
                start = i
        runs.append((start, hi))
        needs = [(cost[b + 1] - cost[a]) / rate for a, b in runs]

        # A run carrying on the line before it is not adrift at all: that speech
        # was measured, and this is the rest of it, so it starts where the
        # measurement stopped. It is placed first and takes no part in the guessing
        # below — otherwise "This week, Douz!" gets dragged forward to the sound of
        # the line after it, six seconds into the theme music.
        at = t0
        if runs[0][0] > 0 and words[runs[0][0]][1] == words[runs[0][0] - 1][1]:
            a, b = runs[0]
            at = min(t1, t0 + needs[0])
            spread(a, b, t0, at)
            runs, needs = runs[1:], needs[1:]
        if not runs:
            return

        # What is left goes in a window: normally the rest of the gap, but where
        # the transcription heard something it could not name, the sound itself —
        # a segment nobody claimed still says speech happened at that moment.
        w0, w1 = at, t1
        spoken = [(x, y) for x, y in heard_elsewhere if x >= at - 0.01 and y <= t1 + 0.01]
        if spoken:
            mid = (min(x for x, _ in spoken) + max(y for _, y in spoken)) / 2
            half = max(sum(needs),
                       max(y for _, y in spoken) - min(x for x, _ in spoken)) / 2
            w0, w1 = max(at, mid - half), min(t1, mid + half)

        # Slack is shared out ONCE across every junction that could hold a pause —
        # before the first run, between runs, and after the last unless the line
        # runs straight on into speech that WAS heard. Centring each run in the
        # whole gap separately is what pushed a radio call two seconds late while
        # the line after it waited: each had budgeted the other's room as silence.
        tail = not (runs[-1][1] + 1 < len(words)
                    and words[runs[-1][1]][1] == words[runs[-1][1] + 1][1])
        junctions = len(runs) + (1 if tail else 0)
        pad = max(0.0, (w1 - w0) - sum(needs)) / junctions if junctions else 0.0

        at = max(at, w0 + pad)
        for (a, b), need in zip(runs, needs):
            spread(a, b, at, min(t1, at + need))
            at = min(t1, at + need) + pad

    prev_end_w, prev_end_t = -1, 0.0
    for a, b, t0, t1 in blocks:
        if a > prev_end_w + 1:
            fill_gap(prev_end_w + 1, a - 1, prev_end_t, t0)
        spread(a, b, t0, t1)
        prev_end_w, prev_end_t = b, t1
    if prev_end_w < len(words) - 1:
        # Nothing measured past here, so the last block's own pace is carried on.
        tail = (cost[len(words)] - cost[prev_end_w + 1]) / rate
        spread(prev_end_w + 1, len(words) - 1, prev_end_t, prev_end_t + tail)
    return times, blocks


def group_by_anchor(words, anchors, tok2word, toks):
    """Assign every script word to an utterance, using a transcription's segment
    boundaries as the anchors.

    Walking the audio blind does not work: a forced aligner spreads whatever text
    it is given across whatever audio it is given, so nothing in the result reveals
    that a window advanced further through the audio than through the script, and
    the two drift apart until the audio runs out.  A transcription is wrong about
    plenty of WORDS but roughly right about WHEN, and that is all that is needed —
    the words come from the script, only the boundaries come from here.

    Returns [(start, end, [word indices]), ...] covering every script word.
    """
    word_seg = map_words_to_anchors(words, anchors, positional=True)

    # Merge anchor segments into utterances long enough to align well and short
    # enough to align reliably — the aligner is accurate on a sentence and drifts
    # over a paragraph.
    groups, cur = [], []
    for si, a in enumerate(anchors):
        if cur and a["end"] - anchors[cur[0]]["start"] > UTTERANCE_S:
            groups.append(cur)
            cur = []
        cur.append(si)
    if cur:
        groups.append(cur)
    seg_group = {si: gi for gi, g in enumerate(groups) for si in g}

    # Every script word joins the utterance its anchor points at; one the
    # transcription never heard rides with the words before it, which is where the
    # script says it is.
    out = [[] for _ in groups]
    cur_g = 0
    for wi in range(len(words)):
        if wi in word_seg:
            cur_g = max(cur_g, seg_group[word_seg[wi]])
        out[cur_g].append(wi)
    return [(anchors[g[0]]["start"], anchors[g[-1]]["end"], ws)
            for g, ws in zip(groups, out) if ws]


def align_anchored(mp3: Path, words, anchors, work: Path, verbose: bool) -> dict:
    """Align each utterance's own words inside its own slice of audio.

    Every utterance is independent, so a bad one spoils only itself — there is no
    cursor to lose and nothing downstream of it to drag out of step.  MFA aligns
    the whole set in one pass, in parallel.
    """
    toks, tok2word = [], []
    for wi, (raw, _l) in enumerate(words):
        for t in lab_tokens(raw):
            toks.append(t)
            tok2word.append(wi)
    groups = group_by_anchor(words, anchors, tok2word, toks)
    duration = audio_duration(mp3)
    corpus = work / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)

    def cut(groups, work, tag):
        """Write one MFA corpus for these utterances; return {name: (offset, words)}."""
        index = {}
        for gi, (gs, ge, wis) in enumerate(groups):
            a, b = max(0.0, gs - PAD_S), min(duration, ge + PAD_S)
            gtoks, gmap = [], []
            for wi in wis:
                for t in lab_tokens(words[wi][0]):
                    gtoks.append(t)
                    gmap.append(wi)
            if not gtoks or b - a < 0.2:
                continue
            name = f"{tag}{gi:05d}"
            cdir = work / f"corpus_{tag}"
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / f"{name}.lab").write_text(" ".join(gtoks) + "\n", encoding='utf-8')
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-i", str(mp3),
                            "-t", f"{b - a:.3f}", "-ac", "1", "-ar", str(SAMPLE_RATE),
                            "-c:a", "pcm_s16le", str(cdir / f"{name}.wav")], check=True)
            index[name] = (a, gmap, gi)
        return index

    def align_corpus(tag):
        out = work / f"out_{tag}"
        r = subprocess.run(["mfa", "align", str(work / f"corpus_{tag}"), DICTIONARY,
                            ACOUSTIC, str(out), "--g2p_model_path", G2P, "--clean",
                            "--output_format", "long_textgrid"],
                           capture_output=True, text=True)
        if not out.exists():
            print("   ", (r.stderr or r.stdout).strip()[-300:], flush=True)
        return out

    times: dict[int, list] = {}

    def harvest(index, out):
        """Read back whatever aligned; returns the group indices that did not."""
        missed = set()
        for name, (off, gmap, gi) in index.items():
            tg = next(out.rglob(f"{name}.TextGrid"), None) if out.exists() else None
            if not tg:
                missed.add(gi)
                continue
            for i, (st, en, _lab) in enumerate(read_textgrid_words(tg)):
                if i >= len(gmap):
                    break
                wi = gmap[i]
                if wi in times:
                    times[wi][1] = max(times[wi][1], off + en)
                else:
                    times[wi] = [off + st, off + en]
        return missed

    if verbose:
        print(f"  {len(groups)} utterances (median "
              f"{duration / max(1, len(groups)):.1f}s), aligning as one corpus",
              flush=True)
    idx = cut(groups, work, "u")
    missed = harvest(idx, align_corpus("u"))

    # An utterance that refuses is usually one that is too densely packed for its
    # audio — singing, or two people at once. Halving it gives the aligner a shorter
    # run to find a path through, and costs one extra pass over a handful of files.
    for depth in range(RETRY_SPLITS):
        if not missed:
            break
        smaller = []
        for gi in sorted(missed):
            gs, ge, wis = groups[gi]
            if len(wis) < 4:
                continue
            mid_t, mid_w = (gs + ge) / 2, len(wis) // 2
            smaller.append((gs, mid_t, wis[:mid_w]))
            smaller.append((mid_t, ge, wis[mid_w:]))
        if not smaller:
            break
        if verbose:
            print(f"  retrying {len(missed)} refused utterance(s) as "
                  f"{len(smaller)} halves", flush=True)
        groups = smaller
        idx = cut(groups, work, f"r{depth}")
        missed = harvest(idx, align_corpus(f"r{depth}"))
    if verbose and missed:
        print(f"  {len(missed)} utterance(s) still unaligned", flush=True)
    return {wi: tuple(v) for wi, v in times.items()}


def mfa_refine(mp3: Path, words, anchors, keep: bool, validate_only: bool):
    """Run MFA over the anchored utterances; returns its word times, or {}.

    This is a REFINEMENT and is allowed to fail. Every word already has a time from
    the transcription's clock, so an utterance MFA refuses — singing, or two voices
    at once, which it has never aligned on this corpus — simply keeps the time it
    already had instead of leaving a hole. That is the difference between the
    aligner being an improvement and the aligner being a dependency.
    """
    work = Path(tempfile.mkdtemp(prefix="align_"))
    corpus = work / "corpus"
    try:
        build_corpus(mp3, Path(_find_markdown_for_audio(str(mp3))), corpus)

        # --ignore_acoustics: validate otherwise TRAINS a throwaway model to prove
        # the corpus is alignable, and a single 28-minute utterance gives it nothing
        # to count (it divides by zero). The dictionary check is the part worth
        # having here — it is what produces the out-of-dictionary list.
        v = subprocess.run(["mfa", "validate", str(corpus), DICTIONARY, ACOUSTIC,
                            "--g2p_model_path", G2P, "--single_speaker", "--clean",
                            "--ignore_acoustics"],
                           capture_output=True, text=True)
        oov_file = next(corpus.parent.rglob("oovs_found*.txt"), None) or \
                   next(Path(os.path.expanduser("~/Documents/MFA")).rglob("oovs_found*.txt"), None)
        if oov_file and oov_file.exists():
            oov = [w for w in oov_file.read_text().split() if w]
            print(f"  out-of-dictionary words ({len(oov)}): {', '.join(oov[:20])}"
                  + (" ..." if len(oov) > 20 else ""))
        if v.returncode != 0:
            print("  validate reported a problem:", flush=True)
            print("   ", (v.stderr or v.stdout).strip().splitlines()[-1][:200])
        if validate_only:
            return {}
        return align_anchored(mp3, words, anchors, work, verbose=True)
    finally:
        if keep:
            print(f"  work kept at {work}", flush=True)
        else:
            shutil.rmtree(work, ignore_errors=True)


def run(mp3_path: str, anchor_path: str, validate_only: bool, keep: bool,
        use_mfa: bool) -> int:
    mp3 = Path(mp3_path).resolve()
    md = _find_markdown_for_audio(str(mp3))
    if not md:
        print(f"  no markdown script next to {mp3.name}"); return 1
    words, lines, dirs, inline = script_stream(md)
    print(f"  script: {len(words)} words over {len(lines)} lines", flush=True)

    anchors = load_anchors(Path(anchor_path))
    if not anchors:
        print(f"  no usable segments in {Path(anchor_path).name}"); return 1
    times_by_word, blocks = times_from_anchors(words, anchors)
    if not times_by_word:
        print("  the script and that transcription do not match at all"); return 1

    # Everything worth knowing about this timing, printed rather than assumed.
    # A block is a stretch the transcription measured and the script matched: its
    # two ends are known, the words inside are estimated, and the estimate is
    # thrown away at the far end. So the longest block bounds the error and nothing
    # accumulates past it. The gaps are the honest weak spot — speech the
    # transcription never heard, which no amount of arithmetic can place exactly.
    heard = sum(b - a + 1 for a, b, _t0, _t1 in blocks)
    lens = sorted(t1 - t0 for _a, _b, t0, t1 in blocks)
    gaps = [(a - prev - 1, t0 - pt) for (prev, pt), (a, t0)
            in zip([(-1, 0.0)] + [(b, t1) for _a, b, _t0, t1 in blocks],
                   [(a, t0) for a, _b, t0, _t1 in blocks]) if a > prev + 1]
    print(f"  measured {heard}/{len(words)} words ({100 * heard / len(words):.1f}%) "
          f"in {len(blocks)} blocks: median {lens[len(lens) // 2]:.2f}s, "
          f"longest {lens[-1]:.2f}s", flush=True)
    if gaps:
        print(f"  {len(gaps)} gaps the transcription did not hear: "
              f"{sum(g[0] for g in gaps)} words over {sum(g[1] for g in gaps):.0f}s"
              f" (longest {max(g[1] for g in gaps):.1f}s)", flush=True)

    if use_mfa or validate_only:
        refined = mfa_refine(mp3, words, anchors, keep, validate_only)
        if validate_only:
            return 0
        print(f"  MFA refined {len(refined)}/{len(words)} words "
              f"({len(words) - len(refined)} keep their anchor time)", flush=True)
        times_by_word.update(refined)

    segs, stats = to_segments_direct(words, lines, dirs, inline, times_by_word)
    dest = mp3.with_suffix(".json")
    dest.write_text(json.dumps({"segments": segs}, ensure_ascii=False, indent=1),
                    encoding='utf-8')
    print(f"  {stats['words_timed']}/{stats['script_words']} words timed into "
          f"{stats['segments']} segments ({stats['stage_dirs']} stage directions)",
          flush=True)
    print(f"  wrote {dest.name}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", nargs="+")
    ap.add_argument("--anchors", metavar="JSON",
                    help="the transcription supplying segment boundaries; "
                         "defaults to <audio stem>.whisper.json beside the audio, "
                         "or timings/<audio stem>.json")
    ap.add_argument("--mfa", action="store_true",
                    help="refine the anchor times to the phoneme with Montreal "
                         "Forced Aligner (minutes per episode, needs the models); "
                         "without it the transcription's own clock is used, which "
                         "is already drift-free and takes about a second")
    ap.add_argument("--validate-only", action="store_true",
                    help="report out-of-dictionary words and stop")
    ap.add_argument("--keep", action="store_true", help="keep the MFA work directory")
    args = ap.parse_args()
    rc = 0
    for a in args.audio:
        print(Path(a).name, flush=True)
        cand = ([Path(args.anchors)] if args.anchors else
                [Path(a).with_suffix("").with_suffix(".whisper.json"),
                 Path(a).parent / "timings" / (Path(a).stem + ".json"),
                 Path(a).with_suffix("").with_name(Path(a).stem + ".whisper.json")])
        anchors = next((str(c) for c in cand if c.exists()), None)
        if not anchors:
            print(f"  no anchor transcript found (looked for "
                  f"{', '.join(str(c.name) for c in cand)}) — see --anchors", flush=True)
            rc |= 1
            continue
        rc |= run(a, anchors, args.validate_only, args.keep, args.mfa)
    return rc


if __name__ == "__main__":
    sys.exit(main())
