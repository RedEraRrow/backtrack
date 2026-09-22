"""Lossless MP3 trim engine: frame probing, the ffmpeg stream copy, tag copy,
provenance. No terminal output — see trim_editor.py / trim_bulk.py for the UI.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from mutagen.id3 import ID3, ID3NoHeaderError, TXXX, TIT2, CHAP, CTOC  # type: ignore[reportPrivateImportUsage]
from mutagen.mp3 import MP3

from src.config import load_config, CONFIG_DIR
from src.id3 import tag_writer as tw
from src.id3.id3_tag_handler import save_id3
from src.music_library import refresh_library_entry

_EPS = 1e-9


def _resolve_ffmpeg() -> str | None:
    """`trim_ffmpeg_path` from config if set, else whatever's on PATH."""
    override = (load_config().get("trim_ffmpeg_path") or "").strip()
    if override:
        return override if os.path.exists(override) else None
    return shutil.which("ffmpeg")


FFMPEG_PATH = _resolve_ffmpeg()
HAS_FFMPEG = bool(FFMPEG_PATH)


def probe_frame_duration(path: str) -> float:
    """Seconds per MPEG audio frame — the engine's snap/nudge granularity.

    Layer III is 1152 samples/frame at MPEG-1 rates, 576 at MPEG-2 LSF rates
    (mutagen's ``version`` is 1 or 2). Read from the file rather than assumed,
    since the target material spans four sample rates (section 2.2).
    """
    try:
        info = MP3(path).info
    except Exception as e:
        raise ValueError(f"Could not read MPEG frame info: {e}") from e
    samples_per_frame = 1152 if info.version == 1 else 576  # type: ignore[reportAttributeAccessIssue]
    return samples_per_frame / info.sample_rate  # type: ignore[reportAttributeAccessIssue]


# ---------------------------------------------------------------------------
# Sting matching (section 4.4): locate a shared opening/closing sting across
# a series by shape, not samples — off-air captures of the same sting are
# never byte- or even level-similar (different broadcast chain, per-episode
# level differences, compression, noise). A normalised RMS-envelope
# correlation is the spec's own "legitimate first implementation"; swapping
# it for a spectral match later is a one-line change behind `find_sting`.
# ---------------------------------------------------------------------------

def decode_mono_pcm(path: str, start_s: float, dur_s: float, rate: int = 8000) -> np.ndarray:
    """Decode a window of `path` to mono float32 PCM at `rate` Hz — low
    enough that a sting's identity (in the low/mid bands) survives, cheap
    enough to run per candidate track. Empty array on failure."""
    if not HAS_FFMPEG or FFMPEG_PATH is None:
        return np.zeros(0, dtype=np.float32)
    cmd = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", f"{max(0.0, start_s):.6f}", "-t", f"{max(0.01, dur_s):.6f}", "-i", path,
           "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _envelope(pcm: np.ndarray, hop: int) -> np.ndarray:
    """Log-RMS envelope, one value per `hop` samples, normalised by median and
    MAD (not mean/std) so level differences between recordings cancel — a
    quiet copy and a loud copy of the same sting produce nearly the same
    envelope after this. Median/MAD rather than mean/std specifically because
    a target window often isn't all programme material: a stretch of near-
    silence right before the sting has an outsized log-RMS outlier that skews
    a mean/std normalisation enough to break the match; the median resists it."""
    n = len(pcm) // hop
    if n == 0:
        return np.zeros(0)
    frames = pcm[: n * hop].reshape(n, hop).astype(np.float64)
    log_rms = np.log(np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-6)
    med = np.median(log_rms)
    mad = np.median(np.abs(log_rms - med)) or 1e-9
    return (log_rms - med) / (mad * 1.4826)  # 1.4826: MAD -> std-equivalent for a normal distribution


def find_sting(reference_pcm: np.ndarray, target_pcm: np.ndarray,
               rate: int = 8000, hop_ms: float = 10.0) -> tuple[float, float]:
    """Locate `reference_pcm` (the sting clip) inside `target_pcm` (a
    candidate track's head or tail window). Returns (offset_s, score): where
    in the target the sting starts, and how much the best match beats the
    runner-up — a peak that barely beats second place is not a match, so the
    caller should not seed from a low score (section 4.4)."""
    hop = max(1, int(rate * hop_ms / 1000))
    ref = _envelope(reference_pcm, hop)
    tgt = _envelope(target_pcm, hop)
    if len(ref) == 0 or len(tgt) < len(ref):
        return 0.0, 0.0

    fsize = 1 << (len(tgt) + len(ref) - 1).bit_length()
    corr = np.fft.ifft(np.fft.fft(tgt, fsize) * np.fft.fft(ref[::-1], fsize)).real
    valid = corr[len(ref) - 1: len(ref) - 1 + (len(tgt) - len(ref) + 1)]
    if len(valid) == 0:
        return 0.0, 0.0

    peak_i = int(np.argmax(valid))
    peak = valid[peak_i]
    excl = max(1, len(ref) // 2)
    mask = np.ones(len(valid), dtype=bool)
    mask[max(0, peak_i - excl): min(len(valid), peak_i + excl + 1)] = False
    runner_up = float(valid[mask].max()) if mask.any() else 0.0

    score = max(0.0, min(1.0, (float(peak) - runner_up) / len(ref)))
    return peak_i * hop_ms / 1000.0, score


def find_candidate_stings(pcm_a: np.ndarray, pcm_b: np.ndarray, *, rate: int = 8000,
                          candidate_len_s: float = 3.0, hop_s: float = 1.0,
                          top_n: int = 4, min_score: float = 0.15) -> list[dict]:
    """Slide a `candidate_len_s` window across `pcm_a` and score how well each
    position matches somewhere in `pcm_b` (`find_sting`). A high score marks
    a segment the two tracks share — a continuity announcement, the theme, a
    trailer — which is what a sting actually is, so this is how 5.2.1's
    candidate list is built rather than the user hunting blind. Returns up to
    `top_n` non-overlapping candidates as {'start', 'end', 'score'}, best
    first. `min_score` is deliberately lower than `find_sting`'s own seeding
    threshold: this scan searches many more positions, so the true match's
    peak-over-runner-up margin is naturally smaller here."""
    cand_len = int(candidate_len_s * rate)
    hop = max(1, int(hop_s * rate))
    raw: list[tuple[float, float]] = []
    pos = 0
    while pos + cand_len <= len(pcm_a):
        _, score = find_sting(pcm_a[pos: pos + cand_len], pcm_b, rate=rate)
        raw.append((pos / rate, score))
        pos += hop
    raw.sort(key=lambda x: -x[1])

    chosen: list[dict] = []
    for start, score in raw:
        if score < min_score:
            break
        if any(abs(start - c['start']) < candidate_len_s for c in chosen):
            continue
        chosen.append({'start': start, 'end': start + candidate_len_s, 'score': score})
        if len(chosen) >= top_n:
            break
    return chosen


def window_bounds(path: str, window_s: float, region: str) -> tuple[float, float]:
    """(start_s, dur_s) for the head or tail scan window of `path` — the tail
    window is measured back from the file's own end."""
    if region == 'head':
        return 0.0, window_s
    length = MP3(path).info.length
    start = max(0.0, length - window_s)
    return start, max(0.01, length - start)


def seed_by_sting(
    reference_pcm: np.ndarray,
    sting_duration_s: float,
    anchor_edge: str,
    anchor_offset_s: float,
    target_paths: list[str],
    *,
    window_s: float = 90.0,
    min_score: float = 0.3,
    region: str = 'head',
) -> dict[str, tuple[float, float] | None]:
    """For each track in `target_paths`, locate the sting in its head or tail
    window (`region`) and compute the seeded mark from the chosen anchor:
    `anchor_edge` ('start' or 'end' of the matched sting) plus a signed
    `anchor_offset_s` from that edge, default zero. The caller decides
    whether the result is an in-point or an out-point (section 5.2.2) — this
    function only locates the sting and applies the anchor.

    Returns path -> (mark_s, score), or path -> None when the match scored
    below `min_score` — section 4.4 is explicit that a low-scoring match must
    be left unseeded, never seeded wrongly."""
    results: dict[str, tuple[float, float] | None] = {}
    for path in target_paths:
        start_s, dur_s = window_bounds(path, window_s, region)
        target_pcm = decode_mono_pcm(path, start_s, dur_s)
        offset, score = find_sting(reference_pcm, target_pcm)
        if score < min_score:
            results[path] = None
            continue
        edge_pos = start_s + (offset if anchor_edge == 'start' else offset + sting_duration_s)
        results[path] = (max(0.0, edge_pos + anchor_offset_s), score)
    return results


_LEARN_CANDIDATE_LEN_S = 3.0  # matches find_candidate_stings' own default clip length


def learn_sting_from_history(paths: list[str], region: str, window_s: float, min_score: float
                             ) -> tuple[str, float, float, str] | None:
    """Auto-detect a shared sting from an earlier, already-committed trim in
    the same folder (section 4.4's "learn from correctly trimmed tracks"),
    instead of marking or discovering one fresh for every new batch of
    episodes — usable for a bulk group or a lone single-track edit alike,
    since it only needs one track (`paths[0]`) to correlate against. The
    backup still holds the removed audio at its original position, but not
    which side of that boundary the sting itself falls on (kept, just after
    the cut, or dropped, just before it) — resolved here by correlating a
    probe window each way against `paths[0]` and keeping whichever direction
    actually matches. None if there's no usable history, or neither direction
    scores well enough to trust."""
    src = learned_sting_source(os.path.dirname(paths[0]), region, exclude_paths=set(paths))
    if src is None:
        return None
    backup_path = src["backup_path"]
    anchor = src["snapped_in_s"] if region == 'head' else src["snapped_out_s"]

    probe_start, probe_dur = window_bounds(paths[0], window_s, region)
    probe_pcm = decode_mono_pcm(paths[0], probe_start, probe_dur)

    candidates = []
    for start, end, side in (
        (max(0.0, anchor - _LEARN_CANDIDATE_LEN_S), anchor, 'end'),
        (anchor, anchor + _LEARN_CANDIDATE_LEN_S, 'start'),
    ):
        if end - start < 0.5:
            continue
        ref_pcm = decode_mono_pcm(backup_path, start, end - start)
        _, score = find_sting(ref_pcm, probe_pcm)
        candidates.append((score, start, end, side))
    if not candidates:
        return None
    candidates.sort(key=lambda c: -c[0])
    best_score, start, end, side = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
    # Both directions scoring near-equally usually means neither is the real
    # sting — plain lead-in/tail padding can self-match trivially against its
    # own kind elsewhere in the probe. A genuine sting stands out clearly over
    # the wrong-direction guess, not just barely (section 4.4: never seed
    # wrongly — the same reasoning as find_sting's own peak-over-runner-up).
    if best_score < min_score or best_score - runner_up < 0.2:
        return None
    return backup_path, start, end, side


# ---------------------------------------------------------------------------
# Silence detection (section 4.3) — the baseline cut-point suggestion when
# there's no sting to match against. Some material has no silent gap at the
# boundary at all (opens cold on a downbeat); that's not a failure, it just
# means nothing is returned and the caller falls back to manual marking.
# ---------------------------------------------------------------------------

def detect_silence(path: str, start_s: float, dur_s: float,
                   noise_db: float = -32.0, min_s: float = 0.4) -> list[tuple[float, float]]:
    """Silent stretches within [start_s, start_s + dur_s) of `path`, as
    absolute (silence_start, silence_end) pairs in the file's own timeline —
    run over the head or tail window only, never the whole file. A stretch
    still silent at the window's edge is closed there rather than dropped.
    Empty on failure or when ffmpeg is absent."""
    if not HAS_FFMPEG or FFMPEG_PATH is None:
        return []
    cmd = [FFMPEG_PATH, "-hide_banner", "-nostats",
           "-ss", f"{max(0.0, start_s):.6f}", "-t", f"{max(0.01, dur_s):.6f}", "-i", path,
           "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)

    pairs: list[tuple[float, float]] = []
    pending_start: float | None = None
    for line in (proc.stderr or "").splitlines():
        if "silence_start" in line:
            try:
                pending_start = float(line.split("silence_start:")[1].split()[0])
            except (IndexError, ValueError):
                pending_start = None
        elif "silence_end" in line and pending_start is not None:
            try:
                end = float(line.split("silence_end:")[1].split()[0])
            except (IndexError, ValueError):
                pending_start = None
                continue
            pairs.append((start_s + pending_start, start_s + end))
            pending_start = None
    if pending_start is not None:
        pairs.append((start_s + pending_start, start_s + dur_s))
    return pairs


def snap_in_point(requested_s: float, frame_duration: float) -> float:
    """Snap an in-point to the frame boundary at or before it (round earlier,
    so nothing wanted is clipped off the start)."""
    if frame_duration <= 0:
        return max(0.0, requested_s)
    n = max(0, math.floor(requested_s / frame_duration + _EPS))
    return n * frame_duration


def snap_out_point(requested_s: float, frame_duration: float) -> float:
    """Snap an out-point to the frame boundary at or after it (round later,
    so nothing wanted is clipped off the end)."""
    if frame_duration <= 0:
        return requested_s
    n = math.ceil(requested_s / frame_duration - _EPS)
    return n * frame_duration


def build_cut_command(ffmpeg_path: str, src_path: str, out_path: str,
                       in_s: float, duration_s: float) -> list[str]:
    """The exact stream-copy invocation (section 2.1). ``-ss`` is an input
    option (before ``-i``) paired with ``-t`` for duration — never ``-to``,
    which is measured from a different origin once ``-ss`` precedes ``-i`` and
    silently produces the wrong length (section 2.4). ``-write_xing 1``
    rewrites the VBR header for the new length (section 3.4); ``-map_metadata
    -1`` leaves the output with no tags at all, since mutagen owns tagging.
    ``-map 0:a:0`` takes the audio stream only — an embedded cover is its own
    (video) stream in ffmpeg's model, and mutagen re-adds it from the source
    tags afterwards, so it must not be copied here too."""
    return [
        ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{in_s:.6f}", "-i", src_path,
        "-t", f"{duration_s:.6f}",
        "-map", "0:a:0", "-c", "copy", "-map_metadata", "-1", "-write_xing", "1",
        out_path,
    ]


def copy_tags(src_path: str, dst_path: str, orig_length_s: float,
              snapped_in_s: float, snapped_out_s: float) -> None:
    """Copy every ID3 frame from src to dst except TLEN and a non-zero TDLY —
    both go stale the instant the file is cut (section 3.2) — plus a
    provenance ``TXXX:BACKTRACK_TRIM`` recording the original length and the
    snapped cut points (section 3.5). ffmpeg leaves the output with no tags at
    all (``-map_metadata -1``); this is what puts them back, saved through
    ``save_id3`` so the v2.3/v2.4 rule holds."""
    src_tags = ID3(src_path)
    try:
        dst = ID3(dst_path)
    except ID3NoHeaderError:
        dst = ID3()

    for frame in src_tags.values():
        if frame.FrameID == 'TLEN':
            continue
        if frame.FrameID == 'TDLY' and frame.text and str(frame.text[0]) != '0':
            continue
        dst.add(frame)

    dst.delall('TXXX:BACKTRACK_TRIM')
    dst.add(TXXX(encoding=3, desc='BACKTRACK_TRIM',
                 text=[f"orig={orig_length_s:.3f};in={snapped_in_s:.3f};out={snapped_out_s:.3f}"]))
    save_id3(dst, dst_path)


# ---------------------------------------------------------------------------
# Chapters (section 3.3). Classification, re-basing and clamping are pure
# list-in/list-out functions over (element_id, start_ms, end_ms, title)
# tuples — no mutagen objects in these signatures, no terminal output, so
# they're testable without a TUI. Reading/writing the CHAP/CTOC frames
# themselves is the only part that touches mutagen.
# ---------------------------------------------------------------------------

def classify_chapter(start_ms: int, end_ms: int, cut_start_ms: int, cut_end_ms: int) -> str:
    """'kept' (entirely inside the trimmed region), 'destroyed' (entirely
    outside it), or 'straddles' a cut boundary."""
    if start_ms >= cut_start_ms and end_ms <= cut_end_ms:
        return 'kept'
    if end_ms <= cut_start_ms or start_ms >= cut_end_ms:
        return 'destroyed'
    return 'straddles'


def classify_chapters(chapters: list[tuple], cut_start_ms: int, cut_end_ms: int) -> list[tuple]:
    """Each chapter tuple with its classification appended:
    (element_id, start_ms, end_ms, title, classification)."""
    return [
        (element_id, start_ms, end_ms, title,
         classify_chapter(start_ms, end_ms, cut_start_ms, cut_end_ms))
        for element_id, start_ms, end_ms, title in chapters
    ]


def rebase_chapter(chapter: tuple, cut_start_ms: int) -> tuple:
    """Shift a surviving chapter's start/end by the snapped in-point. Not
    optional (section 3.3): a surviving chapter with unshifted offsets is
    worse than no chapter at all."""
    element_id, start_ms, end_ms, title = chapter
    return (element_id, start_ms - cut_start_ms, end_ms - cut_start_ms, title)


def clamp_chapter(chapter: tuple, cut_start_ms: int, cut_end_ms: int) -> tuple:
    """Clamp a straddling chapter to the kept region's edge, then rebase, so
    it starts or ends exactly at the cut."""
    element_id, start_ms, end_ms, title = chapter
    new_start = max(start_ms, cut_start_ms)
    new_end = min(end_ms, cut_end_ms)
    return rebase_chapter((element_id, new_start, new_end, title), cut_start_ms)


def rebuild_ctoc_children(child_order: list[str], surviving_ids) -> list[str]:
    """The CTOC's child_element_ids filtered to survivors, preserving order
    and the top-level/ordered flags (those live on the CTOC frame itself,
    untouched by this). Empty means the CTOC should be dropped entirely — a
    kept CTOC with no children is malformed."""
    return [cid for cid in child_order if cid in surviving_ids]


def apply_chapter_policy(path: str, cut_start_s: float, cut_end_s: float,
                         policy: str = 'clamp'
                         ) -> tuple[list[tuple], list[str], int | None] | None:
    """Resolve a cut's chapters by rule rather than by asking.

    The editor asks about every chapter the cut disturbs, which needs a person.
    This is the same resolution driven by one decision instead:

    * ``clamp``  — keep a straddling chapter, pulled to the cut boundary
    * ``drop``   — delete anything the cut disturbs
    * ``keep``   — rebase everything, even a chapter the cut has emptied

    Returns ``(chapters, ctoc_order, flags)``, or None when the file has none.
    `trim_editor.resolve_chapters` delegates here when nothing is affected, so
    the no-question path has one implementation.
    """
    orig_chapters, child_order, flags = read_chapters(path)
    if not orig_chapters:
        return None

    cut_start_ms = int(round(cut_start_s * 1000))
    cut_end_ms = int(round(cut_end_s * 1000))
    survivors: list[tuple] = []
    for eid, start, end, title, cls in classify_chapters(
            orig_chapters, cut_start_ms, cut_end_ms):
        chapter = (eid, start, end, title)
        if cls == 'kept' or policy == 'keep':
            survivors.append(rebase_chapter(chapter, cut_start_ms))
        elif policy == 'drop' or cls == 'destroyed':
            continue                       # the cut removed the audio under it
        else:                              # 'clamp' on a straddling chapter
            survivors.append(clamp_chapter(chapter, cut_start_ms, cut_end_ms))

    surviving_ids = {c[0] for c in survivors}
    new_order = rebuild_ctoc_children(child_order, surviving_ids) \
        if child_order else []
    return survivors, new_order, flags


def read_chapters(path: str) -> tuple[list[tuple], list[str] | None, int | None]:
    """Every CHAP frame as (element_id, start_ms, end_ms, title) tuples, in
    file order, plus the CTOC's child order and flags (None, None if there's
    no CTOC at all). No classification here — that's `classify_chapters`."""
    try:
        audio = ID3(path)
    except ID3NoHeaderError:
        return [], None, None

    chapters = []
    for frame in audio.getall('CHAP'):
        title = ''
        tit2 = frame.sub_frames.get('TIT2')
        if tit2 is not None and getattr(tit2, 'text', None):
            title = str(tit2.text[0])
        chapters.append((frame.element_id, int(frame.start_time), int(frame.end_time), title))

    ctoc_list = audio.getall('CTOC')
    if not ctoc_list:
        return chapters, None, None
    ctoc = ctoc_list[0]
    return chapters, list(ctoc.child_element_ids), int(ctoc.flags)


def write_chapters(path: str, chapters: list[tuple],
                   child_order: list[str] | None, flags: int | None) -> None:
    """Replace every CHAP/CTOC frame on `path` with the given set — the write
    side of section 3.3's decisions. Every existing CHAP/CTOC is removed
    first, so an empty `chapters` (the "discard all" choice) or an empty
    `child_order` (nothing survived to reference) just leaves the file with
    no chapters at all, rather than a malformed CTOC pointing at nothing."""
    try:
        audio = ID3(path)
    except ID3NoHeaderError:
        audio = ID3()
    audio.delall('CHAP')
    audio.delall('CTOC')

    for element_id, start_ms, end_ms, title in chapters:
        sub_frames = [TIT2(encoding=3, text=[title])] if title else []
        audio.add(CHAP(element_id=element_id, start_time=start_ms, end_time=end_ms,
                       start_offset=0xffffffff, end_offset=0xffffffff, sub_frames=sub_frames))

    if chapters and child_order:
        audio.add(CTOC(element_id='toc', flags=flags or 0,
                       child_element_ids=child_order, sub_frames=[]))

    save_id3(audio, path)


@dataclass
class TrimResult:
    ok: bool
    error: str | None = None
    out_path: str | None = None
    snapped_in: float = 0.0
    snapped_out: float = 0.0


def cut_stream(src_path: str, out_path: str, in_s: float, out_s: float) -> TrimResult:
    """Frame-accurate lossless cut: snap in/out to frame boundaries, ffmpeg
    stream-copies the audio between them, then the tags are copied across
    (minus TLEN/TDLY, plus provenance). Writes to ``out_path`` directly —
    staging to a temp name and swapping it over the source is the caller's
    job (the backup/commit sequence, section 6), not the engine's."""
    if not HAS_FFMPEG or FFMPEG_PATH is None:
        return TrimResult(ok=False, error="ffmpeg is not installed")
    if tw.format_kind(src_path) != 'mp3':
        ext = os.path.splitext(src_path)[1] or 'no extension'
        return TrimResult(ok=False, error=f"Not an MP3 ({ext}) — trimming is MP3 only")

    try:
        frame_dur = probe_frame_duration(src_path)
    except ValueError as e:
        return TrimResult(ok=False, error=str(e))

    snapped_in = snap_in_point(in_s, frame_dur)
    snapped_out = snap_out_point(out_s, frame_dur)
    duration = snapped_out - snapped_in
    if duration <= 0:
        return TrimResult(ok=False, error="Out-point is not after the in-point")

    cmd = build_cut_command(FFMPEG_PATH, src_path, out_path, snapped_in, duration)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return TrimResult(ok=False, error=proc.stderr.strip()[-500:] or "ffmpeg failed")

    try:
        orig_length = MP3(src_path).info.length
        copy_tags(src_path, out_path, orig_length_s=orig_length,
                  snapped_in_s=snapped_in, snapped_out_s=snapped_out)
    except Exception as e:
        return TrimResult(ok=False, error=f"Tag copy failed: {e}")

    return TrimResult(ok=True, out_path=out_path, snapped_in=snapped_in, snapped_out=snapped_out)


# ---------------------------------------------------------------------------
# Backups and undo (section 6). Never destroy a source file: the original is
# copied into the store and fsynced before the trimmed file replaces it.
# ---------------------------------------------------------------------------

def _backup_dir() -> Path:
    override = (load_config().get("trim_backup_dir") or "").strip()
    return Path(override) if override else CONFIG_DIR / "trim-backups"


def _manifest_path() -> Path:
    return _backup_dir() / "manifest.json"


def _load_manifest() -> list[dict]:
    """Every backup entry, oldest first (append-only, so file order is time order)."""
    path = _manifest_path()
    if not path.exists():
        return []
    entries = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def _write_manifest(entries: list[dict]) -> None:
    path = _manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def _copy_with_fsync(src: str, dst: Path) -> None:
    """A byte-for-byte copy, fsynced before returning, so a crash right after
    can't leave a truncated backup mistaken for a good one."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        shutil.copyfileobj(fin, fout)
        fout.flush()
        os.fsync(fout.fileno())


def backup_chain(original_path: str) -> list[dict]:
    """Every backup entry for `original_path`, oldest (the true pre-trim
    original) first. A file trimmed more than once has one entry per trim,
    chained by `parent_id` rather than the later ones overwriting the first."""
    path = os.path.abspath(original_path)
    return [e for e in _load_manifest() if e["original_path"] == path]


def _backup_original(path: str, snapped_in: float, snapped_out: float) -> dict:
    """Copy the current (pre-trim) file into the store and record it in the
    manifest, chained onto any earlier backup for the same original path."""
    bdir = _backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    entry_id = uuid.uuid4().hex[:12]
    backup_name = f"{entry_id}.mp3"
    _copy_with_fsync(path, bdir / backup_name)

    chain = backup_chain(path)
    entry = {
        "id": entry_id,
        "original_path": os.path.abspath(path),
        "original_length_s": MP3(path).info.length,
        "snapped_in_s": snapped_in,
        "snapped_out_s": snapped_out,
        "timestamp": time.time(),
        "parent_id": chain[-1]["id"] if chain else None,
        "backup_file": backup_name,
    }
    entries = _load_manifest()
    entries.append(entry)
    _write_manifest(entries)
    return entry


def commit_trim(path: str, in_s: float, out_s: float, library: list | None = None,
                chapters: tuple[list[tuple], list[str] | None, int | None] | None = None) -> TrimResult:
    """Trim `path` in place: cut to a temp file beside the source, verify it,
    back up the original, then swap the temp over the source. Nothing is
    destroyed until the backup copy is confirmed on disk (section 6).

    `chapters`, when given, is the already-resolved (chapters, child_order,
    flags) to write (section 3.3) — the caller has already classified them
    against this cut and asked the user about anything destroyed or
    straddling; this just applies the decision. Without it, chapters pass
    through `cut_stream`'s tag copy unmodified (and unshifted), which is only
    correct for a file that has none."""
    d = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".trim_", suffix=".mp3", dir=d)
    os.close(fd)
    try:
        r = cut_stream(path, tmp_path, in_s, out_s)
        if not r.ok:
            return r

        if chapters is not None:
            try:
                write_chapters(tmp_path, *chapters)
            except Exception as e:
                return TrimResult(ok=False, error=f"Could not write chapters: {e}")

        try:
            MP3(tmp_path)  # verify the written file opens and is a valid MP3
        except Exception as e:
            return TrimResult(ok=False, error=f"Trimmed output failed to verify: {e}")

        try:
            entry = _backup_original(path, r.snapped_in, r.snapped_out)
        except OSError as e:
            return TrimResult(ok=False, error=f"Backup failed, trim aborted: {e}")

        try:
            os.replace(tmp_path, path)
        except OSError as e:
            return TrimResult(ok=False, error=f"Could not write trimmed file: {e}")

        try:
            if library is not None:
                refresh_library_entry(library, path)
        except Exception as e:
            restore_backup(entry["id"])
            return TrimResult(ok=False, error=f"Post-write step failed, restored original: {e}")

        return TrimResult(ok=True, out_path=path, snapped_in=r.snapped_in, snapped_out=r.snapped_out)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def restore_backup(entry_id: str, library: list | None = None) -> TrimResult:
    """Restore the file recorded by `entry_id`, byte for byte, over its
    current location. Restoring the newest entry for a path undoes the most
    recent trim; walk `backup_chain` for the oldest entry to reach the true
    pre-trim original instead."""
    entry = next((e for e in _load_manifest() if e["id"] == entry_id), None)
    if entry is None:
        return TrimResult(ok=False, error="No such backup entry")

    backup_path = _backup_dir() / entry["backup_file"]
    if not backup_path.exists():
        return TrimResult(ok=False, error="Backup file is missing from the store")

    dest = entry["original_path"]
    d = os.path.dirname(dest) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".trimrestore_", suffix=".mp3", dir=d)
    os.close(fd)
    try:
        _copy_with_fsync(str(backup_path), Path(tmp_path))
        os.replace(tmp_path, dest)
    except OSError as e:
        return TrimResult(ok=False, error=f"Restore failed: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    if library is not None:
        try:
            refresh_library_entry(library, dest)
        except Exception:
            pass
    return TrimResult(ok=True, out_path=dest)


def list_backups() -> list[dict]:
    """Every backup entry with its on-disk size, for a prune confirmation
    screen (never pruned automatically — section 6)."""
    bdir = _backup_dir()
    out = []
    for entry in _load_manifest():
        p = bdir / entry["backup_file"]
        out.append({**entry, "size_bytes": p.stat().st_size if p.exists() else 0})
    return out


def learned_sting_source(directory: str, region: str, exclude_paths: set[str] | None = None) -> dict | None:
    """The most recent backup manifest entry for a file in `directory` that
    actually had material cut on `region`'s ('head' or 'tail') side — a real
    prior trim to learn a shared sting from, rather than marking or
    discovering one fresh for every new batch of episodes. The backup still
    holds the removed audio at its original position, so it's a source clip,
    not yet a resolved (start, end): which side of the cut the sting actually
    falls on is for the caller to work out by correlation. None if there's no
    such entry in this folder, or its backup copy has since been pruned."""
    exclude = {os.path.abspath(p) for p in (exclude_paths or ())}
    directory = os.path.abspath(directory)
    best = None
    for entry in _load_manifest():
        if entry["original_path"] in exclude:
            continue
        if os.path.dirname(entry["original_path"]) != directory:
            continue
        cut = entry["snapped_in_s"] if region == 'head' else entry["original_length_s"] - entry["snapped_out_s"]
        if cut < 0.5:
            continue
        backup_path = _backup_dir() / entry["backup_file"]
        if not backup_path.exists():
            continue
        if best is None or entry["timestamp"] > best["timestamp"]:
            best = {**entry, "backup_path": str(backup_path)}
    return best


def prune_backups(entry_ids: set[str]) -> int:
    """Delete the chosen backup files and their manifest entries. Returns the
    count removed. An explicit, user-driven action only — never automatic."""
    bdir = _backup_dir()
    entries = _load_manifest()
    keep, removed = [], 0
    for entry in entries:
        if entry["id"] in entry_ids:
            try:
                (bdir / entry["backup_file"]).unlink(missing_ok=True)
            except OSError:
                pass
            removed += 1
        else:
            keep.append(entry)
    _write_manifest(keep)
    return removed


# ---------------------------------------------------------------------------
# Loudness / ReplayGain (section 5.4). A separate operation from the trim
# itself — it runs after, never before (a pre-trim measurement would include
# the continuity announcement or trailer being cut, which is exactly the loud
# material that would skew it). No audio bytes change: this only measures,
# for a caller to turn into gain tags.
# ---------------------------------------------------------------------------

def measure_loudness(path: str) -> tuple[float, float] | None:
    """(integrated_lufs, true_peak_dbfs) for the whole file, via ffmpeg's
    ebur128 filter. None on failure or when ffmpeg is absent."""
    if not HAS_FFMPEG or FFMPEG_PATH is None:
        return None
    cmd = [FFMPEG_PATH, "-hide_banner", "-nostats", "-i", path,
           "-af", "ebur128=peak=true", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    parts = (proc.stderr or "").rsplit("Summary:", 1)
    if len(parts) < 2:
        return None

    integrated = true_peak = None
    for line in parts[1].splitlines():
        line = line.strip()
        if line.startswith("I:") and "LUFS" in line:
            try:
                integrated = float(line.split(":")[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
        elif line.startswith("Peak:"):
            try:
                true_peak = float(line.split(":")[1].strip().split()[0])
            except (IndexError, ValueError):
                pass
    if integrated is None or true_peak is None:
        return None
    return integrated, true_peak


def measure_track_gain(path: str, target_lufs: float = -18.0) -> dict | None:
    """Measurement and proposed gain for one track (section 5.4.2): a
    speech-led radio target sits lower than a music one, hence -18 LUFS
    rather than the usual -14/-23 defaults elsewhere. None on measurement
    failure — the caller shows nothing rather than a wrong number."""
    measured = measure_loudness(path)
    if measured is None:
        return None
    integrated_lufs, true_peak_dbfs = measured
    gain_db = round(target_lufs - integrated_lufs, 2)
    return {
        'integrated_lufs': integrated_lufs,
        'true_peak_dbfs': true_peak_dbfs,
        'gain_db': gain_db,
        'peak_linear': 10 ** (true_peak_dbfs / 20.0),
        # A gain tag can't clip on its own, but a player applying it will.
        'clips': (true_peak_dbfs + gain_db) > 0.0,
    }
