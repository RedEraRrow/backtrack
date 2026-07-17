"""
Lyrics editor — unified sync and fine-tune tool.

Data sources (auto-detected in order):
  1. Transcript JSON  (Transcript/ .json)  — word-level timing; saves JSON + SRT
  2. SYLT ID3 tag                          — line-level timing; saves SYLT
  3. USLT ID3 tag                          — untimed lyrics; saves SYLT after tap

Modes:
  SEG    browse list; ↑↓ navigate, ←→/,./[] adjust timestamps
  WORD   per-word editing (transcript source only)
  TAP    real-time tap — audio plays, SPACE marks current line's start
  EDIT   type exact timestamp
"""
from __future__ import annotations
import sys, os, json, time, re

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C, MARGIN_V
from src.utils.prompt import (
    _Widget, _read_key, _wait_for_keypress,
    _set_raw, _restore_term_attrs, _get_term_attrs,
    _hint_lines, _rows, _cols, text as _prompt_text,
)

_vlc = None
try:
    import vlc as _vlc  # type: ignore[import-untyped]
    _HAS_VLC = True
except ImportError:
    _HAS_VLC = False

from mutagen.id3 import ID3, ID3NoHeaderError  # type: ignore[reportPrivateImportUsage]
from src.lyrics.lyrics import _apply_markdown_formatting

SOURCE_TRANSCRIPT = 'transcript'
SOURCE_SYLT       = 'sylt'
SOURCE_USLT       = 'uslt'
SEG, WORD, EDIT, TAP, AUDITION = 'seg', 'word', 'edit', 'tap', 'audition'

_TAP_HEADER_MAX_LEN = 38
_AUD_CLIP   = 1.0    # audition: seconds played at a line's start, and at its end
_AUD_STEP   = 0.05   # audition: fine whole-line move per arrow press
_AUD_COARSE = 0.25   # audition: coarser whole-line move per , / . press


def _srt(t: float) -> str:
    t = max(0.0, float(t))
    h, r = divmod(t, 3600); m, s = divmod(r, 60)
    ms = round((s % 1) * 1000); s = int(s)
    if ms == 1000: ms, s = 0, s + 1
    return f"{int(h):02d}:{int(m):02d}:{s:02d},{ms:03d}"


def _fmt(t: float | None) -> str:
    if t is None:
        return "──:──.───"
    t = max(0.0, float(t))
    m, s = divmod(t, 60)
    ms = round((s % 1) * 1000); s = int(s)
    if ms == 1000: ms, s = 0, s + 1
    return f"{int(m):02d}:{s:02d}.{ms:03d}"


_TS_W  = 21   # "MM:SS.mmm → MM:SS.mmm"  (9 + " → " + 9)
_DUR_W = 6    # "123.4s" right-justified


def _norm(t: str) -> str:
    """Canonical match normalization: lowercase, hyphens→spaces, punctuation
    dropped.  This is the single basis for ALL JSON↔MD comparison (alignment and
    the verify report) so the two never disagree about what 'matches'."""
    t = (t or "").lower().replace('-', ' ')
    return re.sub(r'[^a-z0-9 ]', '', t).strip()


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


def _range(s_t, e_t) -> str:
    """Fixed-width 'start → end' timestamp block (21 visible cols)."""
    return f"{_fmt(s_t)} {'→'} {_fmt(e_t)}"


def _dur(s_t, e_t) -> str:
    """Fixed-width duration column ('12.3s' r-justified, blank if untimed)."""
    if s_t is None or e_t is None:
        return " " * _DUR_W
    return f"{max(0.0, (e_t or 0) - (s_t or 0)):.1f}s".rjust(_DUR_W)


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')


def _clip(s: str, width: int, ell: str = "…") -> str:
    """Truncate an ANSI-coloured string to `width` VISIBLE columns.

    Escape sequences are copied through without counting toward the width, so a
    truncation never lands mid-escape and never leaves colour bleeding.  This is
    the hard guarantee that no rendered line can exceed the terminal and wrap.
    """
    if width <= 0:
        return ""
    if ui_utils.visual_len(s) <= width:
        return s
    budget = max(0, width - len(ell))
    out, vis, i, n = [], 0, 0, len(s)
    while i < n and vis < budget:
        m = _ANSI_RE.match(s, i)
        if m:
            out.append(m.group()); i = m.end(); continue
        out.append(s[i]); vis += 1; i += 1
    # copy any trailing escapes so styling closes cleanly
    while i < n:
        m = _ANSI_RE.match(s, i)
        if not m: break
        out.append(m.group()); i = m.end()
    return "".join(out) + ell + C.RESET


def _parse(s: str) -> float | None:
    s = s.strip()
    try:
        if ':' in s:
            m, rest = s.split(':', 1)
            return int(m) * 60 + float(rest)
        return float(s)
    except (ValueError, IndexError):
        return None


# Segmented timestamp editor (EDIT mode): start and end as MM:SS.mmm, each field
# individually tabbable — mirrors the segmented time widget in prompt.py.
_EDIT_ORDER  = ['sm', 'ss', 'sms', 'em', 'es', 'ems']
_EDIT_MAXLEN = {'sm': 2, 'ss': 2, 'sms': 3, 'em': 2, 'es': 2, 'ems': 3}
_EDIT_LIM    = {'sm': 99, 'ss': 59, 'sms': 999, 'em': 99, 'es': 59, 'ems': 999}
_EDIT_START  = ('sm', 'ss', 'sms')
_EDIT_END    = ('em', 'es', 'ems')


def _ts_parts(v: float | None) -> tuple[str, str, str]:
    """(MM, SS, mmm) zero-padded strings for a timestamp, matching `_fmt`."""
    v = max(0.0, float(v or 0.0))
    m, s = divmod(v, 60)
    ms = round((s % 1) * 1000); s = int(s)
    if ms == 1000: ms, s = 0, s + 1
    return f"{int(m):02d}", f"{s:02d}", f"{ms:03d}"


def _is_ms(fk: str) -> bool:
    return fk in ('sms', 'ems')


def _field_value(fk: str, digits: str) -> int:
    """Numeric value of a field's digits.  Milliseconds fill from the LEFT (a
    fraction of a second): '5' → 500, '50' → 500, '05' → 050.  Minutes and
    seconds are plain integers: '5' → 5."""
    if not digits:
        return 0
    return int(digits.ljust(3, '0')[:3]) if _is_ms(fk) else int(digits)


def _field_str(fk: str, value: int) -> str:
    """Canonical fixed-width digits for a value (ms always 3, else field width)."""
    return f"{value:03d}" if _is_ms(fk) else f"{value:0{_EDIT_MAXLEN[fk]}d}"


def _render_edit_fields(edit: dict) -> tuple[str, str]:
    """Render (start, end) as segmented MM:SS.mmm with a caret on the active field.
    Milliseconds show trailing zeros dimly (so a typed '5' reads as '500')."""
    flds   = edit['fields']
    active = _EDIT_ORDER[edit['fi']]
    epos   = edit['pos']

    def _fld(fk: str) -> str:
        val = "".join(flds.get(fk, []))
        w   = _EDIT_MAXLEN[fk]
        if fk == active:
            # Render exactly `w` cells; the caret INVERTS one of them and never
            # appends, so the box keeps a constant width as you type.  Unfilled
            # cells show ms padding ('0', part of the value) or a blank for min/sec.
            cur = min(epos, w - 1)
            pad = '0' if _is_ms(fk) else ' '
            cells = []
            for i in range(w):
                filled = i < len(val)
                ch = val[i] if filled else pad
                if i == cur:
                    cells.append(f"{C.INVERT}{C.BOLD}{ch}{C.RESET}")
                elif filled:
                    cells.append(f"{C.BOLD}{ch}{C.RESET}")
                else:
                    cells.append(f"{C.DIM}{ch}{C.RESET}")
            return "".join(cells)
        if _is_ms(fk):
            return f"{C.DIM}{val.ljust(w, '0')}{C.RESET}"   # left-filled fraction → 500
        return f"{C.DIM}{val.zfill(w)}{C.RESET}"             # right-aligned count → 05

    def _sep(c: str) -> str:
        return f"{C.DIM}{c}{C.RESET}"

    return (f"{_fld('sm')}{_sep(':')}{_fld('ss')}{_sep('.')}{_fld('sms')}",
            f"{_fld('em')}{_sep(':')}{_fld('es')}{_sep('.')}{_fld('ems')}")


def _find_transcript(mp3_path: str) -> str | None:
    d    = os.path.dirname(mp3_path)
    base = os.path.splitext(os.path.basename(mp3_path))[0]
    for name in [f"{base}.json", f"{base}_timings.json", "transcript.json",
                 os.path.join("Transcript", f"{base}.json")]:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _sidecar_path(jpath: str) -> str:
    """Working document that sits beside the transcript.  Edits autosave here and
    are only written back to the transcript when the user commits."""
    return jpath[:-5] + ".sync.json" if jpath.endswith(".json") else jpath + ".sync.json"


def _file_fp(path: str) -> str:
    """Cheap content fingerprint (size + short hash) used to detect that the
    original transcript.json changed under a working sidecar (external edit)."""
    try:
        import hashlib
        with open(path, "rb") as f:
            data = f.read()
        return f"{len(data)}:{hashlib.md5(data).hexdigest()[:16]}"
    except OSError:
        return ""


def _ensure_ids(segs: list, meta: dict) -> None:
    """Assign immutable ids to any block/word that lacks one.

    `wid` (word) is the atomic, permanent identity — splitting a segment just
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


# Fields the editor adds for its own bookkeeping — stripped when committing so the
# transcript.json the user chose to keep clean stays in its original schema.
_SIDECAR_FIELDS = ("wid", "bid", "line_ref", "_md_text")


def _clean_seg(seg: dict) -> dict:
    """Deep-ish copy of a seg with editor-only fields removed (for the Whisper
    export): drops the sidecar bookkeeping AND `kind`, leaving a plain Whisper
    segment (start/end/text/avg_logprob/words)."""
    _strip = _SIDECAR_FIELDS + ("kind", "words")
    out = {k: v for k, v in seg.items() if k not in _strip}
    if "words" in seg:
        out["words"] = [{k: v for k, v in w.items() if k not in _SIDECAR_FIELDS}
                        for w in seg["words"]]
    return out


def _make_stage_dir(text: str, start: float | None = None, end: float | None = None) -> dict:
    # `_md_text` is the immutable MD-derived text used to reconcile this seg
    # against the overlay.  It survives relabelling the visible `text` (via 'l'),
    # so a committed direction keeps claiming its MD event and never re-appears as
    # a ghost overlay.  It is persisted to JSON, so the claim survives reload too.
    return {"kind": "stage_dir", "text": text, "_md_text": text,
            "start": round(start, 3) if start is not None else None,
            "end":   round(end,   3) if end   is not None else None,
            "words": []}


def _rebuild_srt(segs: list) -> str:
    blocks = []
    for i, seg in enumerate(segs, 1):
        _kind = seg.get("kind")
        txt   = seg.get("text", "").strip()
        s     = seg.get("start")
        e     = seg.get("end")
        if _kind == 'stage_dir' and txt:
            txt = f"*({txt})*"
        if _kind == 'dead_air' and not txt:
            continue  # pure silence — no SRT block
        if txt and s is not None:
            blocks.append(f"{i}\n{_srt(s)} --> {_srt(e if e is not None else s)}\n{txt}\n")
    return "\n".join(blocks)


def find_lyrics(mp3_path: str) -> str | None:
    """Return source key if any lyrics data exists, else None."""
    if _find_transcript(mp3_path):
        return SOURCE_TRANSCRIPT
    try:
        audio = ID3(mp3_path)
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
            # Resume from the working document (already carries ids + alignment).
            with open(sc, encoding='utf-8') as f:
                sdata = json.load(f)
            segs = sdata['segments']
            meta = sdata.get('meta', {})
            _ensure_ids(segs, meta)   # ids for anything hand-added since last save
            # Detect that transcript.json changed under us since this sidecar was
            # written (external edit / commit elsewhere) — warn, don't clobber.
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
        from src.lyrics.lyrics import normalize_lyric_newlines
        audio = ID3(mp3_path)

        sylt = audio.getall('SYLT')
        if sylt:
            import re as _re
            entries = sylt[0].text  # [(text, ms), ...]
            segs = []
            for i, (text, start_ms) in enumerate(entries):
                end_ms = entries[i + 1][1] if i + 1 < len(entries) else start_ms + 5000
                # Reconstruct the stage_dir marker that do_save wraps as *(...)*
                # so the overlay's reconciliation recognises it as materialized
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
            return segs, SOURCE_SYLT, {'mp3': mp3_path}

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
    if seg.get("start") is not None: seg["start"] = round(seg["start"] + delta, 3)
    if seg.get("end")   is not None: seg["end"]   = round(seg["end"]   + delta, 3)
    for w in seg.get("words", []):
        if w.get("start") is not None: w["start"] = round(w["start"] + delta, 3)
        if w.get("end")   is not None: w["end"]   = round(w["end"]   + delta, 3)


def _shift_word(word: dict, delta: float) -> None:
    if word.get("start") is not None: word["start"] = round(word["start"] + delta, 3)
    if word.get("end")   is not None: word["end"]   = round(word["end"]   + delta, 3)


def _draw(segs, cursor, seg_cursor, mode, prev_mode, selected, viewport,
          dirty, undo_depth, track_name, playing, play_pos,
          edit, source, total_s, show_hints=False, md_overlay=None,
          md_quality=None, aud_now=None, aud_editing=False) -> tuple[list[str], int, dict]:
    cols = _cols()
    rows = _rows()
    n    = len(segs)
    vp   = viewport

    # Row → item-index map for mouse clicks.  Keyed by the index of a line within
    # the returned list (== its offset below the widget anchor + MARGIN_V), so the
    # click handler never has to reverse-engineer the layout: only rows that carry
    # a selectable item (a seg in SEG, a word in WORD) get an entry.  Speaker
    # banners, ✦ stage-direction overlays and blank spacers are absent, so clicks
    # on them are correctly ignored.
    hit_map: dict[int, int] = {}

    show_words = mode == WORD or (mode == EDIT and prev_mode == WORD)

    B      = cols                 # body width (inside the 2-col indent)
    indent = " " * ui_utils.MARGIN_H

    label = ("TAP SYNC"    if mode == TAP
             else "AUDITION"   if mode == AUDITION
             else "TRANSCRIPT" if source == SOURCE_TRANSCRIPT
             else "LYRICS")

    # Right-aligned status cluster: dirty dot · playhead · position.
    pos_str    = f"{cursor + 1} / {n}" if n else "–"
    right_plain, right_disp = "", ""
    if dirty:
        right_disp += f"{C.ACCENT}●{C.RESET}   "; right_plain += "●   "
    if playing:
        _pp = f"▶ {_fmt(play_pos)}"
        right_disp += f"{C.ACCENT}{_pp}{C.RESET}   "; right_plain += _pp + "   "
    right_disp += f"{C.DIM}{pos_str}{C.RESET}"; right_plain += pos_str
    right_w = len(right_plain)

    # Left cluster: title + track (+ current-seg preview in word view), truncated
    # so the whole bar fits the body width and never wraps.
    ctx_plain = ""
    if show_words and segs and seg_cursor < len(segs):
        _raw = segs[seg_cursor].get("text", "").strip()
        ctx_plain = f"  ›  {_raw}"
    track_budget = max(0, B - len(label) - 2 - right_w - 2)
    track_txt    = ui_utils.truncate_text(track_name + ctx_plain, track_budget)
    left_disp    = f"{C.BOLD}{label}{C.RESET}  {C.DIM}{track_txt}{C.RESET}"
    left_w       = len(label) + 2 + len(track_txt)
    gap          = max(1, B - left_w - right_w)

    out: list[str] = [
        "",
        indent + left_disp + " " * gap + right_disp,
        f"{C.DIM}{indent}{'─' * B}{C.RESET}",
    ]

    sep = f"{C.DIM}{indent}{'─' * B}{C.RESET}"

    if mode == TAP:
        prev_s = segs[cursor - 1] if cursor > 0     else None
        curr_s = segs[cursor]     if cursor < n      else None
        next_s = segs[cursor + 1] if cursor < n - 1  else None

        def _tap_line(seg, bold: bool = False, arrow: bool = False) -> str:
            ptr = f"{indent}{C.ACCENT}▶{C.RESET} " if arrow else indent + "  "
            if seg is None:
                return f"{ptr} {C.DIM}──{C.RESET}"
            ts_v = _fmt(seg['start']) if seg.get("start") is not None else " " * 9
            ts   = f"{C.DIM}{ts_v}{C.RESET}"
            col  = C.BOLD + C.PRIMARY if bold else C.DIM
            budget = max(4, B - 3 - 9 - 3)   # ptr(3) + ts(9) + gaps(3)
            txt  = ui_utils.truncate_text(seg["text"].strip(), budget)
            return f"{ptr} {ts}   {col}{txt}{C.RESET}"

        # Build footer first so its actual height governs padding.
        footer: list[str] = [sep]
        pairs: list[tuple[str, str]] = [('spc/↵', 'mark')]
        if cursor > 0: pairs += [('←→', '±0.25s'), (',/.', '±0.1s')]
        pairs.append(('p', 'pause' if playing else 'play'))
        pairs.append(('s', 'save'))
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        pairs += [('esc', 'done'), ('q', 'quit')]
        footer.extend(_hint_lines(*pairs))

        TAP_ROWS = 9  # 3 content lines + 4 blank spacers + progress + blank
        n_body   = max(0, rows - 1 - 2 * MARGIN_V - 3 - len(footer))
        pad_top  = max(0, (n_body - TAP_ROWS) // 2)

        out += [""] * pad_top
        out.append(_tap_line(prev_s))
        out += ["", ""]
        out.append(_tap_line(curr_s, bold=True, arrow=True))
        out += ["", ""]
        out.append(_tap_line(next_s))
        out.append("")

        if total_s > 0:
            pct = min(play_pos / total_s, 1.0)
            _clock = f"{_fmt(play_pos)} / {_fmt(total_s)}"
            bar = ui_utils.get_progress_bar(pct, max(4, B - len(_clock) - 2))
            out.append(f"{indent}{C.DIM}{bar}  {_clock}{C.RESET}")
        else:
            out.append("")

        padding = max(0, (rows - 1 - 2 * MARGIN_V) - len(out) - len(footer))
        _cap = ui_utils.get_terminal_width() - ui_utils.MARGIN_H
        return [_clip(ln, _cap) for ln in out + [""] * padding + footer], vp, hit_map

    if mode == AUDITION:
        prev_s = segs[cursor - 1] if cursor > 0    else None
        curr_s = segs[cursor]     if cursor < n     else None
        next_s = segs[cursor + 1] if cursor < n - 1 else None
        s_t = curr_s.get("start") if curr_s else None
        e_t = curr_s.get("end")   if curr_s else None

        def _line(seg, bold=False) -> str:
            if seg is None:
                return f"{indent}"
            col = C.BOLD + C.PRIMARY if bold else C.DIM
            txt = ui_utils.truncate_text(seg.get("text", "").strip() or "(…)", max(4, B - 6))
            return f"{indent}   {col}{txt}{C.RESET}"

        # Bottom line: either the static start/end readout (playing one accented)
        # or, when editing, the inline segmented MM:SS.mmm editor for this line.
        if aud_editing:
            s_d, e_d = _render_edit_fields(edit)
            marks = f"{C.ACCENT}✎{C.RESET}  start {s_d}    end {e_d}"
        else:
            def _mark(lbl, val, on) -> str:
                v = _fmt(val) if val is not None else "──:──.───"
                c = C.ACCENT + C.BOLD if on else C.DIM
                return f"{c}{lbl} {v}{C.RESET}"
            marks = (f"{_mark('start', s_t, aud_now == 'start')}"
                     f"    {C.DIM}·{C.RESET}    {_mark('end', e_t, aud_now == 'end')}")

        footer: list[str] = [sep]
        if aud_editing:
            pairs = [('tab/⇧tab', 'field'), ('←→', 'cursor'), ('↑↓', 'adjust'),
                     ('↵', 'apply'), ('esc', 'cancel')]
        else:
            pairs = [('↑↓', 'line'), ('spc', 'whole line'),
                     ('←→', 'move 50ms'), (',/.', 'move ¼s'),
                     ('[/]', 'hear s/e'), ('e', 'edit dur')]
            if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
            pairs += [('p', 'pause' if playing else 'play'), ('esc', 'back'), ('q', 'quit')]
        footer.extend(_hint_lines(*pairs))

        AUD_ROWS = 8
        n_body   = max(0, rows - 1 - 2 * MARGIN_V - 3 - len(footer))
        pad_top  = max(0, (n_body - AUD_ROWS) // 2)

        out += [""] * pad_top
        out.append(_line(prev_s))
        out += [""]
        out.append(_line(curr_s, bold=True))
        out += ["", ""]
        out.append(f"{indent}   {marks}")
        out += [""]
        out.append(_line(next_s))
        out.append("")

        if total_s > 0:
            pct = min(play_pos / total_s, 1.0)
            _clock = f"{_fmt(play_pos)} / {_fmt(total_s)}"
            bar = ui_utils.get_progress_bar(pct, max(4, B - len(_clock) - 2))
            out.append(f"{indent}{C.DIM}{bar}  {_clock}{C.RESET}")
        else:
            out.append("")

        padding = max(0, (rows - 1 - 2 * MARGIN_V) - len(out) - len(footer))
        _cap = ui_utils.get_terminal_width() - ui_utils.MARGIN_H
        return [_clip(ln, _cap) for ln in out + [""] * padding + footer], vp, hit_map

    # Build footer first so its actual height governs how many items we show.
    footer = [sep]

    if mode == EDIT:
        s_d, e_d = _render_edit_fields(edit)
        footer.append(f"  {C.ACCENT}✎{C.RESET}  start  {s_d}    end  {e_d}")
        footer.extend(_hint_lines(('tab/⇧tab', 'field'), ('←→', 'cursor'),
                                  ('↑↓', 'adjust'), ('↵', 'apply'), ('esc', 'cancel')))

    elif mode == WORD:
        pairs: list[tuple[str, str]] = [('↑↓', 'navigate'), ('←→', '±0.25s'), (',/.', '±0.1s'), ('[/]', '±1s')]
        pairs += [('x', 'split'), ('e', 'edit')]
        if _HAS_VLC: pairs.append(('p', 'preview'))
        pairs.append(('s', 'save'))
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        pairs += [('esc', 'back'), ('q', 'quit')]
        footer.extend(_hint_lines(*pairs))

    else:  # SEG
        cur_item  = segs[cursor] if (segs and cursor < len(segs)) else None
        cur_kind  = cur_item.get("kind") if cur_item else None
        is_air    = cur_kind == "dead_air"
        is_sdir   = cur_kind == "stage_dir"
        _md_label = 'remove md' if md_overlay is not None else 'import md'
        _has_sdir = bool(md_overlay) and any(ov['kind'] == 'stage_dir' for ov in md_overlay)
        if show_hints:
            pairs = [('↑↓', 'navigate'), ('←→', '±0.25s'), (',/.', '±0.1s'), ('[/]', '±1s')]
            if source == SOURCE_TRANSCRIPT: pairs.append(('w', 'words'))
            if _HAS_VLC:                    pairs.append(('t', 'tap sync'))
            if _HAS_VLC:                    pairs.append(('b', 'audition'))
            pairs += [('e', 'edit'), ('j', 'join↓'), ('J/K', 'move↑/↓'),
                      ('a', 'dead air'), ('d', 'del'), ('l', 'label'),
                      ('k', 'air↔dir'), ('r', 'fill gaps'), ('m', _md_label)]
            if _has_sdir: pairs.append(('M', 'commit stage dirs'))
            pairs += [('c', 'credits'), ('spc', 'mark')]
            if _HAS_VLC: pairs.append(('p', 'preview'))
            pairs.append(('s', 'save working'))
            if source == SOURCE_TRANSCRIPT:
                pairs += [('V', 'verify md'), ('S', 'split by line'), ('W', 'write file')]
            if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
            pairs += [('?', 'hide hints'), ('q', 'quit')]
        else:
            pairs = [('↑↓', 'navigate'), ('←→', '±0.1s')]
            if is_air or is_sdir:
                pairs += [('d', 'del'), ('l', 'label'),
                          ('k', 'make dir' if is_air else 'make air'),
                          ('e', 'timing'), ('J/K', 'move')]
            else:
                pairs += [('e', 'edit'), ('j', 'join'), ('J/K', 'move')]
                if source == SOURCE_TRANSCRIPT: pairs.append(('w', 'words'))
            if _has_sdir: pairs.append(('M', 'commit stage dirs'))
            pairs += [('s', 'save'), ('?', 'more')]
            if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
            pairs.append(('q', 'quit'))
            # (W = write to transcript.json — shown in full hints via ?)
        if selected: pairs.append(('', f'{len(selected)} marked'))
        footer.extend(_hint_lines(*pairs))

    HEADER    = 3
    available = rows - 1 - 2 * MARGIN_V - HEADER - 2 - len(footer)
    vis       = max(1, available)

    # Compact grid (visible widths after the 2-col indent):
    #   ptr(1) sp(1) chk(1) sp(1)  start(9)  sp(2)  →  TEXT column (offset 15)
    # The words now begin at ~col 15 (was 37) so the transcript reads like a
    # script; duration / end-time / flags live in a dim right gutter.
    PREFIX_W = 1 + 1 + 1 + 1 + 9 + 2   # == 15

    def _rhs(s_t, e_t, is_cur, flags):
        """Right gutter, pinned to the body's right edge: end-time (current row
        only), then any warnings, then the DURATION last so it sits flush right in
        its own column.  Returns (display_string, visible_width)."""
        parts: list[tuple[str, str]] = []
        if is_cur and s_t is not None and e_t is not None:
            parts.append((C.DIM, f"→ {_fmt(e_t)}"))
        parts.extend(flags)                              # warnings BEFORE duration
        if s_t is not None:
            parts.append((C.DIM, _dur(s_t, e_t).strip() or "·"))   # duration pinned rightmost
        disp = "  ".join(f"{c}{t}{C.RESET}" for c, t in parts)
        wid  = sum(len(t) for _, t in parts) + 2 * (len(parts) - 1) if parts else 0
        return disp, wid

    def _compose(prefix_disp, text_raw, text_color, rhs_disp, rhs_w, fmt=False):
        """Fixed prefix, flexible prominent text, right-aligned dim gutter.  The
        visible line never exceeds the body width, so it cannot wrap."""
        budget = max(1, B - PREFIX_W - (rhs_w + 2 if rhs_w else 0))
        t      = ui_utils.truncate_text(text_raw, budget)
        if fmt:
            # Emphasised words are underlined (strong ones also bold) so they stand
            # out even on the bold current row, and every span restores `base` —
            # the row's own colour — so the highlight continues past the emphasis
            # instead of the span's reset blanking the rest of the line.
            t_disp = _apply_markdown_formatting(
                t, base=text_color, strong=f"{C.BOLD}{C.UNDERLINE}", em=C.UNDERLINE)
            # A music note is accent on the current row (text_color set) and dim
            # elsewhere — accent is reserved for the current line.  Either way it
            # renders in a fixed style rather than inheriting the row's bold, so
            # its glyph stays consistent; the span restores `base` afterwards.
            if '♪' in t_disp:
                _note_c = C.ACCENT if text_color else C.DIM
                t_disp = t_disp.replace('♪', f"{C.RESET}{_note_c}♪{C.RESET}{text_color}")
        else:
            t_disp = t
        if text_color:
            t_disp = f"{text_color}{t_disp}{C.RESET}"
        if rhs_w:
            # Pad off the VISIBLE width of what we actually print — markdown
            # formatting strips the * markers, so len(t) over-counts and the
            # gutter (and the duration pinned to its right edge) would drift left.
            vis = ui_utils.visual_len(t_disp)
            pad = max(1, B - PREFIX_W - vis - rhs_w)
            return indent + prefix_disp + t_disp + " " * pad + rhs_disp
        return indent + prefix_disp + t_disp

    if show_words:
        items   = segs[seg_cursor].get("words", []) if segs else []
        n_items = len(items)
        if cursor < vp:         vp = cursor
        if cursor >= vp + vis:  vp = cursor - vis + 1
        vp = max(0, min(vp, max(0, n_items - vis)))
        out.append(f"{indent}{C.DIM}↑  {vp} above{C.RESET}" if vp > 0 else "")
        for slot in range(vis):
            i = vp + slot
            if i >= n_items: break
            item  = items[i]
            is_cur = (i == cursor)
            s_t   = item.get("start"); e_t = item.get("end")
            prev  = items[i - 1] if i > 0 else None
            overlap = (prev is not None and s_t is not None
                       and prev.get("end") is not None and s_t < prev["end"])
            ptr     = f"{C.ACCENT}›{C.RESET}" if is_cur else " "
            start_d = f"{C.DIM}{_fmt(s_t)}{C.RESET}" if s_t is not None else " " * 9
            prefix  = f"{ptr}   {start_d}  "   # ptr + 3sp fills the chk slot → offset 15
            flags   = [(C.YELLOW, "⚠ overlap")] if overlap else []
            rhs_d, rhs_w = _rhs(s_t, e_t, is_cur, flags)
            hit_map[len(out)] = i
            out.append(_compose(prefix, item.get('word', '').strip(),
                                 C.PRIMARY if is_cur else "", rhs_d, rhs_w))
        below = n_items - (vp + vis)
        out.append(f"{indent}{C.DIM}↓  {below} below{C.RESET}" if below > 0 else "")

    else:  # SEG — build flat display_items interleaving segs with MD overlay
        display_items: list[dict] = []
        ov_map: dict[int, list] = {}
        for ov in (md_overlay or []):
            ov_map.setdefault(ov['before_si'], []).append(ov)
        for si, seg in enumerate(segs):
            for ov in ov_map.get(si, []):
                display_items.append({'type': 'overlay', 'data': ov})
            display_items.append({'type': 'seg', 'si': si, 'seg': seg})
        for ov in ov_map.get(len(segs), []):
            display_items.append({'type': 'overlay', 'data': ov})

        # Isolated stage directions (floating ✦ overlays) get a blank line before
        # and after so they hover as a separate beat, not glued to the dialogue.
        def _is_float_sd(it: dict) -> bool:
            return it['type'] == 'overlay' and it['data']['kind'] == 'stage_dir'
        spaced: list[dict] = []
        _n = len(display_items); _i = 0
        while _i < _n:
            if _is_float_sd(display_items[_i]):
                spaced.append({'type': 'blank'})
                while _i < _n and _is_float_sd(display_items[_i]):
                    spaced.append(display_items[_i]); _i += 1
                spaced.append({'type': 'blank'})
            else:
                spaced.append(display_items[_i]); _i += 1
        display_items = spaced

        n_disp    = len(display_items)
        cursor_di = next((i for i, it in enumerate(display_items)
                          if it['type'] == 'seg' and it['si'] == cursor), 0)
        if cursor_di < vp:         vp = cursor_di
        if cursor_di >= vp + vis:  vp = cursor_di - vis + 1
        vp = max(0, min(vp, max(0, n_disp - vis)))

        segs_above = sum(1 for it in display_items[:vp] if it['type'] == 'seg')
        out.append(f"{indent}{C.DIM}↑  {segs_above} above{C.RESET}" if vp > 0 else "")

        for slot in range(vis):
            di = vp + slot
            if di >= n_disp: break
            it = display_items[di]

            if it['type'] == 'blank':
                out.append("")
            elif it['type'] == 'overlay':
                ov = it['data']
                if ov['kind'] == 'speaker':
                    # Speaker banner — plain white name (no colour), then a dim rule.
                    name  = ov['text']
                    stg   = ov.get('stage', '')
                    head  = f"{C.RESET}{name}"
                    hplain = name
                    if stg:
                        head  += f" {C.DIM}({stg}){C.RESET}"
                        hplain += f" ({stg})"
                    rule = '┈' * max(1, B - len(hplain) - 3)
                    out.append(f"{indent}{head} {C.DIM}{rule}{C.RESET}")
                else:
                    # Isolated stage direction (uncommitted overlay) — left-aligned
                    # with a single ✦ and italic text, blank-fenced above and below
                    # so it still reads as its own beat.  Its ✦ sits in the same
                    # column as a committed cue's, so floating and committed cues line up.
                    _sd = ui_utils.truncate_text(f"({ov['text'].strip()})",
                                                 max(4, B - PREFIX_W - 2))
                    # A floating overlay is never the current line, so its ✦ is dim
                    # (accent stays reserved for the current row).
                    out.append(f"{indent}{' ' * PREFIX_W}{C.DIM}✦{C.RESET} "
                               f"{C.DIM}{C.ITALIC}{_sd}{C.RESET}")
            else:
                si  = it['si']
                seg = it['seg']
                is_cur = si == cursor
                is_sel = si in selected
                s_t    = seg.get('start')
                e_t    = seg.get('end')
                is_air  = seg.get('kind') == 'dead_air'
                is_sdir = seg.get('kind') == 'stage_dir'

                prev_seg = segs[si - 1] if si > 0 else None
                overlap  = (prev_seg is not None and s_t is not None
                            and prev_seg.get('end') is not None
                            and s_t < prev_seg['end'])
                word_overlap = False
                if not is_air and not is_sdir:
                    ws = seg.get('words', [])
                    for wi in range(len(ws) - 1):
                        if (ws[wi].get('end') is not None
                                and ws[wi + 1].get('start') is not None
                                and ws[wi]['end'] > ws[wi + 1]['start']):
                            word_overlap = True; break

                ptr  = f"{C.ACCENT}›{C.RESET}" if is_cur else " "
                chk  = f"{C.ACCENT}✔{C.RESET}" if is_sel else " "

                flags: list[tuple[str, str]] = []
                if overlap:      flags.append((C.YELLOW, "⚠ overlap"))
                if word_overlap: flags.append((C.YELLOW, "⚠ words"))
                if md_quality is not None and not is_air and not is_sdir:
                    _mq = md_quality.get(si)
                    if _mq is not None:
                        if _mq['score'] == 0.0:    flags.append((C.DIM,    "? md"))
                        elif _mq['score'] < 0.75:  flags.append((C.YELLOW, "≈ md"))

                start_d = f"{C.DIM}{_fmt(s_t)}{C.RESET}" if s_t is not None else " " * 9
                prefix  = f"{ptr} {chk} {start_d}  "
                rhs_disp, rhs_w = _rhs(s_t, e_t, is_cur, flags)

                hit_map[len(out)] = si   # this display row selects seg `si`
                if is_air or is_sdir:
                    sym   = '✦' if is_sdir else '◌'
                    _lbl  = seg.get('text', '').strip()
                    disp  = f"({_lbl})" if _lbl else ("(…)" if is_sdir else "silence")
                    budget = max(1, B - PREFIX_W - 2 - (rhs_w + 2 if rhs_w else 0))
                    disp   = ui_utils.truncate_text(disp, budget)
                    # Accent is reserved for the current row; off-row markers dim.
                    sym_c = C.ACCENT if is_cur else C.DIM
                    if is_sdir and s_t is not None:
                        # A *timed* stage direction is a placed beat — the italic
                        # label sets it apart from plain silence (◌) and from an
                        # untimed cue still awaiting its moment.
                        lbl_c = f"{C.PRIMARY if is_cur else C.DIM}{C.ITALIC}"
                    else:
                        lbl_c = C.PRIMARY if is_cur else C.DIM
                    body   = f"{prefix}{sym_c}{sym}{C.RESET} {lbl_c}{disp}{C.RESET}"
                    if rhs_w:
                        pad = max(1, B - PREFIX_W - 2 - len(disp) - rhs_w)
                        body += " " * pad + rhs_disp
                    out.append(indent + body)
                else:
                    _mq     = (md_quality or {}).get(si)
                    _use_md = bool(_mq and _mq.get('md_text'))
                    raw     = (_mq['md_text'] if _use_md else seg.get('text', '')).strip()
                    # Words are the anchor: bright/bold on the current row, normal
                    # elsewhere.  Markdown emphasis renders as styling, not literal *.
                    out.append(_compose(prefix, raw, C.PRIMARY if is_cur else "",
                                        rhs_disp, rhs_w, fmt=True))

        segs_below = sum(1 for it in display_items[vp + vis:] if it['type'] == 'seg')
        out.append(f"{indent}{C.DIM}↓  {segs_below} below{C.RESET}" if segs_below > 0 else "")

    padding = max(0, (rows - 1 - 2 * MARGIN_V) - len(out) - len(footer))
    _cap = ui_utils.get_terminal_width() - ui_utils.MARGIN_H
    return [_clip(ln, _cap) for ln in out + [""] * padding + footer], vp, hit_map


_AIR_GAP_THRESHOLD = 1.5   # seconds — gaps shorter than this are not recommended


def _build_md_overlay(segs: list[dict], md_path: str) -> tuple[list, dict, dict]:
    """Overlay the MD script onto the JSON segments via ONE word-level alignment.

    The JSON and MD are the same spoken words (modulo punctuation / hyphens / ♪).
    So we align the two word streams once (difflib) and then, line by line, hand
    each MD line's words to whichever segment its words lined up with:
      • one segment covers the line  → it shows the whole line (with punctuation),
      • several segments cover it     → each shows its own run of the line's words.
    A segment whose words span two MD lines shows both (and is a split candidate).

    Returns (overlay, quality, links):
      overlay — [{kind:'speaker'|'stage_dir', text, before_si, stage?}], display only.
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
        if next_si is not None and next_si != prev_si:
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': next_si})
        elif prev_si is not None:
            stage_dirs.append({'text': d['text'], 'before_line': d['line'], 'anchor_si': prev_si + 1})
        else:
            stage_dirs.append({'text': d['text'], 'before_line': d['line']})

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
                                      'stage': line_meta[l].get('stage', ''), 'before_si': si}))

    # Standalone stage directions: a committed stage_dir seg / labelled dead_air
    # claims the matching one by text so it isn't shown both as a seg AND a ✦.
    materialized: set = set()
    for _seg in segs:
        if _seg.get('kind') not in ('stage_dir', 'dead_air'):
            continue
        sn = _norm(_seg.get('_md_text') or _seg.get('text', ''))
        if not sn:
            continue
        for idx, sd in enumerate(stage_dirs):
            if idx not in materialized and _norm(sd['text']) == sn:
                materialized.add(idx)
                break
    covered = sorted(line_first_seg)
    for idx, sd in enumerate(stage_dirs):
        if idx in materialized:
            continue
        if sd.get('anchor_si') is not None:   # mid-line dir pinned to a seg boundary
            bsi = min(sd['anchor_si'], len(segs))
        else:
            L = sd['before_line']
            if L in line_first_seg:
                bsi = line_first_seg[L]
            else:                             # its line has no seg → next covered line, else end
                k = bisect.bisect_left(covered, L)
                bsi = line_first_seg[covered[k]] if k < len(covered) else len(segs)
        items.append((bsi, 0, {'kind': 'stage_dir', 'text': sd['text'], 'before_si': bsi}))

    items.sort(key=lambda t: (t[0], t[1]))    # stable: stage dir before speaker at a tie
    overlay = [it for _, _, it in items]
    return overlay, quality, links


def _word_streams(segs: list, md_path: str):
    """Shared word-level alignment core for verification and speaker-splitting.

    Returns (js_toks, md_toks, sm):
      js_toks: (token, seg_index, word_index, start)   — spoken words, normalized
      md_toks: (token, line_id, speaker)               — MD dialogue words
      sm:      difflib.SequenceMatcher over the two token streams
    Both streams use `_norm_words`/`_spoken_text` so punctuation is ignored,
    hyphens are spaces, and inline stage directions never count as dialogue.
    line_id is the 0-based dialogue-line index — the SAME id space as the overlay's
    `line_ref`, so a computed split can pin each piece directly.
    """
    import difflib
    from src.lyrics.lyrics import _parse_markdown_dialogue

    with open(md_path, encoding='utf-8') as fh:
        dl = _parse_markdown_dialogue(fh.read())

    md_toks: list[tuple] = []
    lid = 0
    for item in dl:
        if item.is_empty() or item.is_stage_direction():
            continue
        spk = " & ".join(s.strip() for s in item.speakers) if item.speakers else "?"
        for tok in _norm_words(_spoken_text(item.text)):
            md_toks.append((tok, lid, spk))
        lid += 1

    js_toks: list[tuple] = []
    for si, s in enumerate(segs):
        if s.get('kind') in ('dead_air', 'stage_dir'):
            continue
        for wi, w in enumerate(s.get('words', [])):
            for tok in _norm_words(w.get('word', '')):
                js_toks.append((tok, si, wi, w.get('start')))

    sm = difflib.SequenceMatcher(None, [j[0] for j in js_toks],
                                 [m[0] for m in md_toks], autojunk=False)
    return js_toks, md_toks, sm


def _multi_line_segs(segs: list, md_path: str) -> list[dict]:
    """Find dialogue segments whose words span more than one MD line — i.e. Whisper
    merged consecutive script lines into a single segment (e.g. MARTIN's "Dash
    away ..." + DOUGLAS & MARTIN's "... dash away, dash away, all!", or two lines of
    the same speaker).  Each result gives the word-index boundaries to cut at (every
    MD-line change inside the seg) and the line_id each resulting piece pins to, so
    splitting lands exactly one segment per script line — the clean baseline to word-
    split from.
    """
    js, md, sm = _word_streams(segs, md_path)
    jline: dict[int, tuple] = {}     # js token index → (line_id, speaker)
    for tag, i1, i2, k1, k2 in sm.get_opcodes():
        if tag == 'equal':
            for off in range(i2 - i1):
                jline[i1 + off] = (md[k1 + off][1], md[k1 + off][2])

    from collections import defaultdict
    per_seg: dict[int, list] = defaultdict(list)   # si → [(word_index, line_id, speaker)]
    for ti, (tok, si, wi, st) in enumerate(js):
        if ti in jline:
            per_seg[si].append((wi, *jline[ti]))

    out = []
    for si in sorted(per_seg):
        runs = []   # (line_id, speaker, first_word_index) collapsing consecutive same-line words
        for wi, ln, sp in per_seg[si]:
            if not runs or runs[-1][0] != ln:
                runs.append((ln, sp, wi))
        if len(runs) >= 2:                       # spans ≥2 MD lines → split per line
            out.append({'seg': si,
                        'boundaries': [wi for (_, _, wi) in runs[1:]],   # every line change
                        'line_refs':  [ln for (ln, _, _) in runs],
                        'runs': runs})
    return out


def _verify_matchup(segs: list, md_path: str) -> dict:
    """Full word-level verification of the JSON↔MD matchup.  Diffs the entire
    spoken word stream against the entire MD dialogue word stream (normalized:
    punctuation ignored, hyphens→spaces).  Independent of the segment alignment,
    so it surfaces every word the alignment missed — EXTRA / MISSING / CHANGED —
    plus SPLIT segments that merge two speakers into one.

    Returns {'lines': [str], 'summary': {...}}.
    """
    js_toks, md_toks, sm = _word_streams(segs, md_path)

    def _near_time(i: int) -> float | None:
        if i < len(js_toks) and js_toks[i][3] is not None:
            return js_toks[i][3]
        for j in range(min(i, len(js_toks)) - 1, -1, -1):
            if js_toks[j][3] is not None:
                return js_toks[j][3]
        return None

    def _row(t, sym, label, col, loc, detail):
        # Aligned, colour-coded row: dim timestamp · coloured badge · dim location
        # · the actual WORDS bright, so the eye lands on what differs.
        ts    = _fmt(t) if t is not None else "  --:--  "
        badge = f"{col}{sym} {label:<7}{C.RESET}"          # 9 visible cols
        locf  = f"{C.DIM}{(loc or '')[:16]:<16}{C.RESET}"  # 16 visible cols
        return f"{C.DIM}{ts}{C.RESET}  {badge}  {locf}  {detail}"

    lines: list[str] = []
    n_extra = n_missing = n_changed = n_equal = 0
    for tag, i1, i2, k1, k2 in sm.get_opcodes():
        if tag == 'equal':
            n_equal += (i2 - i1); continue
        heard  = " ".join(js_toks[x][0] for x in range(i1, i2))
        script = " ".join(md_toks[x][0] for x in range(k1, k2))
        t = _near_time(i1)
        if tag == 'delete':
            n_extra += (i2 - i1)
            lines.append(_row(t, '+', 'EXTRA', C.CYAN, '', f"{C.PRIMARY}{heard}{C.RESET}"))
        elif tag == 'insert':
            n_missing += (k2 - k1)
            loc = f"{md_toks[k1][2]} · L{md_toks[k1][1]}"
            lines.append(_row(t, '–', 'MISSING', C.YELLOW, loc, f"{C.PRIMARY}{script}{C.RESET}"))
        else:
            n_changed += max(i2 - i1, k2 - k1)
            detail = f"{C.DIM}{heard}{C.RESET}  {C.ACCENT}→{C.RESET}  {C.PRIMARY}{script}{C.RESET}"
            lines.append(_row(t, '~', 'CHANGED', C.MAGENTA, f"L{md_toks[k1][1]}", detail))

    split = _multi_line_segs(segs, md_path)
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
            spk   = c['runs'][i][1] if i < len(c['runs']) else '?'
            txt   = " ".join(w.get('word', '') for w in chunk).strip()
            if not txt:
                continue
            split_lines.append(
                f"           {C.CYAN}{spk[:17]:<17}{C.RESET} {C.PRIMARY}{txt}{C.RESET}")

    total_js, total_md = len(js_toks), len(md_toks)
    rate = (100 * n_equal // max(1, total_md))
    summary = {'json_words': total_js, 'md_words': total_md, 'matched': n_equal,
               'extra': n_extra, 'missing': n_missing, 'changed': n_changed,
               'multi_speaker': len(split), 'match_pct': rate,
               'discrepancies': len(lines) + len(split_lines)}
    header = [
        f"{C.BOLD}VERIFY · JSON ↔ MD word matchup{C.RESET}"
        f"   {C.DIM}(punctuation ignored · hyphens = spaces){C.RESET}",
        f"{C.BOLD}{rate}%{C.RESET} matched  {C.DIM}({n_equal}/{total_md} words){C.RESET}    "
        f"{C.YELLOW}– {n_missing} missing{C.RESET}   {C.CYAN}+ {n_extra} extra{C.RESET}   "
        f"{C.MAGENTA}~ {n_changed} changed{C.RESET}   {C.GREEN}⇄ {len(split)} to split{C.RESET}",
        "",
    ]
    body = list(lines)
    if split_lines:
        if lines:
            body.append("")
        body.append(f"{C.BOLD}Segments spanning more than one MD line{C.RESET}"
                    f"  {C.DIM}— press S to split them one-per-line{C.RESET}")
        body += split_lines
    if not body:
        header.append(f"{C.GREEN}✓ every spoken word matches, and every segment is one MD line.{C.RESET}")
    return {'lines': header + body, 'summary': summary}


def _make_dead_air(start: float, end: float, label: str = "") -> dict:
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


def lyrics_editor(mp3_path: str) -> None:
    result = _load(mp3_path)
    if result is None:
        ui_utils.show_status("No lyrics or transcript found for this track.")
        return

    segs, source, aux = result
    track_name = os.path.basename(os.path.dirname(mp3_path))
    if aux.get('drift'):
        ui_utils.show_status(
            "⚠ transcript.json changed since this working copy — W will overwrite it.", 6.0)

    # Lead-in offset for tap sync (compensates for reaction time)
    try:
        from src.config import load_config
        cfg = load_config()
        tap_offset_s = -cfg.get("lyric_lead_in", 0.0)  # seconds
    except Exception:
        tap_offset_s = 0.0

    mode       = SEG
    prev_mode  = SEG
    cursor     = 0
    seg_cursor = 0
    selected: set[int] = set()
    viewport   = 0
    dirty      = False
    show_hints = False
    md_overlay: list | None = None
    md_quality: dict | None = None
    md_path:    str  | None = None
    undo_stack: list = []

    edit_fields: dict[str, list[str]] = {}   # fk → digit chars (MM:SS.mmm per bound)
    edit_orig:   dict[str, str]       = {}   # snapshot at open → detect what changed
    edit_fi    = 0                            # active field index into _EDIT_ORDER
    edit_pos   = 0                            # caret position within the active field
    edit_fresh = False                        # active field untouched → next digit clears it

    playing    = False
    play_until = 0.0
    play_pos   = 0.0

    aud_queue: list[tuple[float, float, str]] = []   # AUDITION: clips left to play
    aud_now:   str | None = None                      # 'start' / 'end' clip playing now
    aud_editing = False                               # AUDITION: inline timestamp editor open

    fd  = sys.stdin.fileno()
    old = _get_term_attrs(fd)

    mp = None
    if _vlc is not None:
        try:
            old_fd  = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 2); os.close(devnull)
            inst = _vlc.Instance('--no-video', '--quiet')
            mp_i = inst.media_player_new()           # type: ignore[union-attr]
            mp_i.set_media(inst.media_new(mp3_path)) # type: ignore[union-attr]
            os.dup2(old_fd, 2); os.close(old_fd)
            mp = mp_i
        except (AttributeError, OSError):
            pass

    w = _Widget(fd)

    def cur_words() -> list:
        return segs[seg_cursor].get("words", []) if segs else []

    def _sync_seg_bounds(si: int) -> None:
        words = segs[si].get("words", [])
        if not words: return
        first = words[0].get("start")
        last  = words[-1].get("end")
        if first is not None: segs[si]["start"] = round(first, 3)
        if last  is not None: segs[si]["end"]   = round(last,  3)

    def do_preview(start_s: float, dur: float | None = None) -> None:
        """Play from start_s.  With `dur`, stop automatically after that many
        seconds (the main loop honours play_until); without it, play open-ended."""
        nonlocal playing, play_until, play_pos
        if mp is None: return
        start_s = max(0.0, start_s)
        mp.set_time(int(start_s * 1000))
        if not mp.is_playing():
            mp.play(); time.sleep(0.15)
        playing    = True
        play_until = float('inf') if dur is None else time.time() + dur
        play_pos   = start_s

    def do_stop() -> None:
        nonlocal playing
        if mp and mp.is_playing(): mp.pause()
        playing = False

    def _aud_next() -> None:
        """Play the next queued clip; stop when the queue drains."""
        nonlocal aud_now
        if not aud_queue:
            aud_now = None; do_stop(); return
        lo, hi, label = aud_queue.pop(0)
        aud_now = label
        do_preview(lo, max(0.1, hi - lo))

    def _aud_clips(which: str = 'line') -> None:
        """Queue audition playback for the current line and start it:
          'line'  → the whole line, start → end (the default when you land on it);
          'start' / 'end' → a short (<=_AUD_CLIP) clip of just that boundary;
          'both'  → the start clip then the end clip, in sequence.
        Clips never bleed into the neighbouring lines."""
        nonlocal aud_queue
        aud_queue = []
        if segs and cursor < len(segs):
            s = segs[cursor].get("start")
            e = segs[cursor].get("end")
            if which == 'line' and s is not None:
                hi = e if e is not None else s + 3.0
                aud_queue.append((s, max(s + 0.1, hi), 'line'))
            if which in ('both', 'start') and s is not None:
                hi = s + _AUD_CLIP if e is None else min(e, s + _AUD_CLIP)
                aud_queue.append((s, max(s + 0.1, hi), 'start'))
            if which in ('both', 'end') and e is not None:
                floor = s if s is not None else 0.0     # never precede the line's start
                lo = max(0.0, floor, e - _AUD_CLIP)
                aud_queue.append((lo, e, 'end'))
        _aud_next()

    def _aud_shift(delta: float) -> None:
        """Move the whole line — both timestamps and its words — by delta, keeping
        its duration, then play the start so the new position can be judged by ear.
        (The duration itself is changed by typing in the editor: press e.)"""
        if not segs or cursor >= len(segs):
            return
        if segs[cursor].get("start") is None:
            ui_utils.show_status("This line has no timestamp to move."); return
        apply_segs([cursor], delta)   # shifts start, end and words together (undoable)
        _aud_clips('start')

    def refresh_overlay() -> None:
        """Re-derive the MD overlay from the current segs.  Call after ANY
        structural change to segs.  The overlay is a pure projection of
        (segs, MD): rebuilding is always safe and never duplicates, because
        committed stage_dir segs are reconciled inside _build_md_overlay."""
        nonlocal md_overlay, md_quality
        if md_path:
            md_overlay, md_quality, _links = _build_md_overlay(segs, md_path)
            for _si, _lid in _links.items():   # record durable alignment on segs
                if _lid is not None:
                    segs[_si]['line_ref'] = _lid
        else:
            md_overlay = md_quality = None

    def apply_segs(idxs: list[int], delta: float) -> None:
        nonlocal dirty
        for i in idxs: _shift_seg(segs[i], delta)
        dirty = True; undo_stack.append(('seg', list(idxs), delta))

    def apply_word(si: int, wi: int, delta: float) -> None:
        nonlocal dirty
        _shift_word(segs[si]["words"][wi], delta)
        _sync_seg_bounds(si)
        dirty = True; undo_stack.append(('word', si, wi, delta))

    def do_undo() -> None:
        nonlocal dirty, cursor, mode
        if not undo_stack: return
        op = undo_stack.pop()
        if   op[0] == 'seg':
            [_shift_seg(segs[i], -op[2]) for i in op[1]]
        elif op[0] == 'seg_end':
            segs[op[1]]["end"] = op[2]
        elif op[0] == 'word':
            _shift_word(segs[op[1]]["words"][op[2]], -op[3])
            _sync_seg_bounds(op[1])
        elif op[0] == 'word_end':
            segs[op[1]]["words"][op[2]]["end"] = op[3]
            _sync_seg_bounds(op[1])
        elif op[0] == 'split':
            si, orig = op[1], op[2]
            segs[si:si + 2] = [orig]
            mode = SEG; cursor = si
        elif op[0] == 'join':
            ci, seg_a, seg_b = op[1], op[2], op[3]
            segs[ci:ci + 1] = [seg_a, seg_b]
            cursor = ci
        elif op[0] == 'delete':
            ci, seg = op[1], op[2]
            segs[ci:ci] = [seg]  # re-insert; do NOT overwrite the neighbour
            cursor = ci
        elif op[0] == 'tap':
            idx, old_start, old_prev_end = op[1], op[2], op[3]
            segs[idx]["start"] = old_start
            if idx > 0 and old_prev_end is None:
                segs[idx - 1]["end"] = None
            cursor = idx
        elif op[0] == 'snapshot':
            segs[:] = op[1]
        dirty = bool(undo_stack)
        # segs may have changed structurally — keep the overlay in sync so
        # nothing is left pointing at stale indices.
        if op[0] in ('split', 'join', 'delete', 'snapshot'):
            refresh_overlay()

    def do_save() -> None:
        nonlocal dirty
        if source == SOURCE_TRANSCRIPT:
            # Save to the WORKING document (sidecar) — never touches the original
            # transcript.json until the user commits with 'W'.
            _ensure_ids(segs, aux['meta'])
            sdata = {'version': 1, 'source_json': os.path.basename(aux['jpath']),
                     'meta': aux['meta'], 'segments': segs}
            with open(aux['sidecar'], 'w', encoding='utf-8') as f:
                json.dump(sdata, f, indent=2, ensure_ascii=False)
            ui_utils.show_status(
                f"Saved to {os.path.basename(aux['sidecar'])} — press W to write transcript.json")
            dirty = False; undo_stack.clear()
            return
        else:
            from src.lyrics.lyrics import save_sylt_entries
            entries = []
            for s in segs:
                if s.get('start') is None:
                    continue
                _kind = s.get('kind')
                if _kind == 'dead_air' and not s.get('text', '').strip():
                    continue  # pure silence
                _txt = s.get('text', '').strip()
                # Wrap BOTH committed stage directions and labelled dead air in
                # *(...)* so _load reconstructs kind='stage_dir' on reload.  Without
                # this a labelled dead_air round-trips as a plain seg whose text can
                # collide with an MD stage direction and show twice.
                if _kind in ('stage_dir', 'dead_air'):
                    _txt = f"*({_txt})*" if _txt else ""
                    if not _txt: continue
                entries.append((_txt, max(0, int((s['start'] or 0) * 1000))))
            save_sylt_entries(aux['mp3'], entries)
        changed: set[int] = set()
        for op in undo_stack:
            if   op[0] == 'seg':      changed.update(op[1])
            elif op[0] == 'seg_end':  changed.add(op[1])
            elif op[0] in ('word', 'word_end'): changed.add(op[1])
            elif op[0] == 'split':    changed.update([op[1], op[1] + 1])
            elif op[0] == 'join':     changed.add(op[1])
            elif op[0] == 'tap':      changed.add(op[1])
        ui_utils.show_status(f"Saved — {len(changed)} line{'s' if len(changed) != 1 else ''} changed")
        dirty = False; undo_stack.clear()

    def do_commit() -> None:
        """Write timings back to transcript.json (+ .srt) in the ORIGINAL Whisper
        schema: spoken segments only.  The editor's stage-direction / dead-air
        beats and all bookkeeping fields (ids, alignment, kind) are dropped — they
        live on in the sidecar and the .md — so transcript.json stays a plain
        Whisper transcript of just the spoken words."""
        jpath = aux['jpath']
        try:
            with open(jpath, encoding='utf-8') as f:
                container = json.load(f)
        except (OSError, json.JSONDecodeError):
            container = {}
        spoken = [s for s in segs if s.get('kind') not in ('stage_dir', 'dead_air')]
        container['segments']      = [_clean_seg(s) for s in spoken]
        container['word_segments'] = [
            {'word': w['word'], 'start': w.get('start'),
             'end': w.get('end'), 'score': w.get('score')}
            for seg in spoken for w in seg.get('words', [])
        ]
        with open(jpath, 'w', encoding='utf-8') as f:
            json.dump(container, f, indent=2, ensure_ascii=False)
        with open(jpath[:-5] + '.srt', 'w', encoding='utf-8') as f:
            f.write(_rebuild_srt(segs))
        aux['meta']['source_fp'] = _file_fp(jpath)   # we now match the original

    def _pager(body: list, title: str) -> None:
        """Minimal scrollable full-screen viewer.  `body` lines are already
        coloured; the pager just windows and clips them."""
        vp = 0
        foot = [f"{C.DIM}{' ' * ui_utils.MARGIN_H}{'─' * _cols()}{C.RESET}"] + \
               _hint_lines(('↑↓', 'scroll'), ('PgUp/PgDn', 'page'),
                           ('Home/End', 'ends'), ('q', 'back'))
        while True:
            rows, cols = _rows(), _cols()
            vis = max(3, rows - 1 - 2 * MARGIN_V - len(foot) - 1)
            vp  = max(0, min(vp, max(0, len(body) - vis)))
            out = [""]
            for ln in body[vp:vp + vis]:
                out.append(_clip("  " + ln, cols + ui_utils.MARGIN_H))
            pad = max(0, (rows - 1 - 2 * MARGIN_V) - len(out) - len(foot))
            w.render(out + [""] * pad + foot)
            if not _wait_for_keypress(0.2):
                continue
            k = _read_key(fd)
            if   k in ('q', 'ESC', 'CTRL_C'): break
            elif k in ('UP', 'k'):            vp -= 1
            elif k in ('DOWN', 'j', 'SPACE'): vp += 1
            elif k == 'PGUP':                 vp -= vis
            elif k == 'PGDN':                 vp += vis
            elif k == 'HOME':                 vp = 0
            elif k == 'END':                  vp = len(body)

    def do_verify() -> None:
        from src.lyrics.lyrics import _find_markdown_for_audio
        _mdp = md_path or _find_markdown_for_audio(mp3_path)
        if not _mdp:
            ui_utils.show_status("No transcript.md found to verify against."); return
        rep = _verify_matchup(segs, _mdp)
        _rp = aux['jpath'][:-5] + '.verify.txt'
        try:
            with open(_rp, 'w', encoding='utf-8') as f:   # plain text (colour stripped)
                f.write("\n".join(ui_utils.strip_ansi(l) for l in rep['lines']) + "\n")
            _rp_note = f"  ·  report: {os.path.basename(_rp)}"
        except OSError:
            _rp_note = ""
        s = rep['summary']
        _pager(rep['lines'],
               f"VERIFY  {s['match_pct']}% matched  ·  {s['discrepancies']} issues{_rp_note}")
        w.anchor_reset()

    def do_speaker_split() -> None:
        """Split every segment that spans more than one MD line at its line
        boundaries, pinning each piece to its line — one seg per script line — then
        re-verify.  This is the clean 1:1 baseline to word-split from afterwards."""
        nonlocal dirty, cursor
        from src.lyrics.lyrics import _find_markdown_for_audio
        _mdp = md_path or _find_markdown_for_audio(mp3_path)
        if not _mdp:
            ui_utils.show_status("No transcript.md found to verify against."); return
        cands = _multi_line_segs(segs, _mdp)
        if not cands:
            ui_utils.show_status("✓ Every segment is a single MD line — nothing to split.")
            return
        _restore_term_attrs(fd, old)
        sys.stdout.write("\033[?1000l\033[?1006l")
        _ans = _prompt_text(
            f"Split {len(cands)} segment(s) that span multiple MD lines, one seg "
            f"per line? (y/N)")
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")
        w.anchor_reset()
        if not (_ans or "").strip().lower().startswith("y"):
            ui_utils.show_status("Split cancelled."); return
        undo_stack.append(('snapshot', list(segs)))
        n = len(cands)
        # split from the highest seg index down so earlier indices stay valid
        for c in sorted(cands, key=lambda c: c['seg'], reverse=True):
            pieces = _split_seg_at(segs[c['seg']], c['boundaries'], c['line_refs'])
            segs[c['seg']:c['seg'] + 1] = pieces
        cursor = min(cursor, len(segs) - 1)
        dirty = True
        refresh_overlay()
        remain = len(_multi_line_segs(segs, _mdp))   # verification
        ui_utils.show_status(
            f"Split {n} segment(s) to one-per-line — {remain} multi-line remaining.")

    def do_recategorise(si: int) -> None:
        """Flip the beat at `si` between dead air (◌) and a stage direction (✦),
        preserving timings.  A direction needs descriptive text, so promoting
        blank silence prompts for it.  Shared by the 'k' key and the click path."""
        nonlocal dirty
        _seg = segs[si] if (segs and 0 <= si < len(segs)) else None
        if not (_seg and _seg.get("kind") in ("dead_air", "stage_dir")):
            ui_utils.show_status("Only dead air / stage direction segments can be recategorised.")
            return
        _old_kind = _seg["kind"]
        _new_kind = "stage_dir" if _old_kind == "dead_air" else "dead_air"
        _text     = _seg.get("text", "").strip()
        if _new_kind == "stage_dir" and not _text:
            _restore_term_attrs(fd, old)
            sys.stdout.write("\033[?1000l\033[?1006l")
            sys.stdout.flush()
            try:                     # drop a click's pending mouse-release bytes so
                import termios       # they don't leak into the text prompt
                termios.tcflush(fd, termios.TCIFLUSH)
            except Exception:
                pass
            _lbl = _prompt_text("Stage direction text:")
            _set_raw(fd)
            sys.stdout.write("\033[?1000h\033[?1006h")
            w.anchor_reset()
            if _lbl is None or not _lbl.strip():
                ui_utils.show_status("Recategorise cancelled — a stage direction needs text.")
                return
            _text = _lbl.strip()
        # Replace with a fresh dict (never mutate in place) so the snapshot keeps
        # the original for undo.
        undo_stack.append(('snapshot', list(segs)))
        segs[si] = {**_seg, "kind": _new_kind, "text": _text}
        dirty = True
        refresh_overlay()
        ui_utils.show_status(
            "Recategorised as "
            f"{'stage direction' if _new_kind == 'stage_dir' else 'dead air'}.")

    def commit_field(field: str, val: float) -> None:
        nonlocal dirty
        if prev_mode == SEG:
            old_val = segs[cursor].get(field)
            if old_val is None:
                # Setting a previously unset timestamp directly
                undo_stack.append(('seg_end', cursor, None))
                segs[cursor][field] = round(val, 3); dirty = True
                return
            d = round(val - old_val, 3)
            if abs(d) < 1e-6: return
            if field == 'start':
                apply_segs([cursor], d)
            else:
                undo_stack.append(('seg_end', cursor, segs[cursor].get("end")))
                segs[cursor]["end"] = round(val, 3); dirty = True
        else:
            words = cur_words()
            if cursor < len(words):
                ww      = words[cursor]
                old_val = ww.get(field) or 0.0
                d       = round(val - old_val, 3)
                if abs(d) < 1e-6: return
                if field == 'start':
                    apply_word(seg_cursor, cursor, d)
                else:
                    undo_stack.append(('word_end', seg_cursor, cursor, ww.get("end", 0.0)))
                    ww["end"] = round(val, 3)
                    _sync_seg_bounds(seg_cursor)
                    dirty = True

    def _edit_target() -> dict | None:
        if prev_mode == SEG:
            return segs[cursor] if (segs and cursor < len(segs)) else None
        ws = cur_words()
        return ws[cursor] if cursor < len(ws) else None

    def _edit_prefill() -> None:
        """Populate the segmented fields from the item being edited."""
        nonlocal edit_fields, edit_orig, edit_fi, edit_pos, edit_fresh
        item = _edit_target() or {}
        sm, ss, sms = _ts_parts(item.get('start'))
        em, es, ems = _ts_parts(item.get('end'))
        edit_fields = {'sm': list(sm), 'ss': list(ss), 'sms': list(sms),
                       'em': list(em), 'es': list(es), 'ems': list(ems)}
        edit_orig   = {k: "".join(v) for k, v in edit_fields.items()}
        edit_fi     = 0
        edit_pos    = 0
        edit_fresh  = True   # first digit fills the field from the left

    def _edit_field_key(key: str) -> None:
        """Handle one field-manipulation key for the segmented editor.  Digits fill
        from the left (a fresh field is replaced on the first digit); ↑↓ spin the
        value; ms treats its digits as a right-padded fraction (5 → 500)."""
        nonlocal edit_fi, edit_pos, edit_fresh
        fk   = _EDIT_ORDER[edit_fi]
        buf  = edit_fields[fk]
        maxl = _EDIT_MAXLEN[fk]
        if key == 'TAB':
            edit_fi = (edit_fi + 1) % len(_EDIT_ORDER)
            edit_pos = 0; edit_fresh = True
        elif key == 'BACKTAB':
            edit_fi = (edit_fi - 1) % len(_EDIT_ORDER)
            edit_pos = 0; edit_fresh = True
        elif key == 'LEFT':
            edit_pos = max(0, edit_pos - 1); edit_fresh = False
        elif key == 'RIGHT':
            edit_pos = min(len(buf), edit_pos + 1); edit_fresh = False
        elif key in ('UP', 'DOWN'):
            v = _field_value(fk, "".join(buf)) + (1 if key == 'UP' else -1)
            v = max(0, min(_EDIT_LIM[fk], v))
            buf[:] = list(_field_str(fk, v)); edit_pos = len(buf); edit_fresh = False
        elif key == 'BACKSPACE':
            edit_fresh = False
            if edit_pos > 0: buf.pop(edit_pos - 1); edit_pos -= 1
        elif key == 'DELETE':
            edit_fresh = False
            if edit_pos < len(buf): buf.pop(edit_pos)
        elif key == 'HOME':
            edit_pos = 0; edit_fresh = False
        elif key == 'END':
            edit_pos = len(buf); edit_fresh = False
        elif len(key) == 1 and key.isdigit():
            if edit_fresh:
                buf[:] = [key]; edit_pos = 1; edit_fresh = False
            elif len(buf) < maxl:
                buf.insert(edit_pos, key); edit_pos += 1

    def _edit_apply() -> None:
        """Commit only the bound(s) whose digits changed, reusing commit_field so
        start keeps its shift semantics and end is set absolutely."""
        def _val(keys) -> float:
            m  = _field_value(keys[0], "".join(edit_fields[keys[0]]))
            s  = _field_value(keys[1], "".join(edit_fields[keys[1]]))
            ms = _field_value(keys[2], "".join(edit_fields[keys[2]]))
            return round(m * 60 + s + ms / 1000.0, 3)
        if any("".join(edit_fields[k]) != edit_orig[k] for k in _EDIT_START):
            commit_field('start', _val(_EDIT_START))
        if any("".join(edit_fields[k]) != edit_orig[k] for k in _EDIT_END):
            commit_field('end', _val(_EDIT_END))

    try:
        _set_raw(fd)
        sys.stdout.write("\033[?1000h\033[?1006h")  # enable mouse (click + scroll)
        sys.stdout.flush()
        need_redraw = True
        hit_map: dict[int, int] = {}   # row → item index, rebuilt on every _draw

        while True:
            n = len(segs)

            total_s: float = 0.0
            if mp and mp.get_length() > 0:
                total_s = mp.get_length() / 1000.0
            elif segs and segs[-1].get("end") is not None:
                total_s = float(segs[-1]["end"])

            if playing:
                if mp and not mp.is_playing():
                    # clip ran to the track's natural end
                    if aud_queue: _aud_next()
                    else: playing = False; aud_now = None
                    need_redraw = True
                elif time.time() > play_until:
                    # this clip's window elapsed — play the next queued one, or stop
                    if aud_queue: _aud_next()
                    else: do_stop(); aud_now = None
                    need_redraw = True
                elif mp:
                    pos = mp.get_time() / 1000.0
                    if abs(pos - play_pos) > 0.05:
                        play_pos = pos; need_redraw = True
                    # auto-scroll cursor in SEG/WORD/TAP (never in AUDITION — the
                    # boundary being auditioned must stay put while it plays)
                    if mode in (SEG, WORD, TAP):
                        scroll_items = segs if mode in (SEG, TAP) else cur_words()
                        new_cur = cursor
                        for i in range(cursor + 1, len(scroll_items)):
                            if (scroll_items[i].get("start") or 0.0) <= play_pos:
                                new_cur = i
                            else:
                                break
                        if new_cur != cursor:
                            cursor = new_cur; need_redraw = True

            if ui_utils.consume_resize():
                w.anchor_reset(); need_redraw = True

            if need_redraw:
                lines, viewport, hit_map = _draw(
                    segs, cursor, seg_cursor, mode, prev_mode, selected,
                    viewport, dirty, len(undo_stack), track_name,
                    playing, play_pos,
                    {'fields': edit_fields, 'fi': edit_fi, 'pos': edit_pos},
                    source, total_s,
                    show_hints, md_overlay, md_quality, aud_now, aud_editing,
                )
                w.render(lines)
                need_redraw = False

            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            need_redraw = True

            # Mouse: scroll navigates; a click positions the cursor on a row.
            if key == 'SCROLL_UP':
                key = 'UP'
            elif key == 'SCROLL_DOWN':
                key = 'DOWN'
            elif key.startswith(('MOUSE_CLICK:', 'MOUSE_RELEASE:')):
                # Only the press acts; the paired release is swallowed so one
                # physical click is one logical action.  `hit_map` (from the last
                # _draw) maps a rendered line index to its item; the widget draws
                # line[i] at terminal row w.row + MARGIN_V + i, so invert that.
                # Clicking the line that is ALREADY current opens its word view;
                # a not-yet-current line is first made current, so it takes a
                # second click (a double-click) to open.
                if key.startswith('MOUSE_CLICK:') and mode in (SEG, WORD) and w.row is not None:
                    parts = key.split(':')
                    r = int(parts[2]) if len(parts) > 2 else 0
                    line_idx = r - w.row - ui_utils.MARGIN_V
                    target = hit_map.get(line_idx)
                    if target is not None:
                        _t = segs[target] if target < len(segs) else {}
                        if mode == SEG and target == cursor:
                            # Confirm on the already-current line:
                            #  · dialogue with words → open its word view
                            #  · a *timed* dead air   → reclassify it (e.g. to a
                            #    stage direction) now that you've placed it
                            if source == SOURCE_TRANSCRIPT and _t.get("words"):
                                seg_cursor = target
                                mode = WORD; cursor = 0; viewport = 0
                            elif _t.get("kind") == "dead_air" and _t.get("start") is not None:
                                do_recategorise(target)
                        else:
                            cursor = target
                continue

            if mode == EDIT:
                if key == 'ESC':
                    mode = prev_mode
                elif key == 'ENTER':
                    _edit_apply()
                    mode = prev_mode
                else:
                    _edit_field_key(key)
                continue

            if mode == TAP:
                if key in ('q', 'CTRL_C'):
                    do_stop(); break
                elif key == 's':
                    do_save()
                elif key == 'u':
                    do_undo()
                elif key == 'ESC':
                    mode = SEG; do_stop()
                elif key == 'p':
                    if playing:
                        do_stop()
                    else:
                        do_preview(play_pos)  # resume from where we paused
                elif key in ('SPACE', 'ENTER') and cursor < n:
                    old_start    = segs[cursor].get("start")
                    old_prev_end = segs[cursor - 1].get("end") if cursor > 0 else None
                    corrected    = round(play_pos + tap_offset_s, 3)
                    segs[cursor]["start"] = max(0.0, corrected)
                    if cursor > 0 and segs[cursor - 1].get("end") is None:
                        segs[cursor - 1]["end"] = segs[cursor]["start"]
                    undo_stack.append(('tap', cursor, old_start, old_prev_end))
                    dirty = True
                    if cursor < n - 1:
                        cursor += 1
                    else:
                        mode = SEG; do_stop()  # reached end — done
                elif key == 'LEFT' and cursor > 0:
                    apply_segs([cursor - 1], -0.25)
                elif key == 'RIGHT' and cursor > 0:
                    apply_segs([cursor - 1],  0.25)
                elif key == ',':
                    if cursor > 0: apply_segs([cursor - 1], -0.1)
                elif key == '.':
                    if cursor > 0: apply_segs([cursor - 1],  0.1)
                continue

            if mode == AUDITION:
                if aud_editing:
                    # inline timestamp editor at the bottom of the audition view
                    if key in ('q', 'CTRL_C'):
                        aud_editing = False; do_stop(); break
                    elif key == 'ESC':
                        aud_editing = False                 # cancel, keep listening
                    elif key == 'ENTER':
                        _edit_apply(); aud_editing = False
                        _aud_clips('line')                  # hear the line in context
                    else:
                        _edit_field_key(key)
                    continue
                if key in ('q', 'CTRL_C'):
                    do_stop(); break
                elif key == 'ESC':
                    mode = SEG; do_stop(); aud_now = None
                elif key == 'UP':
                    cursor = max(0, cursor - 1); _aud_clips('line')
                elif key == 'DOWN':
                    cursor = min(n - 1, cursor + 1); _aud_clips('line')
                elif key in ('SPACE', 'ENTER'):
                    _aud_clips('line')          # play the whole line
                # move the whole line by ear (both timestamps together):
                elif key == 'LEFT':
                    _aud_shift(-_AUD_STEP)
                elif key == 'RIGHT':
                    _aud_shift( _AUD_STEP)
                elif key == ',':
                    _aud_shift(-_AUD_COARSE)
                elif key == '.':
                    _aud_shift( _AUD_COARSE)
                elif key == '[':
                    _aud_clips('start')         # hear the start boundary (no change)
                elif key == ']':
                    _aud_clips('end')           # hear the end boundary (no change)
                elif key == 'u':
                    do_undo(); _aud_clips('line')
                elif key == 'e':
                    # open the inline timestamp editor for this line
                    if segs and cursor < len(segs):
                        do_stop(); aud_now = None
                        prev_mode   = SEG       # audition edits the line (a seg)
                        aud_editing = True
                        _edit_prefill()
                elif key == 'p':
                    if playing: do_stop(); aud_now = None
                    else:       _aud_clips('line')
                continue

            items = segs if mode == SEG else cur_words()
            n_i   = len(items)

            if key in ('q', 'CTRL_C'):
                if playing: do_stop()
                break
            elif key == '?' and mode == SEG:
                show_hints = not show_hints
            elif key == 's':
                do_save()
            elif key == 'W' and source == SOURCE_TRANSCRIPT:
                _restore_term_attrs(fd, old)
                sys.stdout.write("\033[?1000l\033[?1006l")
                _ans = _prompt_text(
                    f"Write spoken words (Whisper format) to "
                    f"{os.path.basename(aux['jpath'])}? (y/N)")
                _set_raw(fd)
                sys.stdout.write("\033[?1000h\033[?1006h")
                w.anchor_reset()
                if (_ans or "").strip().lower().startswith("y"):
                    do_commit()    # writes transcript.json + refreshes fingerprint
                    do_save()      # persist the refreshed fingerprint into the sidecar
                    ui_utils.show_status(f"Written to {os.path.basename(aux['jpath'])} + .srt")
                else:
                    ui_utils.show_status("Commit cancelled — working file untouched.")
            elif key == 'V' and source == SOURCE_TRANSCRIPT:
                do_verify()
            elif key == 'S' and mode == SEG and source == SOURCE_TRANSCRIPT:
                do_speaker_split()
            elif key == 'u':
                do_undo()
            elif key == 'UP':
                cursor = max(0, cursor - 1)
            elif key == 'DOWN':
                cursor = min(n_i - 1, max(0, cursor + 1))
            elif key == 'SPACE' and mode == SEG:
                selected.symmetric_difference_update({cursor})
            elif key == 'c' and mode == SEG:
                try:
                    from mutagen.id3 import ID3
                    from mutagen.id3._util import ID3NoHeaderError
                    _aud = ID3(mp3_path)
                    _credits: list[str] = []
                    _tcom = _aud.getall('TCOM')
                    if _tcom:
                        _credits.append("Music by: " + "; ".join(str(f) for f in _tcom))
                    _text = _aud.getall('TEXT')
                    if _text:
                        _credits.append("Words by: " + "; ".join(str(f) for f in _text))
                    if _credits:
                        for _cl in _credits:
                            segs.append({"text": _cl})
                        cursor = len(segs) - 1
                        dirty = True
                        refresh_overlay()  # seg count changed — re-derive before_si
                    else:
                        ui_utils.show_status("No composer or lyricist tags found.")
                except Exception as _ce:
                    ui_utils.show_status(f"Could not read tags: {_ce}")
            elif key == 'a' and mode == SEG:
                _restore_term_attrs(fd, old)
                sys.stdout.write("\033[?1000l\033[?1006l")
                # At position 0 offer inserting before the first seg (track intro)
                _insert_before = (cursor == 0 and segs
                                  and segs[0].get("kind") not in ("dead_air", "stage_dir")
                                  and (segs[0].get("start") or 0) > 0)
                if _insert_before:
                    _where = _prompt_text("Insert before first segment (b) or after cursor (a)?")
                    _insert_before = (_where or "").strip().lower().startswith("b")
                _dur_s = _prompt_text("Dead air duration (seconds):")
                _label = _prompt_text("Stage direction (leave blank for silence):")
                _set_raw(fd)
                sys.stdout.write("\033[?1000h\033[?1006h")
                w.anchor_reset()
                if _dur_s is not None:
                    try:
                        _dur = float(_dur_s.strip())
                        if _dur > 0:
                            if _insert_before:
                                _end   = round(float(segs[0].get("start") or 0), 3)
                                _start = max(0.0, _end - _dur)
                                _air   = _make_dead_air(_start, _end, (_label or "").strip())
                                segs.insert(0, _air)
                                # cursor stays at 0, which is now the new dead air
                            else:
                                _ref   = segs[cursor].get("end") if segs and cursor < len(segs) else None
                                _start = round(float(_ref or 0), 3)
                                _air   = _make_dead_air(_start, _start + _dur, (_label or "").strip())
                                segs.insert(cursor + 1, _air)
                                cursor += 1
                            dirty = True
                            refresh_overlay()
                    except ValueError:
                        ui_utils.show_status("Enter a number of seconds, e.g. 2 or 1.5")
            elif key == 'd' and mode == SEG:
                if segs and cursor < len(segs) and segs[cursor].get("kind") in ("dead_air", "stage_dir"):
                    undo_stack.append(('delete', cursor, segs[cursor]))
                    segs.pop(cursor)
                    cursor = min(cursor, len(segs) - 1)
                    dirty = True
                    refresh_overlay()
                else:
                    ui_utils.show_status("Only dead air / stage direction segments can be deleted here.")
            elif key == 'l' and mode == SEG:
                if segs and cursor < len(segs) and segs[cursor].get("kind") in ("dead_air", "stage_dir"):
                    _cur_lbl = segs[cursor].get("text", "")
                    _restore_term_attrs(fd, old)
                    sys.stdout.write("\033[?1000l\033[?1006l")
                    _prompt = ("Stage direction text:" if segs[cursor].get("kind") == "stage_dir"
                               else "Stage direction (blank = silence):")
                    _new_lbl = _prompt_text(_prompt, default=_cur_lbl)
                    _set_raw(fd)
                    sys.stdout.write("\033[?1000h\033[?1006h")
                    w.anchor_reset()
                    if _new_lbl is not None:
                        segs[cursor]["text"] = _new_lbl.strip()
                        dirty = True
                        refresh_overlay()  # text changed — re-reconcile the overlay
                else:
                    ui_utils.show_status("Cursor is not on a dead air or stage direction segment.")
            elif key == 'k' and mode == SEG:
                do_recategorise(cursor)
            elif key == 'r' and mode == SEG:
                # Scan gaps between timed segments; also time any untimed stage_dir segs.
                _restore_term_attrs(fd, old)
                sys.stdout.write("\033[?1000l\033[?1006l")
                _inserted = 0; _timed = 0
                _i = 0
                _new_segs: list[dict] = []
                while _i < len(segs):
                    _cur = segs[_i]
                    # Auto-time an untimed stage_dir that sits between two timed segs
                    if (_cur.get("kind") == "stage_dir" and _cur.get("start") is None):
                        _prev_t = _new_segs[-1].get("end") if _new_segs else None
                        _next_t = next((segs[j].get("start") for j in range(_i + 1, len(segs))
                                        if segs[j].get("start") is not None), None)
                        if _prev_t is not None and _next_t is not None:
                            _cur = dict(_cur)
                            _cur["start"] = round(float(_prev_t), 3)
                            _cur["end"]   = round(float(_next_t), 3)
                            _timed += 1
                    _new_segs.append(_cur)
                    _nxt = segs[_i + 1] if _i + 1 < len(segs) else None
                    _skip_kinds = ("dead_air", "stage_dir")
                    if (_nxt is not None
                            and _cur.get("end") is not None
                            and _nxt.get("start") is not None
                            and _nxt["start"] - _cur["end"] >= _AIR_GAP_THRESHOLD
                            and _nxt.get("kind") not in _skip_kinds
                            and _cur.get("kind") not in _skip_kinds):
                        _gap_start = float(_cur["end"])
                        _gap_end   = float(_nxt["start"])
                        _gap       = round(_gap_end - _gap_start, 3)
                        if mp is not None:
                            _play_from = max(0.0, _gap_start - 0.3)
                            mp.set_time(int(_play_from * 1000))
                            if not mp.is_playing():
                                mp.play(); time.sleep(0.15)
                        _lbl = _prompt_text(
                            f"Gap of {_gap}s — stage direction? (blank = silence, skip = ignore):")
                        if mp is not None and mp.is_playing():
                            mp.pause()
                        if _lbl is not None and _lbl.strip().lower() != 'skip':
                            _new_segs.append(_make_dead_air(_gap_start, _gap_end, _lbl.strip()))
                            _inserted += 1
                    _i += 1
                segs[:] = _new_segs
                if _inserted:
                    refresh_overlay()
                _set_raw(fd)
                sys.stdout.write("\033[?1000h\033[?1006h")
                w.anchor_reset()
                if _inserted or _timed:
                    dirty = True
                    _msg = []
                    if _inserted: _msg.append(f"{_inserted} dead air added")
                    if _timed:    _msg.append(f"{_timed} stage dir timed")
                    ui_utils.show_status(", ".join(_msg))
                else:
                    ui_utils.show_status("No gaps above threshold found.")
            elif key == 'm' and mode == SEG:
                if md_overlay is not None:
                    md_overlay = md_quality = md_path = None
                    viewport   = 0
                    ui_utils.show_status("Transcript overlay removed.")
                else:
                    from src.lyrics.lyrics import _find_markdown_for_audio
                    _md_path = _find_markdown_for_audio(mp3_path)
                    if _md_path is None:
                        ui_utils.show_status("No transcript.md found next to this file.")
                    else:
                        try:
                            md_overlay, md_quality, _links = _build_md_overlay(segs, _md_path)
                            for _si, _lid in _links.items():   # record durable alignment
                                if _lid is not None:
                                    segs[_si]['line_ref'] = _lid
                            md_path    = _md_path
                            viewport   = 0
                            ui_utils.show_status(
                                f"Overlay: {len(md_overlay)} annotations from {os.path.basename(_md_path)}"
                            )
                        except Exception as _exc:
                            ui_utils.show_status(f"MD overlay failed: {_exc}")
            elif key == 'M' and mode == SEG:
                # Materialize the overlay's stage directions as real (untimed)
                # segs so they can be timed and saved.  Speakers are display-only
                # and are never committed — they stay derived from the overlay, so
                # there is only ever one source for a speaker header and nothing
                # can duplicate.  After inserting, refresh_overlay() re-derives:
                # the new stage_dir segs are now "materialized" and drop out of the
                # overlay, making a second M a no-op (idempotent).
                if not md_overlay:
                    ui_utils.show_status("No stage directions to commit — press m first.")
                else:
                    _sdir_items = [ov for ov in md_overlay if ov['kind'] == 'stage_dir']
                    if not _sdir_items:
                        ui_utils.show_status("No stage directions in the overlay to commit.")
                    else:
                        undo_stack.append(('snapshot', list(segs)))
                        # md_overlay is in ascending (before_si, order); inserting in
                        # reverse keeps earlier indices valid and preserves the order
                        # of directions that share a before_si.
                        for _ov in reversed(_sdir_items):
                            segs.insert(_ov['before_si'], _make_stage_dir(_ov['text']))
                        _committed = len(_sdir_items)
                        refresh_overlay()
                        viewport = 0
                        dirty    = True
                        ui_utils.show_status(
                            f"Committed {_committed} stage direction{'s' if _committed != 1 else ''}.")
            elif key == 't' and mode == SEG:
                if _HAS_VLC:
                    mode = TAP
                    start_s = segs[cursor].get("start") or play_pos
                    do_preview(start_s)
            elif key == 'b' and mode == SEG:
                if _HAS_VLC and mp is not None and segs:
                    mode = AUDITION
                    _aud_clips('line')   # landing on a line plays it whole
                elif not _HAS_VLC:
                    ui_utils.show_status("Audition needs VLC (not available).")
            elif key == 'w' and mode == SEG and source == SOURCE_TRANSCRIPT:
                if segs and cursor < len(segs) and segs[cursor].get("words"):
                    seg_cursor = cursor
                    mode = WORD; cursor = 0; viewport = 0
            elif key == 'ESC' and mode == WORD:
                mode = SEG; cursor = seg_cursor; viewport = max(0, seg_cursor - 2)
            elif key == 'e':
                if mode == SEG:
                    item = segs[cursor] if (segs and cursor < len(segs)) else None
                else:
                    words = cur_words()
                    item  = words[cursor] if cursor < len(words) else None
                if item is not None:
                    prev_mode = mode
                    mode      = EDIT
                    _edit_prefill()
            elif key == 'p':
                if playing:
                    do_stop()
                elif mode == WORD:
                    words = cur_words()
                    if cursor < len(words):
                        ww = words[cursor]
                        do_preview(ww.get("start") or 0.0)
                elif segs and cursor < len(segs):
                    do_preview(segs[cursor].get("start") or 0.0)
            elif key == 'J' and mode == SEG:
                if segs and cursor > 0:
                    undo_stack.append(('snapshot', list(segs)))
                    segs[cursor], segs[cursor - 1] = segs[cursor - 1], segs[cursor]
                    cursor -= 1
                    dirty = True
                    refresh_overlay()
            elif key == 'K' and mode == SEG:
                if segs and cursor < len(segs) - 1:
                    undo_stack.append(('snapshot', list(segs)))
                    segs[cursor], segs[cursor + 1] = segs[cursor + 1], segs[cursor]
                    cursor += 1
                    dirty = True
                    refresh_overlay()
            elif key == 'j' and mode == SEG:
                if segs and cursor < len(segs) - 1:
                    seg_a  = segs[cursor]
                    seg_b  = segs[cursor + 1]
                    if seg_a.get("kind") in ("dead_air", "stage_dir") or \
                       seg_b.get("kind") in ("dead_air", "stage_dir"):
                        ui_utils.show_status("Cannot join dead air or stage direction segments.")
                        continue
                    merged = {
                        "start": seg_a.get("start"),
                        "end":   seg_b.get("end"),
                        "text":  (seg_a.get("text", "").strip() + " " +
                                  seg_b.get("text", "").strip()).strip(),
                        "words": seg_a.get("words", []) + seg_b.get("words", []),
                    }
                    if seg_a.get("line_ref"):   # keep the pinned MD line through the join
                        merged["line_ref"] = seg_a["line_ref"]
                    undo_stack.append(('join', cursor, seg_a, seg_b))
                    segs[cursor:cursor + 2] = [merged]
                    dirty = True
                    refresh_overlay()
            elif key == 'x' and mode == WORD:
                words = cur_words()
                if 0 < cursor < len(words):
                    seg = segs[seg_cursor]
                    w_a, w_b = words[:cursor], words[cursor:]
                    boundary = (w_b[0].get("start") or w_a[-1].get("end")
                                or round((seg.get("start", 0) + seg.get("end", 0)) / 2, 3))
                    seg_a = {"start": seg.get("start"), "end": round(boundary, 3),
                             "text": " ".join(ww["word"] for ww in w_a), "words": w_a}
                    seg_b = {"start": round(boundary, 3), "end": seg.get("end"),
                             "text": " ".join(ww["word"] for ww in w_b), "words": w_b}
                    if seg.get("line_ref"):    # both halves stay on the split line's MD line
                        seg_a["line_ref"] = seg_b["line_ref"] = seg["line_ref"]
                    undo_stack.append(('split', seg_cursor, seg))
                    segs[seg_cursor:seg_cursor + 1] = [seg_a, seg_b]
                    dirty = True; mode = SEG; cursor = seg_cursor + 1
                    viewport = max(0, cursor - 2)
                    refresh_overlay()
            else:
                if mode == SEG:
                    tgts = sorted(selected) if selected else [cursor]
                    key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                    if key in key_deltas:
                        if segs[cursor].get("start") is None:
                            ui_utils.show_status("No timestamp set — press e to enter one.")
                        else:
                            apply_segs(tgts, key_deltas[key])
                else:
                    key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                    if key in key_deltas:
                        apply_word(seg_cursor, cursor, key_deltas[key])

    finally:
        if mp:
            try: mp.stop()
            except Exception: pass
        sys.stdout.write("\033[?1000l\033[?1006l")  # disable mouse
        sys.stdout.flush()
        _restore_term_attrs(fd, old)
        w.clear()
