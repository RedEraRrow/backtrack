"""The interactive player *view* over the shared PlaybackSession (feature #14).

Audio ownership, the queue, and the track lifecycle live in ``session.py``; this
module is the foreground renderer/controller. Opening a track starts the shared
session and attaches this view; **leaving the view (b/Esc) keeps the session
playing in the background** — only Stop (s) ends it. The session's background
tick advances the queue and logs history whether or not this view is attached.
"""
from __future__ import annotations
import os
import sys

from src.state import QuitToTerminal
import time

from src.utils import ui_utils
from src.art.album_art import get_art_from_mp3
from src.lyrics import lyric_pane
from src.lyrics.lyrics import (
    _parse_sylt, _parse_uslt, build_uslt_line_times, DialoguePlaybackState,
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
from src.playback.player_geom import geom
from src.utils.log import log
from src.utils import prompt_core as pc
from src.playback.session import (
    SESSION, is_client, has_other_windows,
)
from src.utils.terminal_input import (
    clear_escape_buffer,
    get_key_non_blocking,
    is_arrow_key,
    raw_mode,
)
from src import tuning as tune

_KEY_POLL_INTERVAL_S = tune.KEY_POLL_INTERVAL_S
_LOOP_TICK_S = tune.LOOP_TICK_S

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


def music_player(file_path: str, is_grouping: bool = False,
                 queue_titles: list[str] | None = None, queue_index: int = 0,
                 queue_paths: list[str] | None = None, mode: str | None = None) -> dict:
    """Start the shared session on ``file_path`` (with its queue) and open the
    player view. Kept name/signature for existing callers; audio now persists in
    the background after the view is left (only Stop ends it). Returns a status
    dict: ``DETACH`` (minimised, still playing), ``STOP``, ``OK`` (queue
    finished), or ``ERROR``; q raises QuitToTerminal."""
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


# Seek keys shared by the host player and a joined window's: seconds to move.
_SEEK_KEYS = {',': -30, '.': 30, 'j': -1, 'J': -1, 'l': 1, 'L': 1}


def _seek_step(key: str, arrow, duration: float, elapsed: float) -> tuple[float, str] | None:
    """The seek a player key asks for, as (seconds to move, toast), or None when
    the key isn't a seek. `e`, with Diagnostics on, jumps to near the end."""
    if arrow in ('C', 'D'):
        secs = 5 if arrow == 'C' else -5
    elif key in ('e', 'E') and playback_ui._ui_state['debug']:
        return (duration - tune.NEAR_END_JUMP_S) - elapsed, f'Skip to last {tune.NEAR_END_JUMP_S}s'
    elif key in _SEEK_KEYS:
        secs = _SEEK_KEYS[key]
    else:
        return None
    return secs, f"Seek {'Forward +' if secs > 0 else 'Backward -'}{abs(secs)}s"


def _step_volume(remote, target, delta: int):
    """A +/- press in a joined window. The host's volume only reaches this
    window with its next snapshot (a quarter-second later), so quick presses
    step from the level just asked for, not the stale one — five fast presses
    are +25, not +5. Returns the new (level, time)."""
    now = time.time()
    base = target[0] if target and now - target[1] < 1.0 else remote.get_volume()
    level = max(0, min(100, base + delta))
    remote.set_volume(level)
    return (level, now)


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
    vol_target = None                      # (level, time) of the last +/- press
    prog_row = 0
    ctrl_row = 0
    toast = ""
    toast_expiry = 0.0
    width = ui_utils.get_terminal_size()[0]
    playback_ui._ui_state['lyrics_pane'] = False
    playback_ui.refresh_debug_flag()
    try:
        with raw_mode(sys.stdin):
            sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse
            sys.stdout.flush()
            while True:
                # A hand-off replaces the session link (or makes this window the
                # host): this view's `remote` is then a closed link still showing
                # its last snapshot. Step out; reopening picks up the new one.
                if sess.active_session() is not remote:
                    return {"status": "DETACH"}
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
                            # No ID3 tag (an M4A, an untagged MP3): an empty one,
                            # as the host player uses — the layout reads from it.
                            audio = ID3()
                    duration = float(np.get('duration') or 0.0)
                    prog_row, ctrl_row, _lr, width, _br = draw_full_ui(
                        fp, audio, None, size, is_paused=bool(np.get('paused')),
                        volume=sess.clamp_volume(np.get('volume')), toast=toast)
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
                            _frac = playback_ui.progress_from_click(_mr, _mc)
                            if _vol is not None:
                                remote.set_volume(_vol); key = ''
                            elif _frac is not None and duration:
                                # Click anywhere on the bar to jump there (the
                                # session only takes relative seeks).
                                remote.seek(_frac * duration - elapsed); key = ''
                            else:
                                _hk = playback_ui.hint_click_key(_mr, _mc)
                                key = _PLAYER_SYNTH.get(_hk, _hk) if _hk else ''
                        arrow = is_arrow_key(key)
                    if key == 'FOCUS_IN':
                        last_sig = None                # force a full redraw
                    elif key in (' ', 'p', 'P'):
                        remote.pause_toggle()
                    elif (step := _seek_step(key, arrow, duration, elapsed)) is not None:
                        remote.seek(step[0])
                    elif key == ']':
                        remote.next()
                    elif key == '[':
                        remote.prev()
                    elif key in ('=', '+'):
                        vol_target = _step_volume(remote, vol_target, +5)
                    elif key in ('-', '_'):
                        vol_target = _step_volume(remote, vol_target, -5)
                    elif key in ('b', 'B') or key == '\x1b':
                        # Pinned open while another window browses this session
                        # (#14) — the two windows stay specialised until one closes.
                        if has_other_windows():
                            toast = 'Close the other window to leave the player'
                            toast_expiry = time.time() + tune.TOAST_MEDIUM_S
                            last_sig = None
                        else:
                            return {"status": "DETACH"}
                    elif key.lower() == 's':
                        remote.stop()
                        return {"status": "STOP"}
                    elif key.lower() == 'q':
                        raise QuitToTerminal()
                    elif key.lower() in ('i', 'm'):
                        ui_utils.clear_screen()
                        (toggle_help if key.lower() == 'i' else toggle_metadata)()
                        last_sig = None                # redraw with the new layout
                    elif key.lower() == 'w' and audio is not None:
                        if cycle_right_pane(False, bool(audio.getall('TMCL') or audio.getall('TIPL')),
                                            playback_ui.has_queue()):
                            ui_utils.clear_screen()
                            last_sig = None
                time.sleep(_LOOP_TICK_S)
    finally:
        playback_ui._ui_state['lyrics_pane'] = True
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
    has_credits = False
    has_lyrics = False
    dialogue_state = None

    # --- view / loop state ---
    toast_text = ""
    toast_expiry = 0.0
    last_size = ui_utils.get_terminal_size()
    resize_pending = False
    resize_timer = 0.0
    prog_row = ctrl_row = lyric_row = art_bottom_row = 0
    pane = None
    current_width = last_size[0]
    last_q_sig: tuple | None = None

    def _prepare() -> None:
        """(Re)load per-track render state from the session for the current track."""
        nonlocal audio, duration, pre_art, sylt_data, uslt_lines, line_times
        nonlocal is_uslt, has_credits, has_lyrics, dialogue_state
        nonlocal toast_text, toast_expiry, pane
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
        line_times = []
        if not sylt_data and uslt_lines_raw:
            is_uslt = True
            sylt_data = uslt_lines_raw
            line_times = build_uslt_line_times(uslt_lines, duration)
        has_lyrics = bool(sylt_data) or bool(uslt_lines) or dialogue_state.is_active()

        # One timeline, whichever source this track has. Richest first: a dialogue
        # transcript knows speakers and directions, SYLT knows real timings, USLT
        # knows only the words and is paced across the track. All three become the
        # same beats here, so nothing downstream asks which kind of lyrics these
        # are or keeps a second set of rules for them.
        _tl = None
        # Whatever this track's words come from, say so in the same breath as
        # choosing it — the 'm' panel then names the real source rather than a
        # guess reconstructed later from the file names.
        _srcs, _est = [], False
        if dialogue_state.is_active():
            _tl = lyric_pane.from_chunks(dialogue_state.expanded_chunks,
                                         dialogue_state.line_times, duration)
            _srcs = [os.path.basename(p) for p in
                     (dialogue_state.md_path, dialogue_state.json_path) if p]
            _est = dialogue_state.timing_source == 'estimated'
        elif sylt_data and not is_uslt:
            _tl = lyric_pane.from_sylt(sylt_data, duration)
            _srcs = ['embedded SYLT']
        elif uslt_lines and line_times:
            _tl = lyric_pane.from_uslt(uslt_lines, line_times, duration)
            _srcs = ['embedded USLT']
            _est = True          # USLT carries words only; the pacing is invented
        playback_ui.set_lyric_sources(fp, _srcs, _est)
        pane = lyric_pane.Pane(_tl) if _tl else None
        # Say it out loud. Paced-out timing looks right for the first minute and is
        # a line adrift by the last, which is not something a still screen shows.
        if dialogue_state.is_active() and dialogue_state.timing_source == 'estimated':
            toast_text = "⚠ No transcript — lyric timing is estimated and will drift"
            toast_expiry = time.time() + tune.TOAST_LONG_S

        if audio and audio.getall('EQU2'):
            toast_text = "♫ Equaliser applied"
            toast_expiry = time.time() + tune.TOAST_LONG_S

    def _redraw_full() -> None:
        """Full-screen redraw for the current track + view state; sets row positions."""
        nonlocal prog_row, ctrl_row, lyric_row, current_width, art_bottom_row
        vol = SESSION.get_volume()
        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
            SESSION.file_path or "", audio, pre_art, last_size,
            is_paused=SESSION.is_paused(), volume=vol,
            toast=toast_text if time.time() < toast_expiry else "",
        )

        if pane and _ui_state['show_lyrics'] and not _ui_state.get('show_queue'):
            # A full redraw has just wiped the screen, so what the pane last
            # painted is gone. Its record is of rows it wrote, not rows that
            # survived, so it is told — and repaints in step with the rest of the
            # UI rather than a tick later.
            _mp = getattr(SESSION, 'mp', None)
            _ms = _mp.get_time() if _mp is not None else 0
            pane.paint(sys.stdout, (_ms / 1000.0) if _ms and _ms > 0 else 0.0,
                       lyric_pane.Geometry(
                           row=lyric_row,
                           col=geom.right_left or 1,
                           width=geom.right_width or current_width,
                           bottom=art_bottom_row or last_size[1]),
                       force=True)
            sys.stdout.flush()


    def update_ctrl_ui() -> None:
        """Redraw just the transport/status line in place (see #86)."""
        active_toast = toast_text if time.time() < toast_expiry else ""
        status_ln, _ = _controls_line(
            is_uslt, SESSION.is_paused(), SESSION.get_volume(), active_toast,
            has_lyrics=has_lyrics, has_credits=has_credits)
        sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}")
        sys.stdout.flush()
        # Written outside the painter: have it forget the row, or the next full
        # frame skips it as unchanged (a paused ⏵ stayed after skipping track).
        pc.screen_forget_rows(ctrl_row, ctrl_row)

    playback_ui.set_resizing(False)      # in case the last visit ended mid-resize
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

            # Redraw at every size the window passes through, as soon as it's
            # seen — the gap in which the terminal shows the old frame reflowed
            # is one loop tick, not a debounce window. Mid-resize the art image
            # is only the quick preview; once the size has held still for
            # ART_FULL_IMAGE_SETTLE_S, the full-quality image goes on top.
            current_size = ui_utils.get_terminal_size()
            if current_size != last_size:
                last_size = current_size
                resize_pending = True
                resize_timer = time.time()
                playback_ui.set_resizing(True)
                # A group cover is drawn to the width, so render it again.
                if SESSION.is_grouping:
                    pre_art = _render_grouping_cover(SESSION.file_path or "", last_size[0])
                # The terminal reflowed the old frame. No separate clear: the
                # painter sees the new size and wipes in the same write as the
                # new rows, so a drag doesn't flash blank at every step.
                _t0 = time.monotonic()
                _redraw_full()
                log.debug("player redraw at %sx%s took %.0f ms (%.0f ms after the signal)",
                          last_size[0], last_size[1], (time.monotonic() - _t0) * 1000,
                          ui_utils.ms_since_resize_signal())
            elif resize_pending and (time.time() - resize_timer > tune.ART_FULL_IMAGE_SETTLE_S):
                resize_pending = False
                playback_ui.set_resizing(False)
                playback_ui.redraw_art_image()
                log.debug("player resize settled at %sx%s", last_size[0], last_size[1])
            elif not resize_pending and playback_ui.art_image_incomplete():
                # A resize cut the full image short but didn't change the size
                # (or already settled): send it again.
                playback_ui.redraw_art_image()

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
                    _qi = playback_ui.queue_click_index(_mr, _mc)
                    if _act == 'prev':
                        key = '['
                    elif _act == 'next':
                        key = ']'
                    elif _act == 'playpause':
                        key = ' '
                    elif _qi is not None:
                        # A track row in the queue pane: play it. The track
                        # change is picked up (and redrawn) at the top of the loop.
                        if _qi != SESSION.index:
                            SESSION.jump(_qi)
                        key = ''
                    else:
                        _vol = playback_ui.volume_from_click(_mr, _mc)
                        _frac = playback_ui.progress_from_click(_mr, _mc)
                        if _vol is not None:
                            v = SESSION.set_volume(_vol)
                            toast_text = f'Volume: {v}%'; toast_expiry = time.time() + tune.TOAST_SHORT_S
                            playback_ui.draw_volume_bar(v); update_ctrl_ui()
                            key = ''
                        elif _frac is not None and duration:
                            # Click anywhere on the bar to jump there; SESSION only
                            # takes relative seeks, so aim from where we are.
                            _tgt = _frac * duration
                            SESSION.seek(_tgt - elapsed)
                            toast_text = f'Seek to {ui_utils.format_time(int(_tgt))}'
                            toast_expiry = time.time() + tune.TOAST_SHORT_S
                            update_ctrl_ui()
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
                elif (step := _seek_step(key, arrow, duration, elapsed)) is not None:
                    SESSION.seek(step[0])
                    toast_text = step[1]; toast_expiry = time.time() + tune.TOAST_SHORT_S
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
                        toast_expiry = time.time() + tune.TOAST_MEDIUM_S
                        update_ctrl_ui(); continue
                    return {"status": "DETACH"}
                elif key.lower() == 's':          # STOP playback
                    SESSION.stop()
                    return {"status": "STOP"}
                elif key.lower() == 'q':          # QUIT the app
                    raise QuitToTerminal()
                elif key in ('=', '+'):
                    v = SESSION.set_volume(SESSION.get_volume() + 5)
                    toast_text = f'Volume: {v}%'; toast_expiry = time.time() + tune.TOAST_SHORT_S
                    playback_ui.draw_volume_bar(v)
                    update_ctrl_ui()
                elif key in ('-', '_'):
                    v = SESSION.set_volume(SESSION.get_volume() - 5)
                    toast_text = f'Volume: {v}%'; toast_expiry = time.time() + tune.TOAST_SHORT_S
                    playback_ui.draw_volume_bar(v)
                    update_ctrl_ui()
                elif key.lower() == 'i':
                    ui_utils.clear_screen()
                    toggle_help()
                    _redraw_full()
                elif key.lower() == 'w':
                    if cycle_right_pane(has_lyrics, has_credits, playback_ui.has_queue()):
                        ui_utils.clear_screen()
                        _redraw_full()
                elif key.lower() == 'm':
                    ui_utils.clear_screen()
                    toggle_metadata()
                    _redraw_full()

            update_progress_ui(prog_row, elapsed, duration, current_width)

            if pane and _ui_state.get('show_lyrics', True) and not _ui_state.get('show_queue'):
                # Every tick takes the same path: the frame is a function of the
                # clock and the geometry and nothing else. A seek is just a
                # different number arriving, a resize just a different box —
                # neither is an event anyone has to notice, and neither can leave
                # the words disagreeing with the audio. Only rows that differ get
                # written, so an unchanged frame costs nothing.
                pane.paint(sys.stdout, elapsed, lyric_pane.Geometry(
                    row=lyric_row,
                    col=geom.right_left or 1,
                    width=geom.right_width or current_width,
                    bottom=art_bottom_row or last_size[1]))
                sys.stdout.flush()

            time.sleep(_LOOP_TICK_S)
        finally:
            sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse on exit
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        music_player(file_path=sys.argv[1])
    else:
        sys.stdout.write("Usage: python -m src.playback.playback <file_path>\n")
