"""
Music playback engine with UI rendering.

Handles audio playback, lyric display, and interactive player controls.
Backend: python-vlc (replaces miniaudio for seeking, volume, and codec support).
"""
from __future__ import annotations
import os
import sys
import time

import vlc
from mutagen.id3 import ID3
# Some type-checkers (pyright) can't resolve dynamic enum-like attributes on vlc.State;
# expose safe local aliases to avoid attribute-access diagnostics.
_VLC_STATE_PAUSED = getattr(vlc.State, 'Paused', None)
_VLC_STATE_ERROR = getattr(vlc.State, 'Error', None)
_VLC_STATE_ENDED = getattr(vlc.State, 'Ended', None)
_VLC_STATE_STOPPED = getattr(vlc.State, 'Stopped', None)

from src import ui_utils
from src.history import log_listening_history
from src.music_library import get_song_duration
from src.album_art import get_ascii_from_mp3
from src.playback_lyrics import (
    _parse_sylt,
    _parse_uslt,
    build_uslt_line_times,
    draw_lyric_window,
    draw_uslt_window,
    expand_uslt_lines,
    find_current_uslt_line,
)
from src.playback_ui import (
    _controls_line,
    _layout_mode,
    ART_MAX_WIDTH,
    draw_full_ui,
    update_progress_ipod,
    update_progress_ui,
)
from src import playback_ui
from src.config import load_config
from src.terminal_input import (
    clear_escape_buffer,
    get_key_non_blocking,
    is_arrow_key,
    raw_mode,
)


def _make_player(file_path: str) -> vlc.MediaPlayer:
    """Create a VLC MediaPlayer with quiet output."""
    instance = vlc.Instance('--no-video', '--quiet')
    media = instance.media_new(file_path)
    mp = instance.media_player_new()
    mp.set_media(media)
    return mp


def _handle_seek(mp, elapsed: float, duration: float, seek_amount: int) -> None:
    """Seek by a relative number of seconds."""
    target = max(0.0, min(elapsed + seek_amount, max(duration - 0.5, 0.0)))
    mp.set_time(int(target * 1000))


def _set_stderr_to_null() -> tuple[int, int]:
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    return old_stderr, devnull


def _restore_stderr(old_stderr: int) -> None:
    os.dup2(old_stderr, 2)
    os.close(old_stderr)


def _render_grouping_cover(file_path: str, cols: int) -> str:
    mode = _layout_mode(cols)
    if mode == 'wide':
        art_w = min(cols // 2, ART_MAX_WIDTH)
    elif mode == 'standard':
        art_w = min(cols, ART_MAX_WIDTH)
    else:
        art_w = min(45, max(1, cols))
    return get_ascii_from_mp3(file_path, art_w, preferred_desc='Booklet', preferred_type=6)


def musicplayer(file_path: str, is_grouping: bool = False, preloaded_data: dict | None = None) -> dict:
    """Main music player engine (VLC backend)."""
    manual_line_index = None
    arrow_key_time = None
    uslt_time_offset = 0.0
    toast_text = ""
    toast_expiry = 0.0

    if preloaded_data:
        audio = preloaded_data['audio']
        duration = preloaded_data['duration']
        pre_art = preloaded_data.get('art')
    else:
        try:
            audio = ID3(file_path)
            duration = get_song_duration(file_path)
            pre_art = None
        except Exception as exc:
            ui_utils.clear_screen()
            sys.stdout.write(f"\033[1;31mPlayback Error:\033[0m Could not load structure for:\n")
            sys.stdout.write(f" → {file_path}\n")
            sys.stdout.write(f"\033[2mDetail: {str(exc)}\033[0m\n\n")
            sys.stdout.write("Press any key to return...")
            sys.stdout.flush()
            with raw_mode(sys.stdin):
                while not get_key_non_blocking():
                    time.sleep(0.05)
            return {"status": "ERROR"}

    sylt_data = _parse_sylt(audio)
    is_uslt = False
    uslt_lines: list[str] = []
    line_times: list[tuple[float, float]] = []

    if not sylt_data:
        uslt_lines_raw = _parse_uslt(audio)
        if uslt_lines_raw:
            is_uslt = True
            sylt_data = uslt_lines_raw
            uslt_lines = [line for line, _ in uslt_lines_raw]
            line_times = build_uslt_line_times(uslt_lines)

    old_stderr, _ = _set_stderr_to_null()
    mp = _make_player(file_path)

    last_size = ui_utils.get_terminal_size()
    last_lyric_idx = -1
    resize_pending = False
    resize_timer = 0.0
    pending_size = last_size

    if is_grouping:
        pre_art = _render_grouping_cover(file_path, last_size[0])

    with raw_mode(sys.stdin):
        mp.play()
        time.sleep(0.3)

        try:
            if not duration or duration <= 0:
                vlc_len = mp.get_length()
                duration = vlc_len / 1000.0 if vlc_len > 0 else 999.0

            volume = mp.audio_get_volume()
            prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
                file_path, audio, pre_art, last_size,
                is_paused=False, volume=volume, toast=toast_text,
            )

            if is_uslt:
                _wrap_w = max(20, current_width - 8)
                exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)
            else:
                exp_lines, exp_times = uslt_lines, line_times

            track_start = time.time()

            def update_ctrl_ui():
                state = mp.get_state()
                is_paused = (state == vlc.State(4))
                vol = mp.audio_get_volume()
                active_toast = toast_text if time.time() < toast_expiry else ""
                if state != vlc.State(7):
                    status_ln, shortcuts_ln = _controls_line(
                        bool(sylt_data), is_paused, vol, active_toast
                    )
                    shortcut_lines = shortcuts_ln.splitlines() or [""]
                    sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}\n")
                    for offset, line in enumerate(shortcut_lines, start=1):
                        sys.stdout.write(f"\033[{ctrl_row + offset};1H\033[K{line}\n")
                    sys.stdout.flush()

            while True:
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
                    if is_grouping:
                        pre_art = _render_grouping_cover(file_path, last_size[0])
                    volume = mp.audio_get_volume()
                    prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
                        file_path, audio, pre_art, last_size,
                        is_paused=(mp.get_state() == vlc.State(4)),
                        volume=volume,
                        toast=toast_text if time.time() < toast_expiry else "",
                    )
                    last_lyric_idx = -1
                    if is_uslt:
                        _wrap_w = max(20, current_width - 8)
                        exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)

                if toast_text and time.time() >= toast_expiry:
                    toast_text = ""
                    update_ctrl_ui()

                elapsed_ms = mp.get_time()
                elapsed = elapsed_ms / 1000.0 if elapsed_ms >= 0 else 0.0

                state = mp.get_state()
                if state in (vlc.State(6), vlc.State(5), vlc.State(7)):
                    break
                if duration and elapsed >= duration:
                    break

                key = get_key_non_blocking()
                if key:
                    clear_escape_buffer()
                    arrow = is_arrow_key(key)

                    if key in (' ', 'p', 'P'):
                        mp.pause()
                        time.sleep(0.05)
                        update_ctrl_ui()
                        last_lyric_idx = -1

                    elif arrow == 'C':
                        _handle_seek(mp, elapsed, duration, 5)
                        toast_text = 'Seek Forward +5s'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif arrow == 'D':
                        _handle_seek(mp, elapsed, duration, -5)
                        toast_text = 'Seek Backward -5s'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif arrow in ('A', 'B') and is_uslt:
                        current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)
                        target_idx = max(0, current_idx - 1) if arrow == 'A' else min(len(exp_times) - 1, current_idx + 1)
                        uslt_time_offset = exp_times[target_idx][0] - elapsed
                        manual_line_index = target_idx
                        arrow_key_time = time.time()

                    elif key == ',':
                        _handle_seek(mp, elapsed, duration, -30)
                        toast_text = 'Seek Backward -30s'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif key == '.':
                        _handle_seek(mp, elapsed, duration, 30)
                        toast_text = 'Seek Forward +30s'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif key.lower() == 'j':
                        _handle_seek(mp, elapsed, duration, -1)
                        toast_text = 'Seek Backward -1s'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif key.lower() == 'l':
                        _handle_seek(mp, elapsed, duration, 1)
                        toast_text = 'Seek Forward +1s'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif key.lower() == 'n':
                        mp.stop()
                        _restore_stderr(old_stderr)
                        ui_utils.clear_screen()
                        return {'status': 'BACK'}

                    elif key.lower() == 'q':
                        mp.stop()
                        _restore_stderr(old_stderr)
                        return {'status': 'QUIT_ALL'}

                    elif key in ('=', '+'):
                        new_vol = min(100, mp.audio_get_volume() + 10)
                        mp.audio_set_volume(new_vol)
                        toast_text = f'Volume: {new_vol}%'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif key in ('-', '_'):
                        new_vol = max(0, mp.audio_get_volume() - 10)
                        mp.audio_set_volume(new_vol)
                        toast_text = f'Volume: {new_vol}%'
                        toast_expiry = time.time() + 1.0
                        update_ctrl_ui()

                    elif key.lower() == 'c':
                        from src.playback_ui import toggle_credits
                        toggle_credits()
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1

                    elif key.lower() == 'i':
                        from src.playback_ui import toggle_help
                        toggle_help()
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1

                    elif key.lower() == 'm':
                        from src.playback_ui import toggle_metadata
                        toggle_metadata()
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1

                if load_config().get('player_view') == 'ipod':
                    update_progress_ipod(prog_row, elapsed, duration, current_width, current_width)
                else:
                    update_progress_ui(prog_row, elapsed, duration, current_width)

                if sylt_data:
                    if is_uslt:
                        if manual_line_index is not None and arrow_key_time and time.time() - arrow_key_time > 4.0:
                            manual_line_index = None

                        current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)
                        if current_idx != last_lyric_idx or manual_line_index is not None:
                            # If UI provided a right-pane, draw lyrics inside it.
                            right_col = playback_ui._last_right_left or 1
                            right_w = playback_ui._last_right_width or current_width
                            draw_uslt_window(
                                lyric_row, exp_lines, exp_times, elapsed + uslt_time_offset,
                                width=right_w, manual_idx=manual_line_index,
                                max_row=last_size[1], col=right_col,
                            )
                            last_lyric_idx = current_idx
                    else:
                        current_idx = max(
                            (i for i, (_, ts) in enumerate(sylt_data) if ts <= elapsed_ms),
                            default=-1,
                        )
                        if current_idx >= 0 and current_idx != last_lyric_idx:
                            right_col = playback_ui._last_right_left or 1
                            right_w = playback_ui._last_right_width or current_width
                            draw_lyric_window(
                                lyric_row, sylt_data, current_idx,
                                width=right_w, max_row=last_size[1], col=right_col,
                            )
                            last_lyric_idx = current_idx

                time.sleep(0.05)
        except Exception as exc:
            mp.stop()
            _restore_stderr(old_stderr)
            ui_utils.clear_screen()
            sys.stdout.write(f"\033[1;31mPlayback Error:\033[0m {exc}\n")
            sys.stdout.flush()
            time.sleep(1.5)
            return {'status': 'ERROR'}

    mp.stop()
    _restore_stderr(old_stderr)
    log_listening_history(file_path, track_start, time.time())
    ui_utils.clear_screen()
    return {'status': 'FINISHED'}
