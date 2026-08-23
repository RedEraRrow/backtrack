"""The interactive player *view* over the shared PlaybackSession (feature #14).

Audio ownership, the queue, and the track lifecycle live in ``session.py``; this
module is the foreground renderer/controller. Opening a track starts the shared
session and attaches this view; **leaving the view (b/Esc) keeps the session
playing in the background** — only Stop (s) ends it. The session's background
tick advances the queue and logs history whether or not this view is attached.
"""
from __future__ import annotations
import sys
import time

from src.utils import ui_utils
from src.art.album_art import get_art_from_mp3
from src.lyrics.lyrics import (
    _parse_sylt,
    _parse_uslt,
    build_uslt_line_times,
    draw_lyric_initial,
    draw_lyric_window,
    draw_uslt_window,
    draw_dialogue_window,
    estimate_sylt_last_line_end,
    expand_uslt_lines,
    find_current_uslt_line,
    find_uslt_handoff_index,
    DialoguePlaybackState,
)
from src.playback.playback_ui import (
    _controls_line,
    _layout_mode,
    ART_MAX_WIDTH,
    draw_full_ui,
    update_progress_ui,
    _ui_state,
    toggle_metadata,
    toggle_help,
    cycle_right_pane,
)
from src.playback import playback_ui
from src.playback.session import (
    SESSION, is_client, has_other_windows,
)
from src.utils.terminal_input import (
    clear_escape_buffer,
    get_key_non_blocking,
    is_arrow_key,
    raw_mode,
)

_KEY_POLL_INTERVAL_S = 0.05
_LOOP_TICK_S = 0.02

# Translate a clicked hint's synthesised key (from add_hint_click_cells, which
# speaks the menu vocabulary) into what the player's key switch expects: arrows as
# raw escape sequences, space/enter as their characters. Anything else passes
# through unchanged (plain letters/symbols already match).
_PLAYER_SYNTH = {
    'UP': '\x1b[A', 'DOWN': '\x1b[B', 'LEFT': '\x1b[D', 'RIGHT': '\x1b[C',
    'SPACE': ' ', 'ENTER': '\n',
}


def _render_grouping_cover(file_path: str, cols: int) -> str:
    """Render the booklet/cover image for a grouping (audiobook-style) file, sized to the layout mode."""
    mode = _layout_mode(cols)
    if mode == 'wide':
        art_w = min(cols // 2, ART_MAX_WIDTH)
    elif mode == 'standard':
        art_w = min(cols, ART_MAX_WIDTH)
    else:
        art_w = min(45, max(1, cols))
    return get_art_from_mp3(file_path, art_w, preferred_desc='Booklet', preferred_type=6)


def _show_load_error(file_path: str) -> None:
    """Show a 'could not load' screen and wait for a keypress."""
    ui_utils.clear_screen()
    sys.stdout.write("\033[1;31mPlayback Error:\033[0m Could not load:\n")
    sys.stdout.write(f" → {file_path}\n\n")
    sys.stdout.write("Press any key to return...")
    sys.stdout.flush()
    with raw_mode(sys.stdin):
        while not get_key_non_blocking():
            time.sleep(_KEY_POLL_INTERVAL_S)


def music_player(file_path: str, is_grouping: bool = False, preloaded_data: dict | None = None,
                 queue_titles: list[str] | None = None, queue_index: int = 0,
                 queue_paths: list[str] | None = None, mode: str | None = None) -> dict:
    """Start the shared session on ``file_path`` (with its queue) and open the
    player view. Kept name/signature for existing callers; audio now persists in
    the background after the view is left (only Stop ends it). Returns a status
    dict: ``DETACH`` (minimised, still playing), ``STOP``, ``QUIT_ALL``, ``OK``
    (queue finished), or ``ERROR``."""
    paths = queue_paths if queue_paths else [file_path]
    # In a joined (client) window the audio lives in the host process: send the
    # play there and stay in this window's menus (the host's now-playing box
    # updates via the mirror). The full player view from a client is Phase 2c.
    if is_client():
        from src.playback.session import active_session
        active_session().start(file_path, queue=paths, titles=queue_titles,
                               index=queue_index, mode=mode, is_grouping=is_grouping)
        ui_utils.show_status("▶ Sent to the session host.")
        return {"status": "OK"}
    ok = SESSION.start(file_path, queue=paths, titles=queue_titles, index=queue_index,
                       mode=mode, is_grouping=is_grouping)
    if not ok:
        _show_load_error(file_path)
        return {"status": "ERROR"}
    SESSION.start_background_tick()
    return open_player_view()


def open_player_view() -> dict:
    """Render + control the current session in the foreground until the user
    leaves. Does NOT stop the session on ``DETACH`` — audio keeps playing.
    Claims the cross-window view lock; if another window holds it, just detaches."""
    from src.playback.session import my_token
    if not SESSION.is_active():
        return {"status": "OK"}
    if not SESSION.acquire_view(my_token()):
        return {"status": "DETACH"}          # a client window has the player open
    SESSION.view_attached = True
    try:
        return _player_view_loop()
    finally:
        SESSION.view_attached = False
        SESSION.release_view(my_token())
        ui_utils.clear_screen()


def open_client_player_view() -> dict:
    """Full player view for a *joined* window (#14 Phase 2c): renders the host's
    current track from its snapshots + the track file on the shared disk, with
    transport routed to the host. Elapsed is interpolated between snapshots for a
    smooth progress bar. (Lyrics on a client are a later addition.)"""
    from src.playback import session as sess
    from mutagen.id3 import ID3

    remote = sess.active_session()
    np0 = remote.now_playing()
    if np0 is None:
        return {"status": "OK"}
    token = sess.my_token()
    # Exactly one player view may exist across the whole session. If another
    # window already holds it, don't open a second one (#14).
    holder = np0.get('view_holder')
    if holder and holder != token:
        ui_utils.show_status("The player is open in another window.")
        return {"status": "DETACH"}
    remote.acquire_view(token)

    last_sig = None
    audio = None
    duration = 0.0
    prog_row = 0
    ctrl_row = 0
    toast = ""
    toast_expiry = 0.0
    width = ui_utils.get_terminal_size()[0]
    try:
        with raw_mode(sys.stdin):
            sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse
            sys.stdout.flush()
            while True:
                np = remote.now_playing()
                if np is None or not np.get('file_path'):
                    return {"status": "OK"}          # host stopped / no track
                # Another window won a simultaneous grab or took the view — never
                # show a second player; step back to the mirror (#14).
                if np.get('view_holder') not in (None, token):
                    return {"status": "DETACH"}
                if toast and time.time() >= toast_expiry:
                    toast = ""; last_sig = None       # clear an expired message
                fp = np['file_path']
                size = ui_utils.get_terminal_size()
                playback_ui.set_queue_context(np.get('titles') or [], int(np.get('index') or 0), np.get('queue') or [])
                sig = (
                    fp,
                    np.get('paused'),
                    np.get('volume'),
                    np.get('generation'),
                    int(np.get('index') or 0),
                    len(np.get('queue') or []),
                    tuple(np.get('titles') or []),
                    size,
                )
                if sig != last_sig:
                    if last_sig is None or fp != last_sig[0] or np.get('generation') != last_sig[3]:
                        try:
                            audio = ID3(fp)
                        except Exception:
                            audio = None
                    duration = float(np.get('duration') or 0.0)
                    prog_row, ctrl_row, _lr, width, _br = draw_full_ui(
                        fp, audio, None, size, is_paused=bool(np.get('paused')),
                        volume=int(np.get('volume') or 0), toast=toast)
                    last_sig = sig

                elapsed = float(np.get('elapsed') or 0.0)
                if not np.get('paused'):
                    elapsed += max(0.0, time.time() - (remote.latest_at() or time.time()))
                if duration:
                    elapsed = min(elapsed, duration)
                update_progress_ui(prog_row, elapsed, duration, width)

                key = get_key_non_blocking()
                if key:
                    clear_escape_buffer()
                    arrow = is_arrow_key(key)
                    if key.startswith('MOUSE_CLICK:'):
                        _mp = key.split(':'); _mr = int(_mp[2]); _mc = int(_mp[3])
                        _act = playback_ui.transport_click_action(_mr, _mc, ctrl_row)
                        if _act == 'prev':
                            key = '['
                        elif _act == 'next':
                            key = ']'
                        elif _act == 'playpause':
                            key = ' '
                        else:
                            _vol = playback_ui.volume_from_click(_mr, _mc)
                            if _vol is not None:
                                remote.set_volume(_vol); key = ''
                            else:
                                _hk = playback_ui.hint_click_key(_mr, _mc)
                                key = _PLAYER_SYNTH.get(_hk, _hk) if _hk else ''
                        arrow = is_arrow_key(key)
                    if key == 'FOCUS_IN':
                        last_sig = None                # force a full redraw
                    elif key in (' ', 'p', 'P'):
                        remote.pause_toggle()
                    elif arrow == 'C':
                        remote.seek(5)
                    elif arrow == 'D':
                        remote.seek(-5)
                    elif key == ',':
                        remote.seek(-30)
                    elif key == '.':
                        remote.seek(30)
                    elif key.lower() == 'e':
                        remote.seek((duration - 35) - elapsed)
                    elif key.lower() == 'j':
                        remote.seek(-1)
                    elif key.lower() == 'l':
                        remote.seek(1)
                    elif key == ']':
                        remote.next()
                    elif key == '[':
                        remote.prev()
                    elif key in ('=', '+'):
                        remote.set_volume(min(100, remote.get_volume() + 5))
                    elif key in ('-', '_'):
                        remote.set_volume(max(0, remote.get_volume() - 5))
                    elif key in ('b', 'B') or key == '\x1b':
                        # Pinned open while another window browses this session
                        # (#14) — the two windows stay specialised until one closes.
                        if has_other_windows():
                            toast = 'Close the other window to leave the player'
                            toast_expiry = time.time() + 2.0
                            last_sig = None
                        else:
                            return {"status": "DETACH"}
                    elif key.lower() == 's':
                        remote.stop()
                        return {"status": "STOP"}
                    elif key.lower() == 'q':
                        return {"status": "QUIT_ALL"}
                time.sleep(_LOOP_TICK_S)
    finally:
        sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse on exit
        sys.stdout.flush()
        remote.release_view(token)
        ui_utils.clear_screen()


def _player_view_loop() -> dict:
    """The foreground render + input loop for the attached session."""
    mp = SESSION.mp
    assert mp is not None

    # --- per-track render state (rebuilt by _prepare() on every track) ---
    audio = None
    duration = 0.0
    pre_art = None
    sylt_data: list = []
    uslt_lines: list[str] = []
    line_times: list = []
    is_uslt = False
    has_uslt = False
    has_credits = False
    has_lyrics = False
    dialogue_state = None
    sylt_handoff_end_s: float | None = None
    uslt_handoff_idx = 0

    # --- view / loop state ---
    manual_line_index = None
    arrow_key_time = None
    uslt_time_offset = 0.0
    toast_text = ""
    toast_expiry = 0.0
    last_size = ui_utils.get_terminal_size()
    last_lyric_idx = -1
    resize_pending = False
    resize_timer = 0.0
    pending_size = last_size
    in_uslt_tail = False
    prog_row = ctrl_row = lyric_row = art_bottom_row = 0
    current_width = last_size[0]
    exp_lines: list = []
    exp_times: list = []
    last_q_sig: tuple | None = None

    def _prepare() -> None:
        """(Re)load per-track render state from the session for the current track."""
        nonlocal audio, duration, pre_art, sylt_data, uslt_lines, line_times
        nonlocal is_uslt, has_uslt, has_credits, has_lyrics, dialogue_state
        nonlocal sylt_handoff_end_s, uslt_handoff_idx, in_uslt_tail, uslt_time_offset
        nonlocal toast_text, toast_expiry
        fp = SESSION.file_path or ""
        audio = SESSION.audio
        duration = SESSION.duration
        # Keep the in-player queue pane ('w' cycle) in sync with the session queue.
        playback_ui.set_queue_context(SESSION.titles, SESSION.index, SESSION.queue)
        pre_art = _render_grouping_cover(fp, last_size[0]) if SESSION.is_grouping else None
        dialogue_state = DialoguePlaybackState(fp, track_duration=duration)
        has_credits = bool(audio and (audio.getall('TMCL') or audio.getall('TIPL')))

        sylt_data = _parse_sylt(audio) if audio else []
        uslt_lines_raw = _parse_uslt(audio) if audio else []
        uslt_lines = [line for line, _ in uslt_lines_raw]
        is_uslt = False
        has_uslt = bool(uslt_lines)
        sylt_handoff_end_s = None
        uslt_handoff_idx = 0
        line_times = []
        if not sylt_data and uslt_lines_raw:
            is_uslt = True
            sylt_data = uslt_lines_raw
            line_times = build_uslt_line_times(uslt_lines, duration)
        has_lyrics = bool(sylt_data) or has_uslt or dialogue_state.is_active()
        in_uslt_tail = False
        uslt_time_offset = 0.0
        if sylt_data and not is_uslt and has_uslt:
            sylt_handoff_end_s = estimate_sylt_last_line_end(sylt_data, duration * 1000)
            uslt_handoff_idx = find_uslt_handoff_index(uslt_lines, sylt_data[-1][0])

        if audio and audio.getall('EQU2'):
            toast_text = "♫ Equaliser applied"
            toast_expiry = time.time() + 2.5

    def _redraw_full() -> None:
        """Full-screen redraw for the current track + view state; sets row positions."""
        nonlocal prog_row, ctrl_row, lyric_row, current_width, art_bottom_row
        nonlocal last_lyric_idx, exp_lines, exp_times
        vol = SESSION.get_volume()
        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
            SESSION.file_path or "", audio, pre_art, last_size,
            is_paused=SESSION.is_paused(), volume=vol,
            toast=toast_text if time.time() < toast_expiry else "",
        )
        last_lyric_idx = -1
        if is_uslt or in_uslt_tail:
            _wrap_w = max(20, current_width - 8)
            exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)
        else:
            exp_lines, exp_times = [], []

        if _ui_state['show_lyrics']:
            right_col = playback_ui._last_right_left or 1
            right_w = playback_ui._last_right_width or current_width
            if dialogue_state and dialogue_state.is_active():
                dialogue_state.update(0.0)
                draw_dialogue_window(
                    row=lyric_row, dialogue_lines=dialogue_state.expanded_chunks,
                    current_idx=dialogue_state.current_idx, width=right_w, max_row=last_size[1],
                    col=right_col, bottom_row=art_bottom_row, line_times=dialogue_state.line_times)
            elif sylt_data or has_uslt:
                first_line = sylt_data[0][0] if sylt_data else (uslt_lines[0] if uslt_lines else "")
                draw_lyric_initial(
                    lyric_row, first_line, width=right_w, max_row=last_size[1],
                    col=right_col, bottom_row=art_bottom_row)

    def update_ctrl_ui() -> None:
        """Redraw just the transport/status line in place (see #86)."""
        active_toast = toast_text if time.time() < toast_expiry else ""
        status_ln, _ = _controls_line(
            is_uslt or in_uslt_tail, SESSION.is_paused(), SESSION.get_volume(), active_toast,
            has_lyrics=has_lyrics, has_credits=has_credits)
        sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}")
        sys.stdout.flush()

    with raw_mode(sys.stdin):
        sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse (click + scroll)
        sys.stdout.flush()
        _prepare()
        _redraw_full()
        last_track_sig = (SESSION.generation, SESSION.file_path)

        try:
          while True:
            if not SESSION.is_active():
                return {"status": "OK"}

            current_size = ui_utils.get_terminal_size()
            if current_size != last_size:
                if not resize_pending:
                    resize_pending = True
                    pending_size = current_size
                    resize_timer = time.time()
                elif current_size != pending_size:
                    pending_size = current_size
                    resize_timer = time.time()
            if resize_pending and (time.time() - resize_timer > 0.15):
                last_size = pending_size
                resize_pending = False
                _redraw_full()

            if toast_text and time.time() >= toast_expiry:
                toast_text = ""
                update_ctrl_ui()

            current_track_sig = (SESSION.generation, SESSION.file_path)
            if current_track_sig != last_track_sig:
                last_track_sig = current_track_sig
                _prepare()
                _redraw_full()
                continue

            # End-of-track / auto-advance is owned by the session.
            adv = SESSION.tick()
            if adv == 'stopped':
                return {"status": "OK"}
            if adv == 'changed':
                last_track_sig = (SESSION.generation, SESSION.file_path)
                _prepare()
                _redraw_full()
                continue

            # Live-refresh the queue pane when the queue changes mid-track (e.g.
            # another window queued a song) — not only on track change.
            q_sig = (tuple(SESSION.titles), SESSION.index)
            if q_sig != last_q_sig:
                last_q_sig = q_sig
                playback_ui.set_queue_context(SESSION.titles, SESSION.index, SESSION.queue)
                if _ui_state.get('show_queue'):
                    _redraw_full()

            elapsed_ms = mp.get_time()
            elapsed = elapsed_ms / 1000.0 if elapsed_ms >= 0 else 0.0

            key = get_key_non_blocking()
            if key:
                clear_escape_buffer()
                arrow = is_arrow_key(key)

                if key.startswith('MOUSE_CLICK:'):
                    # Map clicks on the transport icons, volume bar, or hint
                    # glyphs to the equivalent key, then let the switch handle it.
                    _mp = key.split(':'); _mr = int(_mp[2]); _mc = int(_mp[3])
                    _act = playback_ui.transport_click_action(_mr, _mc, ctrl_row)
                    if _act == 'prev':
                        key = '['
                    elif _act == 'next':
                        key = ']'
                    elif _act == 'playpause':
                        key = ' '
                    else:
                        _vol = playback_ui.volume_from_click(_mr, _mc)
                        if _vol is not None:
                            v = SESSION.set_volume(_vol)
                            toast_text = f'Volume: {v}%'; toast_expiry = time.time() + 1.0
                            playback_ui.draw_volume_bar(v); update_ctrl_ui()
                            key = ''
                        else:
                            _hk = playback_ui.hint_click_key(_mr, _mc)
                            key = _PLAYER_SYNTH.get(_hk, _hk) if _hk else ''
                    arrow = is_arrow_key(key)

                if key == 'FOCUS_OUT':
                    pass
                elif key == 'FOCUS_IN':
                    _redraw_full()
                elif key in (' ', 'p', 'P'):
                    SESSION.pause_toggle()
                    time.sleep(_KEY_POLL_INTERVAL_S)
                    update_ctrl_ui()
                    last_lyric_idx = -1
                elif arrow == 'C':
                    SESSION.seek(5)
                    toast_text = 'Seek Forward +5s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif arrow == 'D':
                    SESSION.seek(-5)
                    toast_text = 'Seek Backward -5s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif arrow in ('A', 'B') and (is_uslt or in_uslt_tail):
                    current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)
                    target_idx = max(0, current_idx - 1) if arrow == 'A' else min(len(exp_times) - 1, current_idx + 1)
                    uslt_time_offset = exp_times[target_idx][0] - elapsed
                    manual_line_index = target_idx
                    arrow_key_time = time.time()
                elif key == ',':
                    SESSION.seek(-30)
                    toast_text = 'Seek Backward -30s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif key == '.':
                    SESSION.seek(30)
                    toast_text = 'Seek Forward +30s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif key.lower() == 'e':          # TEMP: jump to last 35 seconds
                    SESSION.seek((duration - 35) - elapsed)
                    toast_text = 'Skip to last 35s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif key.lower() == 'j':
                    SESSION.seek(-1)
                    toast_text = 'Seek Backward -1s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif key.lower() == 'l':
                    SESSION.seek(1)
                    toast_text = 'Seek Forward +1s'; toast_expiry = time.time() + 1.0
                    update_ctrl_ui()
                elif key == ']':                  # NEXT track (skip), stay in the view
                    if SESSION.next(manual=True) is None:
                        return {"status": "OK"}   # was the last track — queue finished
                    _prepare(); _redraw_full(); continue
                elif key == '[':                  # PREVIOUS track (or restart current)
                    if SESSION.prev() is not None:
                        _prepare(); _redraw_full()
                    continue
                elif key in ('b', 'B') or key == '\x1b':   # MINIMISE — keep playing (#14)
                    # Pinned open while another window is browsing this session:
                    # the two windows stay specialised until one closes (#14).
                    if has_other_windows():
                        toast_text = 'Close the other window to leave the player'
                        toast_expiry = time.time() + 2.0
                        update_ctrl_ui(); continue
                    return {"status": "DETACH"}
                elif key.lower() == 's':          # STOP playback
                    SESSION.stop()
                    return {"status": "STOP"}
                elif key.lower() == 'q':          # QUIT the app
                    return {"status": "QUIT_ALL"}
                elif key in ('=', '+'):
                    v = SESSION.set_volume(SESSION.get_volume() + 5)
                    toast_text = f'Volume: {v}%'; toast_expiry = time.time() + 1.0
                    playback_ui.draw_volume_bar(v)
                    update_ctrl_ui()
                elif key in ('-', '_'):
                    v = SESSION.set_volume(SESSION.get_volume() - 5)
                    toast_text = f'Volume: {v}%'; toast_expiry = time.time() + 1.0
                    playback_ui.draw_volume_bar(v)
                    update_ctrl_ui()
                elif key.lower() == 'i':
                    ui_utils.clear_screen()
                    toggle_help()
                    _redraw_full()
                elif key.lower() == 'w':
                    ui_utils.clear_screen()
                    cycle_right_pane(has_lyrics, has_credits, playback_ui.has_queue())
                    _redraw_full()
                elif key.lower() == 'm':
                    ui_utils.clear_screen()
                    toggle_metadata()
                    _redraw_full()

            update_progress_ui(prog_row, elapsed, duration, current_width)

            if _ui_state.get('show_lyrics', True) and not _ui_state.get('show_queue'):
                if dialogue_state and dialogue_state.is_active():
                    right_col = playback_ui._last_right_left or 1
                    right_w = playback_ui._last_right_width or current_width
                    dialogue_state.update(elapsed=elapsed)
                    if dialogue_state.current_idx != last_lyric_idx:
                        draw_dialogue_window(
                            row=lyric_row, dialogue_lines=dialogue_state.expanded_chunks,
                            current_idx=dialogue_state.current_idx, width=right_w, max_row=last_size[1],
                            col=right_col, bottom_row=art_bottom_row, line_times=dialogue_state.line_times)
                        last_lyric_idx = dialogue_state.current_idx

                elif sylt_data or has_uslt:
                    right_col = playback_ui._last_right_left or 1
                    right_w = playback_ui._last_right_width or current_width

                    if (in_uslt_tail and has_uslt and sylt_handoff_end_s is not None
                            and elapsed < sylt_handoff_end_s):
                        in_uslt_tail = False
                        uslt_time_offset = 0.0
                        last_lyric_idx = -1

                    if is_uslt or in_uslt_tail:
                        if manual_line_index is not None and arrow_key_time and time.time() - arrow_key_time > 4.0:
                            manual_line_index = None
                        current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)
                        display_idx = min(current_idx, len(exp_lines) - 1)
                        if display_idx != last_lyric_idx or manual_line_index is not None:
                            draw_uslt_window(
                                lyric_row, exp_lines, exp_times, elapsed + uslt_time_offset,
                                width=right_w, manual_idx=manual_line_index,
                                max_row=last_size[1], col=right_col, bottom_row=art_bottom_row)
                            last_lyric_idx = display_idx

                    else:
                        current_idx = max(
                            (i for i, (_, ts) in enumerate(sylt_data) if ts <= elapsed_ms), default=-1)

                        if (not in_uslt_tail and has_uslt and sylt_handoff_end_s is not None
                                and current_idx == len(sylt_data) - 1 and elapsed >= sylt_handoff_end_s):
                            in_uslt_tail = True
                            uslt_time_offset = 0.0
                            _wrap_w = max(20, current_width - 8)
                            tail_lines = uslt_lines[uslt_handoff_idx:]
                            tail_raw_times = build_uslt_line_times(tail_lines, duration)
                            tail_raw_times = [
                                (t_start + sylt_handoff_end_s, t_end + sylt_handoff_end_s)
                                for t_start, t_end in tail_raw_times]
                            exp_lines, exp_times = expand_uslt_lines(tail_lines, tail_raw_times, _wrap_w)
                            last_lyric_idx = -1

                        if current_idx != last_lyric_idx:
                            draw_lyric_window(
                                lyric_row, sylt_data, current_idx,
                                width=right_w, max_row=last_size[1], col=right_col,
                                bottom_row=art_bottom_row)
                            last_lyric_idx = current_idx

            time.sleep(_LOOP_TICK_S)
        finally:
            sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse on exit
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        music_player(file_path=sys.argv[1])
    else:
        sys.stdout.write("Usage: python -m src.playback.playback <file_path>\n")
