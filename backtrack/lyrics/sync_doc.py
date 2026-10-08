"""The lyrics editor's document: finding and loading a track's timed lyrics, the
working copy (.sync.json) that keeps segment ids, and the per-segment checks the review phases use."""
from __future__ import annotations
from backtrack.id3.tag_formats import load_id3
import os, json
from backtrack import tuning as tune
from mutagen.id3 import ID3NoHeaderError
from backtrack.lyrics.formats import _find_timing_files_for_audio
from backtrack.lyrics.md_overlay import _SD_SCOPES
from backbone import timefmt


SOURCE_TRANSCRIPT = 'transcript'


SOURCE_SYLT       = 'sylt'


SOURCE_USLT       = 'uslt'


def _find_transcript(mp3_path: str) -> str | None:
    """The track's timed transcript: the same file the player reads
    (lyrics._find_timing_files_for_audio), so the two never disagree."""
    return _find_timing_files_for_audio(mp3_path)[1]


def _sidecar_path(jpath: str) -> str:
    """The working copy (.sync.json) beside the transcript.  Edits autosave here and
    are only written back to the transcript when the user commits."""
    return jpath[:-5] + ".sync.json" if jpath.endswith(".json") else jpath + ".sync.json"


def _file_fp(path: str) -> str:
    """Cheap content fingerprint (size + short hash) used to detect that the
    original transcript.json changed under the working copy (external edit)."""
    try:
        import hashlib
        with open(path, "rb") as f:
            data = f.read()
        return f"{len(data)}:{hashlib.md5(data).hexdigest()[:16]}"
    except OSError:
        return ""


def _ensure_ids(segs: list, meta: dict) -> None:
    """Assign immutable ids to any block/word that lacks one.

    `wid` (word) is the atomic, permanent identity: splitting a segment just
    partitions its word list and joining concatenates it, so wids are never
    reassigned and the JSON↔MD alignment they carry survives any mutation.
    `bid` identifies word-less blocks (stage directions / dead air).  The `meta`
    counters guarantee ids are never reused, even across reload.
    """
    nw = meta.get("next_wid", 0)
    nb = meta.get("next_bid", 0)
    for seg in segs:
        if seg.get("kind") in ("stage_dir", "dead_air") and "bid" not in seg:
            seg["bid"] = nb; nb += 1
        for w in seg.get("words", []):
            if "wid" not in w:
                w["wid"] = nw; nw += 1
    meta["next_wid"] = nw
    meta["next_bid"] = nb


# Fields the editor adds for its own bookkeeping, stripped when committing so the
# transcript.json the user chose to keep clean stays in its original schema.
_SIDECAR_FIELDS = ("wid", "bid", "line_ref", "_md_text")


def _clean_seg(seg: dict) -> dict:
    """Deep-ish copy of a seg with editor-only fields removed (for the Whisper
    export): drops the working-copy bookkeeping AND `kind`, leaving a plain Whisper
    segment (start/end/text/avg_logprob/words)."""
    _strip = _SIDECAR_FIELDS + ("kind", "words")
    out = {k: v for k, v in seg.items() if k not in _strip}
    if "words" in seg:
        out["words"] = [{k: v for k, v in w.items() if k not in _SIDECAR_FIELDS}
                        for w in seg["words"]]
    return out


def _make_stage_dir(text: str, start: float | None = None, end: float | None = None) -> dict:
    """Build a stage-direction segment, tagging `_md_text` so it stays reconciled
    against the MD overlay even after the visible `text` is relabelled."""
    # `_md_text` is the immutable MD-derived text used to reconcile this seg
    # against the overlay.  It survives relabelling the visible `text` (via 'l'),
    # so a committed direction keeps claiming its MD event and never re-appears as
    # a ghost overlay.  It is persisted to JSON, so the claim survives reload too.
    return {"kind": "stage_dir", "text": text, "_md_text": text,
            "start": round(start, 3) if start is not None else None,
            "end":   round(end,   3) if end   is not None else None,
            "words": []}


# ── Review mode: walk each flagged line so things get fixed without scrolling ──
# Phases run in order; each is recomputed live, so fixing a line drops it and
# stopping/saving/re-entering simply resumes on whatever is still outstanding.
# Three programs: 'issues' (R), 'dirs' (D) and 'long' (L).
_REVIEW_PROGRAMS = {
    'issues': ('md', 'words', 'overlaps'),
    'dirs':   ('uncat', 'untimed'),
    'long':   ('long',),
}


_REVIEW_PHASE_NAME = {
    'md': 'MD mismatches', 'words': 'word timings', 'overlaps': 'overlaps',
    'uncat': 'uncategorised directions', 'untimed': 'untimed directions',
    'long': 'long lines',
}


def _seg_overlap(segs: list, si: int) -> bool:
    """The seg starts before the previous one ends (the ⚠ overlap flag)."""
    if si <= 0:
        return False
    s = segs[si].get('start')
    pe = segs[si - 1].get('end')
    return s is not None and pe is not None and s < pe


def _seg_word_timing_error(seg: dict) -> bool:
    """A word inside the line ends after the next word starts (the ⚠ words flag)."""
    if seg.get('kind') in ('dead_air', 'stage_dir'):
        return False
    ws = seg.get('words') or []
    return any(ws[i].get('end') is not None and ws[i + 1].get('start') is not None
               and ws[i]['end'] > ws[i + 1]['start'] for i in range(len(ws) - 1))


def _seg_md_error(si: int, md_quality: dict | None) -> bool:
    """The transcript text doesn't match the MD script for this seg (≈/? md)."""
    if not md_quality:
        return False
    mq = md_quality.get(si)
    return bool(mq) and mq.get('score', 1.0) < 0.75


def _seg_uncategorised(seg: dict) -> bool:
    """A stage direction with no explicit kind chosen yet (still on the default)."""
    return seg.get('kind') == 'stage_dir' and seg.get('scope') not in _SD_SCOPES


def _seg_untimed_dir(seg: dict) -> bool:
    """A stage direction with no start time set."""
    return seg.get('kind') == 'stage_dir' and seg.get('start') is None


_LONG_LINE_CHARS = 64   # a spoken line longer than this reads better split


def _seg_long(seg: dict, limit: int = _LONG_LINE_CHARS) -> bool:
    """A spoken line long enough to be worth splitting (and splittable: ≥2 words)."""
    if seg.get('kind') in ('dead_air', 'stage_dir'):
        return False
    return len((seg.get('text') or '').strip()) > limit and len(seg.get('words') or []) >= 2


# Punctuation ranked by how strong a semantic break it makes (for auto-splitting).
def _split_rank(word: str) -> int:
    w = (word or '').strip().rstrip('*"”’\')]}')
    if w.endswith(('.', '!', '?', '…')):      return 4   # sentence end
    if w.endswith((';', ':')):                return 3   # clause break
    if w.endswith(('—', '–')) or w.endswith('--'): return 2   # dash aside
    if w.endswith(','):                       return 1   # comma
    return 0


def _best_split_index(words: list, text_toks: list | None = None) -> int:
    """The word index (1..len-1) to split at: PRIORITISE a real punctuation break
    (strongest, then nearest the middle); only fall back to the middle when the
    line has none.  Word timing tokens usually carry no punctuation
    (it's only in the segment text), so pass the text's whitespace tokens when
    they line up 1:1, since that's where the commas/periods actually are."""
    n = len(words)
    toks = (text_toks if (text_toks and len(text_toks) == n)
            else [w.get('word', '') for w in words])
    mid = n / 2
    best_i, best_key = None, None
    for i in range(1, n):
        rank = _split_rank(toks[i - 1])
        if rank == 0:
            continue
        key = (rank, -abs(i - mid))            # strongest break, then closest to centre
        if best_key is None or key > best_key:
            best_key, best_i = key, i
    return best_i if best_i is not None else max(1, round(mid))


def _review_phase_issues(segs: list, phase: str, md_quality: dict | None) -> list[int]:
    """The seg indices flagged for a review phase, in document order."""
    n = len(segs)
    if phase == 'md':
        return [si for si in range(n) if _seg_md_error(si, md_quality)]
    if phase == 'words':
        return [si for si in range(n) if _seg_word_timing_error(segs[si])]
    if phase == 'overlaps':
        return [si for si in range(n) if _seg_overlap(segs, si)]
    if phase == 'uncat':
        return [si for si in range(n) if _seg_uncategorised(segs[si])]
    if phase == 'untimed':
        return [si for si in range(n) if _seg_untimed_dir(segs[si])]
    if phase == 'long':
        return [si for si in range(n) if _seg_long(segs[si])]
    return []


def _rebuild_srt(segs: list) -> str:
    """Render segs as SRT text, wrapping stage-direction lines in *(...)* and skipping untexted dead-air blocks."""
    blocks = []
    for i, seg in enumerate(segs, 1):
        _kind = seg.get("kind")
        txt   = seg.get("text", "").strip()
        s     = seg.get("start")
        e     = seg.get("end")
        if _kind == 'stage_dir' and txt:
            txt = f"*({txt})*"
        if _kind == 'dead_air' and not txt:
            continue  # pure silence: no SRT block
        if txt and s is not None:
            blocks.append(f"{i}\n{timefmt.srt(s)} --> {timefmt.srt(e if e is not None else s)}\n{txt}\n")
    return "\n".join(blocks)


def find_lyrics(mp3_path: str) -> str | None:
    """Return source key if any lyrics data exists, else None."""
    if _find_transcript(mp3_path):
        return SOURCE_TRANSCRIPT
    try:
        audio = load_id3(mp3_path)
        if audio.getall('SYLT'): return SOURCE_SYLT
        if audio.getall('USLT'): return SOURCE_USLT
    except (OSError, ID3NoHeaderError):
        pass
    return None


def _load(mp3_path: str) -> tuple[list, str, dict] | None:
    """Load lyrics data. Returns (segs, source, aux) or None."""
    jpath = _find_transcript(mp3_path)
    if jpath:
        sc = _sidecar_path(jpath)
        if os.path.isfile(sc):
            # Resume from the working copy (already carries ids + alignment).
            with open(sc, encoding='utf-8') as f:
                sdata = json.load(f)
            segs = sdata['segments']
            meta = sdata.get('meta', {})
            _ensure_ids(segs, meta)   # ids for anything hand-added since last save
            # Detect that transcript.json changed under us since this working copy was
            # written (external edit / commit elsewhere): warn, don't clobber.
            _fp_now  = _file_fp(jpath)
            _drift   = bool(meta.get('source_fp')) and _fp_now != meta['source_fp']
            return segs, SOURCE_TRANSCRIPT, {'jpath': jpath, 'sidecar': sc,
                                             'meta': meta, 'mp3': mp3_path,
                                             'from_sidecar': True, 'drift': _drift}
        with open(jpath, encoding='utf-8') as f:
            data = json.load(f)
        segs = data['segments']
        meta: dict = {'source_fp': _file_fp(jpath)}
        _ensure_ids(segs, meta)       # bootstrap ids on first open
        return segs, SOURCE_TRANSCRIPT, {'jpath': jpath, 'sidecar': sc, 'data': data,
                                         'meta': meta, 'mp3': mp3_path,
                                         'from_sidecar': False, 'drift': False}
    try:
        from backtrack.lyrics.formats import normalize_lyric_newlines
        audio = load_id3(mp3_path)

        sylt = audio.getall('SYLT')
        if sylt:
            import re as _re
            entries = sylt[0].text  # [(text, ms), ...]
            segs = []
            for i, (text, start_ms) in enumerate(entries):
                end_ms = (entries[i + 1][1] if i + 1 < len(entries)
                          else start_ms + tune.LYRIC_FABRICATED_END_MS)
                # Reconstruct the stage_dir marker that do_save wraps as *(...)*
                # so the overlay's reconciliation recognises it as materialised
                # (otherwise it round-trips as a plain seg and re-duplicates).
                _m  = _re.match(r'^\*\((.*)\)\*$', text.strip())
                seg = {'text': _m.group(1) if _m else text,
                       'start': round(start_ms / 1000.0, 3),
                       'end':   round(end_ms   / 1000.0, 3),
                       'words': []}
                if _m:
                    seg['kind']     = 'stage_dir'
                    seg['_md_text'] = _m.group(1)  # stable reconciliation key
                segs.append(seg)
            # The frame edited: saving replaces just this one, so other SYLT
            # frames (other languages / descriptions) survive.
            return segs, SOURCE_SYLT, {'mp3': mp3_path, 'desc': sylt[0].desc, 'lang': sylt[0].lang}

        uslt = audio.getall('USLT')
        if uslt:
            raw   = normalize_lyric_newlines(uslt[0].text)
            lines = [line.strip() for line in raw.split('\n') if line.strip()]
            segs  = [{'text': line, 'start': None, 'end': None, 'words': []} for line in lines]
            return segs, SOURCE_USLT, {'mp3': mp3_path}
    except (OSError, KeyError, ID3NoHeaderError):  # type: ignore[reportPrivateImportUsage]
        pass
    return None


def _shift_seg(seg: dict, delta: float) -> None:
    """Shift a segment's start/end and all its word timings by delta seconds, in place."""
    if seg.get("start") is not None: seg["start"] = round(seg["start"] + delta, 3)
    if seg.get("end")   is not None: seg["end"]   = round(seg["end"]   + delta, 3)
    for w in seg.get("words", []):
        if w.get("start") is not None: w["start"] = round(w["start"] + delta, 3)
        if w.get("end")   is not None: w["end"]   = round(w["end"]   + delta, 3)


def _shift_word(word: dict, delta: float) -> None:
    """Shift a single word's start/end by delta seconds, in place."""
    if word.get("start") is not None: word["start"] = round(word["start"] + delta, 3)
    if word.get("end")   is not None: word["end"]   = round(word["end"]   + delta, 3)
