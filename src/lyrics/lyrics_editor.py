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
import sys, os, json, time

from src.utils import ui_utils
from src.utils.ui_utils import Colors as C
from src.utils.prompt import (
    _Widget, _read_key, _wait_for_keypress,
    _set_raw, _restore_term_attrs, _get_term_attrs,
    _hint_lines, _rows, _cols,
)

_vlc = None
try:
    import vlc as _vlc  # type: ignore[import-untyped]
    _HAS_VLC = True
except ImportError:
    _HAS_VLC = False

from mutagen.id3 import ID3, ID3NoHeaderError  # type: ignore[reportPrivateImportUsage]

SOURCE_TRANSCRIPT = 'transcript'
SOURCE_SYLT       = 'sylt'
SOURCE_USLT       = 'uslt'
SEG, WORD, EDIT, TAP = 'seg', 'word', 'edit', 'tap'

_TAP_HEADER_MAX_LEN = 38


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


def _parse(s: str) -> float | None:
    s = s.strip()
    try:
        if ':' in s:
            m, rest = s.split(':', 1)
            return int(m) * 60 + float(rest)
        return float(s)
    except (ValueError, IndexError):
        return None


def _find_transcript(mp3_path: str) -> str | None:
    base = os.path.splitext(os.path.basename(mp3_path))[0]
    p = os.path.join(os.path.dirname(mp3_path), "Transcript", f"{base}.json")
    return p if os.path.isfile(p) else None


def _rebuild_srt(segs: list) -> str:
    blocks = []
    for i, seg in enumerate(segs, 1):
        txt = seg.get("text", "").strip()
        s   = seg.get("start")
        e   = seg.get("end")
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
        with open(jpath, encoding='utf-8') as f:
            data = json.load(f)
        return data['segments'], SOURCE_TRANSCRIPT, {'jpath': jpath, 'data': data, 'mp3': mp3_path}
    try:
        from src.lyrics.lyrics import normalize_lyric_newlines
        audio = ID3(mp3_path)

        sylt = audio.getall('SYLT')
        if sylt:
            entries = sylt[0].text  # [(text, ms), ...]
            segs = []
            for i, (text, start_ms) in enumerate(entries):
                end_ms = entries[i + 1][1] if i + 1 < len(entries) else start_ms + 5000
                segs.append({'text': text,
                             'start': round(start_ms / 1000.0, 3),
                             'end':   round(end_ms   / 1000.0, 3),
                             'words': []})
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
          edit_field, edit_buf, source, total_s) -> tuple[list[str], int]:
    cols = _cols()
    rows = _rows()
    n    = len(segs)
    vp   = viewport

    show_words = mode == WORD or (mode == EDIT and prev_mode == WORD)

    label = ("TAP SYNC"    if mode == TAP
             else "TRANSCRIPT" if source == SOURCE_TRANSCRIPT
             else "LYRICS")
    dot  = f"  {C.ACCENT}●{C.RESET}" if dirty else ""
    play = f"  {C.ACCENT}▶ {_fmt(play_pos)}{C.RESET}" if playing else ""

    if show_words and segs and seg_cursor < len(segs):
        raw = segs[seg_cursor].get("text", "").strip()
        sub = (raw[:_TAP_HEADER_MAX_LEN] + "…") if len(raw) > _TAP_HEADER_MAX_LEN else raw
        ctx = f"  {C.DIM}›  {sub}{C.RESET}"
    else:
        ctx = ""

    pos_str = f"{cursor + 1} / {n}" if n else "–"
    out: list[str] = [
        "",
        f"  {C.BOLD}{label}{C.RESET}  {C.DIM}{track_name}{C.RESET}{ctx}{dot}{play}  {C.DIM}{pos_str}{C.RESET}",
        f"{C.DIM}{'·' * cols}{C.RESET}",
    ]

    sep = f"{C.DIM}{'─' * cols}{C.RESET}"

    if mode == TAP:
        prev_s = segs[cursor - 1] if cursor > 0     else None
        curr_s = segs[cursor]     if cursor < n      else None
        next_s = segs[cursor + 1] if cursor < n - 1  else None

        def _tap_line(seg, bold: bool = False, arrow: bool = False) -> str:
            ptr = f"  {C.ACCENT}▶{C.RESET}" if arrow else "   "
            if seg is None:
                return f"{ptr}  {C.DIM}──{C.RESET}"
            ts  = f"{C.DIM}{_fmt(seg['start'])}  {C.RESET}" if seg.get("start") is not None else " " * 12
            txt = seg["text"].strip()[:cols - 22]
            col = C.BOLD + C.PRIMARY if bold else C.DIM
            return f"{ptr}  {ts}{col}{txt}{C.RESET}"

        FTR      = 2
        TAP_ROWS = 9  # 3 content lines + 4 blank spacers + progress + blank
        n_body   = max(0, rows - 1 - 3 - FTR)
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
            bar = ui_utils.get_progress_bar(pct, cols - 24)
            out.append(f"  {C.DIM}{bar}  {_fmt(play_pos)} / {_fmt(total_s)}{C.RESET}")
        else:
            out.append("")

        footer: list[str] = [sep]
        pairs: list[tuple[str, str]] = [('spc/↵', 'mark')]
        if cursor > 0: pairs.append(('←→', '±0.25s'))
        pairs.append(('p', 'pause' if playing else 'play'))
        pairs += [('esc', 'done'), ('q', 'quit')]
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        footer.extend(_hint_lines(*pairs))

        padding = max(0, (rows - 1) - len(out) - len(footer))
        return out + [""] * padding + footer, vp

    if show_words:
        items  = segs[seg_cursor].get("words", []) if segs else []
        ITEM_H = 1
        FTR    = 2 if mode != EDIT else 3
    else:
        items  = segs
        ITEM_H = 3
        FTR    = 3 if mode == EDIT else 2

    n_items = len(items)
    vis     = max(1, (rows - 1 - 3 - 1 - 1 - FTR) // ITEM_H)

    if cursor < vp:          vp = cursor
    if cursor >= vp + vis:   vp = cursor - vis + 1
    vp = max(0, min(vp, max(0, n_items - vis)))

    out.append(f"  {C.DIM}↑  {vp} above{C.RESET}" if vp > 0 else "")

    for slot in range(vis):
        i = vp + slot
        if i >= n_items: break

        item   = items[i]
        is_cur = (i == cursor)
        is_sel = (mode == SEG and i in selected)
        s_t    = item.get("start")
        e_t    = item.get("end")

        if show_words:
            word = item.get("word", "").strip()
            ts   = f"{_fmt(s_t)}  →  {_fmt(e_t)}"
            ptr  = f"{C.ACCENT}›{C.RESET}" if is_cur else " "
            ts_c = C.BOLD if is_cur else C.DIM
            out.append(f"  {ptr}    {ts_c}{ts}   {word}{C.RESET}")
        else:
            ts_str = f"{_fmt(s_t)}  →  {_fmt(e_t)}"
            text_r = item.get("text", "").strip()
            text   = (text_r[:cols - 10] + "…") if len(text_r) > cols - 10 else text_r
            ptr    = f"{C.ACCENT}›{C.RESET}" if is_cur else " "
            chk    = f"{C.ACCENT}✔{C.RESET}" if is_sel else " "
            ts_c   = C.BOLD    if is_cur else C.DIM
            tx_c   = C.PRIMARY if is_cur else C.DIM
            dur    = (f"  {C.DIM}{(e_t or 0) - (s_t or 0):.2f}s{C.RESET}"
                      if is_cur and s_t is not None and e_t is not None else "")
            out.append(f"  {ptr} {chk}  {ts_c}{ts_str}{C.RESET}{dur}")
            out.append(f"       {tx_c}{text}{C.RESET}")
            out.append("")

    below = n_items - (vp + vis)
    out.append(f"  {C.DIM}↓  {below} below{C.RESET}" if below > 0 else "")

    footer = [sep]

    if mode == EDIT:
        item = items[cursor] if (items and cursor < len(items)) else None
        if item:
            sv  = item.get("start")
            ev  = item.get("end")
            buf = "".join(edit_buf)
            s_d = (f"{C.ACCENT}{C.BOLD}{buf}█{C.RESET}" if edit_field == 'start'
                   else f"{C.DIM}{_fmt(sv)}{C.RESET}")
            e_d = (f"{C.DIM}{_fmt(ev)}{C.RESET}" if edit_field == 'start'
                   else f"{C.ACCENT}{C.BOLD}{buf}█{C.RESET}")
            footer.append(f"  {C.ACCENT}✎{C.RESET}  start  {s_d}    end  {e_d}")
        footer.extend(_hint_lines(('tab', 'switch field'), ('↵', 'apply'), ('esc', 'cancel')))

    elif mode == WORD:
        pairs = []
        if _HAS_VLC: pairs.append(('p', 'preview'))
        pairs += [('↑↓', 'navigate'), ('←→', '±0.25s'), (',/.', '±0.1s'), ('[/]', '±1s'),
                  ('x', 'split'), ('e', 'edit'), ('esc', 'back'), ('s', 'save'), ('q', 'quit')]
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        footer.extend(_hint_lines(*pairs))

    else:  # SEG
        pairs = []
        if _HAS_VLC: pairs.append(('p', 'preview'))
        pairs += [('↑↓', 'navigate'), ('←→', '±0.25s'), (',/.', '±0.1s'), ('[/]', '±1s')]
        if _HAS_VLC:                           pairs.append(('t', 'tap sync'))
        if source == SOURCE_TRANSCRIPT:         pairs.append(('w', 'words'))
        pairs += [('j', 'join↓'), ('e', 'edit'), ('spc', 'mark'), ('s', 'save'), ('q', 'quit')]
        if undo_depth: pairs.append(('u', f'undo ×{undo_depth}'))
        if selected:   pairs.append(('', f'{len(selected)} marked'))
        footer.extend(_hint_lines(*pairs))

    padding = max(0, (rows - 1) - len(out) - len(footer))
    return out + [""] * padding + footer, vp


def lyrics_editor(mp3_path: str) -> None:
    result = _load(mp3_path)
    if result is None:
        ui_utils.show_status("No lyrics or transcript found for this track.")
        return

    segs, source, aux = result
    track_name = os.path.basename(os.path.dirname(mp3_path))

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
    undo_stack: list = []

    edit_field = 'start'
    edit_buf: list[str] = []

    playing    = False
    play_until = 0.0
    play_pos   = 0.0

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

    def do_preview(start_s: float) -> None:
        nonlocal playing, play_until, play_pos
        if mp is None: return
        mp.set_time(int(start_s * 1000))
        if not mp.is_playing():
            mp.play(); time.sleep(0.15)
        playing    = True
        play_until = float('inf')
        play_pos   = start_s

    def do_stop() -> None:
        nonlocal playing
        if mp and mp.is_playing(): mp.pause()
        playing = False

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
        elif op[0] == 'tap':
            idx, old_start, old_prev_end = op[1], op[2], op[3]
            segs[idx]["start"] = old_start
            if idx > 0 and old_prev_end is None:
                segs[idx - 1]["end"] = None
            cursor = idx
        dirty = bool(undo_stack)

    def do_save() -> None:
        nonlocal dirty
        if source == SOURCE_TRANSCRIPT:
            aux['data']['word_segments'] = [
                {'word': ww['word'], 'start': ww.get('start'),
                 'end': ww.get('end'), 'score': ww.get('score')}
                for seg in segs for ww in seg.get('words', [])
            ]
            jpath = aux['jpath']
            with open(jpath, 'w', encoding='utf-8') as f:
                json.dump(aux['data'], f, indent=2, ensure_ascii=False)
            with open(jpath.replace('.json', '.srt'), 'w', encoding='utf-8') as f:
                f.write(_rebuild_srt(segs))
        else:
            from src.lyrics.lyric_timer import save_sylt_entries
            entries = [(s['text'], max(0, int((s['start'] or 0) * 1000)))
                       for s in segs if s.get('start') is not None]
            save_sylt_entries(aux['mp3'], entries)
        n_timed = sum(1 for s in segs if s.get('start') is not None)
        ui_utils.show_status(f"Saved — {n_timed} timed lines")
        dirty = False; undo_stack.clear()

    def commit_field(field: str, val: float) -> None:
        nonlocal dirty
        if prev_mode == SEG:
            old_val = segs[cursor].get(field) or 0.0
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

    def prefill(field: str) -> None:
        if prev_mode == SEG:
            v = (segs[cursor].get(field) or 0.0) if segs else 0.0
        else:
            words = cur_words()
            v = (words[cursor].get(field) or 0.0) if cursor < len(words) else 0.0
        edit_buf[:] = list(_fmt(v))

    try:
        _set_raw(fd)
        need_redraw = True

        while True:
            n = len(segs)

            total_s: float = 0.0
            if mp and mp.get_length() > 0:
                total_s = mp.get_length() / 1000.0
            elif segs and segs[-1].get("end") is not None:
                total_s = float(segs[-1]["end"])

            if playing:
                if mp and not mp.is_playing():
                    playing = False; need_redraw = True
                elif time.time() > play_until:
                    do_stop(); need_redraw = True
                elif mp:
                    pos = mp.get_time() / 1000.0
                    if abs(pos - play_pos) > 0.05:
                        play_pos = pos; need_redraw = True
                    # auto-scroll cursor in SEG/WORD/TAP
                    if mode != EDIT:
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
                lines, viewport = _draw(
                    segs, cursor, seg_cursor, mode, prev_mode, selected,
                    viewport, dirty, len(undo_stack), track_name,
                    playing, play_pos, edit_field, edit_buf, source, total_s,
                )
                w.render(lines)
                need_redraw = False

            if not _wait_for_keypress(0.05):
                continue
            key = _read_key(fd)
            need_redraw = True

            if mode == EDIT:
                if key == 'ESC':
                    mode = prev_mode; edit_buf.clear()
                elif key == 'ENTER':
                    val = _parse("".join(edit_buf))
                    if val is not None and val >= 0:
                        commit_field(edit_field, val)
                    mode = prev_mode; edit_buf.clear()
                elif key == 'TAB':
                    val = _parse("".join(edit_buf))
                    if val is not None and val >= 0:
                        commit_field(edit_field, val)
                    edit_field = 'end' if edit_field == 'start' else 'start'
                    prefill(edit_field)
                elif key == 'BACKSPACE':
                    if edit_buf: edit_buf.pop()
                elif len(key) == 1 and (key.isdigit() or key in ':.'):
                    edit_buf.append(key)
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

            items = segs if mode == SEG else cur_words()
            n_i   = len(items)

            if key in ('q', 'CTRL_C'):
                if playing: do_stop()
                break
            elif key == 's':
                do_save()
            elif key == 'u':
                do_undo()
            elif key == 'UP':
                cursor = max(0, cursor - 1)
            elif key == 'DOWN':
                cursor = min(n_i - 1, max(0, cursor + 1))
            elif key == 'SPACE' and mode == SEG:
                selected.symmetric_difference_update({cursor})
            elif key == 't' and mode == SEG:
                if _HAS_VLC:
                    mode = TAP
                    start_s = segs[cursor].get("start") or play_pos
                    do_preview(start_s)
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
                    prev_mode  = mode
                    mode       = EDIT
                    edit_field = 'start'
                    prefill('start')
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
            elif key == 'j' and mode == SEG:
                if segs and cursor < len(segs) - 1:
                    seg_a  = segs[cursor]
                    seg_b  = segs[cursor + 1]
                    merged = {
                        "start": seg_a.get("start"),
                        "end":   seg_b.get("end"),
                        "text":  (seg_a.get("text", "").strip() + " " +
                                  seg_b.get("text", "").strip()).strip(),
                        "words": seg_a.get("words", []) + seg_b.get("words", []),
                    }
                    undo_stack.append(('join', cursor, seg_a, seg_b))
                    segs[cursor:cursor + 2] = [merged]
                    dirty = True
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
                    undo_stack.append(('split', seg_cursor, seg))
                    segs[seg_cursor:seg_cursor + 1] = [seg_a, seg_b]
                    dirty = True; mode = SEG; cursor = seg_cursor + 1
                    viewport = max(0, cursor - 2)
            else:
                if mode == SEG:
                    tgts = sorted(selected) if selected else [cursor]
                    key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                    if key in key_deltas:
                        apply_segs(tgts, key_deltas[key])
                else:
                    key_deltas = {'LEFT': -0.25, 'RIGHT': 0.25, ',': -0.1, '.': 0.1, '[': -1.0, ']': 1.0}
                    if key in key_deltas:
                        apply_word(seg_cursor, cursor, key_deltas[key])

    finally:
        if mp:
            try: mp.stop()
            except Exception: pass
        _restore_term_attrs(fd, old)
        w.clear()
