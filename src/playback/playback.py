"""VLC playback engine and the interactive player loop."""
from __future__ import annotations
import os
import sys
import time
import vlc
from mutagen.id3 import ID3
import mutagen.id3
# Some type-checkers (pyright) can't resolve dynamic enum-like attributes on vlc.State;
# expose safe local aliases to avoid attribute-access diagnostics.
_VLC_STATE_PAUSED = getattr(vlc.State, 'Paused', None)
_VLC_STATE_ERROR = getattr(vlc.State, 'Error', None)
_VLC_STATE_ENDED = getattr(vlc.State, 'Ended', None)
_VLC_STATE_STOPPED = getattr(vlc.State, 'Stopped', None)

from src.utils import ui_utils
from src.history import log_listening_history
from src.music_library import get_song_duration
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
    find_current_dialogue_line,
    parse_markdown_file,
    expand_dialogue_into_sentences,
    _apply_markdown_formatting,
    DialoguePlaybackState
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
from src.utils.terminal_input import (
    clear_escape_buffer,
    get_key_non_blocking,
    is_arrow_key,
    raw_mode,
)
from src.utils.ui_utils import Colors as C

_VLC_PLAY_SETTLE_S = 0.3
_KEY_POLL_INTERVAL_S = 0.05
_LOOP_TICK_S = 0.02


def _make_player(file_path: str) -> vlc.MediaPlayer:
    """Create a VLC media player instance loaded with file_path."""
    instance = vlc.Instance('--no-video', '--quiet')
    assert instance is not None
    media = instance.media_new(file_path)
    assert media is not None
    mp = instance.media_player_new()
    assert mp is not None
    mp.set_media(media)
    return mp


def _handle_seek(mp, elapsed: float, duration: float, seek_amount: float) -> None:
    """Seek the player by seek_amount seconds, clamped to the track bounds."""
    target = max(0.0, min(elapsed + seek_amount, max(duration - 0.5, 0.0)))
    mp.set_time(int(target * 1000))


def _apply_equalizer(mp, audio) -> bool:
    """Apply the file's EQU2 equalisation to playback via libvlc's equaliser.

    Each stored (frequency, gain) point is snapped to the nearest libvlc band
    (the 10 ISO bands match the editor's defaults), so absent bands stay flat.
    Returns True if an equaliser was applied.
    """
    try:
        frames = audio.getall('EQU2')
    except Exception:
        frames = []
    adjustments = [pt for fr in frames for pt in (getattr(fr, 'adjustments', None) or [])]
    if not adjustments:
        return False

    try:
        eq = vlc.AudioEqualizer()
        count = vlc.libvlc_audio_equalizer_get_band_count()
        band_freqs = [vlc.libvlc_audio_equalizer_get_band_frequency(i) for i in range(count)]
        for freq, gain in adjustments:
            if not freq or freq <= 0:
                continue
            # Nearest band by frequency ratio (log-ish distance).
            def _ratio(k: int) -> float:
                """Distance of band k from freq as a >=1 ratio, for nearest-band matching."""
                r = band_freqs[k] / freq
                return r if r >= 1.0 else 1.0 / r
            idx = min(range(count), key=_ratio)
            eq.set_amp_at_index(float(max(-20.0, min(20.0, gain))), idx)  # type: ignore[reportOptionalMemberAccess]
        return bool(mp.set_equalizer(eq) == 0)
    except Exception:
        return False


def _set_stderr_to_null() -> tuple[int, int]:
    """Redirect fd 2 to /dev/null to silence VLC's native stderr spam; returns the
    saved original fd (for _restore_stderr) and the now-closed devnull fd."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    return old_stderr, devnull


def _restore_stderr(old_stderr: int) -> None:
    """Restore fd 2 from the value saved by _set_stderr_to_null."""
    os.dup2(old_stderr, 2)
    os.close(old_stderr)


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

def music_player(file_path: str, is_grouping: bool = False, preloaded_data: dict | None = None,
                 queue_titles: list[str] | None = None, queue_index: int = 0) -> dict:
    """Run the interactive playback loop for file_path: draws the UI, handles
    keyboard controls (seek/pause/volume/panes), drives lyric/dialogue sync, and
    logs listening history. Returns a status dict once playback ends or the user exits."""
    # Register up-next context so the queue view ('u') can render it.
    playback_ui.set_queue_context(queue_titles or [], queue_index)
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
        except (FileNotFoundError, OSError, mutagen.id3.ID3NoHeaderError) as exc:  # type: ignore[reportPrivateImportUsage]
            ui_utils.clear_screen()
            sys.stdout.write(f"\033[1;31mPlayback Error:\033[0m Could not load structure for:\n")
            sys.stdout.write(f" → {file_path}\n")
            sys.stdout.write(f"\033[2mDetail: {str(exc)}\033[0m\n\n")
            sys.stdout.write("Press any key to return...")
            sys.stdout.flush()
            with raw_mode(sys.stdin):
                while not get_key_non_blocking():
                    time.sleep(_KEY_POLL_INTERVAL_S)
            return {"status": "ERROR"}

    dialogue_state = DialoguePlaybackState(file_path, track_duration=duration)

    has_credits = bool(audio.getall('TMCL') or audio.getall('TIPL'))

    sylt_data = _parse_sylt(audio)
    uslt_lines_raw = _parse_uslt(audio)
    uslt_lines: list[str] = [line for line, _ in uslt_lines_raw]

    is_uslt = False
    has_uslt = bool(uslt_lines)
    sylt_handoff_end_s: float | None = None
    uslt_handoff_idx: int = 0
    line_times: list[tuple[float, float]] = []

    if not sylt_data:
        if uslt_lines_raw:
            is_uslt = True
            sylt_data = uslt_lines_raw
            line_times = build_uslt_line_times(uslt_lines, duration)
    else:
        # Check if USLT is also present — SYLT may only cover part of the track.
        if has_uslt:
            pass

    old_stderr, _ = _set_stderr_to_null()
    mp = _make_player(file_path)

    # Apply the track's stored equalisation (EQU2) to the audio output, if any.
    if _apply_equalizer(mp, audio):
        toast_text = "♫ Equaliser applied"
        toast_expiry = time.time() + 2.5

    last_size = ui_utils.get_terminal_size()
    last_lyric_idx = -1
    resize_pending = False
    resize_timer = 0.0
    pending_size = last_size

    if is_grouping:
        pre_art = _render_grouping_cover(file_path, last_size[0])

    with raw_mode(sys.stdin):
        mp.play()
        time.sleep(_VLC_PLAY_SETTLE_S)
        track_start = 0.0

        try:
            if not duration or duration <= 0:
                vlc_len = mp.get_length()
                duration = vlc_len / 1000.0 if vlc_len > 0 else 999.0

            volume = mp.audio_get_volume()
            prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
                file_path, audio, pre_art, last_size,
                is_paused=False, volume=volume, toast=toast_text,
            )

            # Compute SYLT→USLT handoff timing now that duration is known.
            if sylt_data and not is_uslt and has_uslt:
                sylt_handoff_end_s = estimate_sylt_last_line_end(
                    sylt_data, duration * 1000
                )
                uslt_handoff_idx = find_uslt_handoff_index(
                    uslt_lines, sylt_data[-1][0]
                )

            if is_uslt:
                _wrap_w = max(20, current_width - 8)
                exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)
            else:
                exp_lines, exp_times = [], []

            right_col = playback_ui._last_right_left or 1
            right_w = playback_ui._last_right_width or current_width

            if _ui_state['show_lyrics']:
                # Draw initial lyric/dialogue state.
                if dialogue_state.is_active():
                    dialogue_state.update(0.0)
                    draw_dialogue_window(
                        row=lyric_row,
                        dialogue_lines=dialogue_state.expanded_chunks,
                        current_idx=dialogue_state.current_idx,
                        width=right_w,
                        max_row=last_size[1],
                        col=right_col,
                        bottom_row=art_bottom_row,
                        line_times=dialogue_state.line_times,
                    )

                elif sylt_data or has_uslt:
                    first_line = sylt_data[0][0] if sylt_data else (uslt_lines[0] if uslt_lines else "")
                    draw_lyric_initial(
                        lyric_row, first_line,
                        width=right_w, max_row=last_size[1], col=right_col,
                        bottom_row=art_bottom_row,
                    )

            in_uslt_tail = False

            track_start = time.time()

            has_lyrics = bool(sylt_data) or has_uslt or dialogue_state.is_active()

            def update_ctrl_ui():
                """Redraw just the transport/status line (play state, volume, toast) in place,
                leaving the hint line(s) below it untouched to avoid flicker."""
                state = mp.get_state()
                is_paused = (state == _VLC_STATE_PAUSED)
                vol = mp.audio_get_volume()
                active_toast = toast_text if time.time() < toast_expiry else ""
                if state != _VLC_STATE_ERROR:
                    status_ln, _ = _controls_line(
                        is_uslt or in_uslt_tail, is_paused, vol, active_toast,
                        has_lyrics=has_lyrics, has_credits=has_credits,
                    )
                    # Only the transport/status line changes on play-pause / seek /
                    # volume. The hint line(s) below it are owned by the full-UI
                    # draw (start, `i` toggle, resize, focus, panel/meta toggles);
                    # rewriting them here made them flicker and reappear on every
                    # action — they must change only when `i` is pressed.
                    sys.stdout.write(f"\033[{ctrl_row};1H\033[K{status_ln}")
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
                    prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
                        file_path, audio, pre_art, last_size,
                        is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                        volume=volume,
                        toast=toast_text if time.time() < toast_expiry else "",
                    )
                    last_lyric_idx = -1
                    if is_uslt or in_uslt_tail:
                        _wrap_w = max(20, current_width - 8)
                        exp_lines, exp_times = expand_uslt_lines(uslt_lines, line_times, _wrap_w)

                if toast_text and time.time() >= toast_expiry:
                    toast_text = ""
                    update_ctrl_ui()

                elapsed_ms = mp.get_time()
                elapsed = elapsed_ms / 1000.0 if elapsed_ms >= 0 else 0.0

                state = mp.get_state()
                if state in (_VLC_STATE_ERROR, _VLC_STATE_ENDED, _VLC_STATE_STOPPED):
                    break
                if duration and elapsed >= duration:
                    break

                key = get_key_non_blocking()
                if key:
                    clear_escape_buffer()

                    if key == 'FOCUS_OUT':
                        pass  # silently consume — no redraw needed on blur
                    elif key == 'FOCUS_IN':
                        # Redraw the full UI when the window regains focus; the
                        # screen may have been dirtied by other apps while away.
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1

                    arrow = is_arrow_key(key)
                    toggle_controls = {
                        'm': toggle_metadata,
                    }

                    if key in (' ', 'p', 'P'):
                        mp.pause()
                        time.sleep(_KEY_POLL_INTERVAL_S)
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

                    elif arrow in ('A', 'B') and (is_uslt or in_uslt_tail):
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

                    elif key.lower() == 'e':          # TEMP: jump to last 35 seconds
                        _handle_seek(mp, elapsed, duration, (duration - 35) - elapsed)
                        toast_text = 'Skip to last 35s'
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
                        ui_utils.clear_screen()
                        return {'status': 'BACK'}

                    elif key in ('b', 'B'):
                        # Stop playback and return to the page we came from (#41).
                        mp.stop()
                        ui_utils.clear_screen()
                        return {'status': 'STOP'}

                    elif key.lower() == 'q':
                        mp.stop()
                        return {'status': 'QUIT_ALL'}

                    elif key in ('=', '+'):
                        new_vol = min(100, mp.audio_get_volume() + 5)
                        mp.audio_set_volume(new_vol)
                        toast_text = f'Volume: {new_vol}%'
                        toast_expiry = time.time() + 1.0
                        playback_ui.draw_volume_bar(new_vol)
                        update_ctrl_ui()

                    elif key in ('-', '_'):
                        new_vol = max(0, mp.audio_get_volume() - 5)
                        mp.audio_set_volume(new_vol)
                        toast_text = f'Volume: {new_vol}%'
                        toast_expiry = time.time() + 1.0
                        playback_ui.draw_volume_bar(new_vol)
                        update_ctrl_ui()

                    elif key.lower() == 'i':
                        ui_utils.clear_screen()
                        toggle_help()
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1

                    elif key.lower() == 'w':
                        # Single key cycles the right column: off → lyrics →
                        # queue → lyrics+credits (unavailable views skipped).
                        ui_utils.clear_screen()
                        cycle_right_pane(has_lyrics, has_credits, playback_ui.has_queue())
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1

                    elif key.lower() in toggle_controls:
                        # Wipe the screen so toggling actually removes content from the terminal
                        ui_utils.clear_screen()

                        toggle_controls[key.lower()]()
                        volume = mp.audio_get_volume()
                        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
                            file_path, audio, pre_art, last_size,
                            is_paused=(mp.get_state() == _VLC_STATE_PAUSED),
                            volume=volume,
                            toast=toast_text if time.time() < toast_expiry else "",
                        )
                        last_lyric_idx = -1


                update_progress_ui(prog_row, elapsed, duration, current_width)

                if _ui_state.get('show_lyrics', True) and not _ui_state.get('show_queue'):
                    if dialogue_state.is_active():
                        right_col = playback_ui._last_right_left or 1
                        right_w = playback_ui._last_right_width or current_width

                        dialogue_state.update(elapsed=elapsed)

                        if dialogue_state.current_idx != last_lyric_idx:
                            draw_dialogue_window(
                                row=lyric_row,
                                dialogue_lines=dialogue_state.expanded_chunks,
                                current_idx=dialogue_state.current_idx,
                                width=right_w,
                                max_row=last_size[1],
                                col=right_col,
                                bottom_row=art_bottom_row,
                                line_times=dialogue_state.line_times,
                            )
                            last_lyric_idx = dialogue_state.current_idx

                    elif sylt_data or has_uslt:
                        right_col = playback_ui._last_right_left or 1
                        right_w = playback_ui._last_right_width or current_width

                        # Switch back to SYLT if we seek backwards past the handoff point.
                        if (
                            in_uslt_tail
                            and has_uslt
                            and sylt_handoff_end_s is not None
                            and elapsed < sylt_handoff_end_s
                        ):
                            in_uslt_tail = False
                            uslt_time_offset = 0.0
                            last_lyric_idx = -1

                        if is_uslt or in_uslt_tail:
                            if manual_line_index is not None and arrow_key_time and time.time() - arrow_key_time > 4.0:
                                manual_line_index = None

                            current_idx = find_current_uslt_line(exp_times, elapsed + uslt_time_offset)
                            display_idx = current_idx
                            display_idx = min(display_idx, len(exp_lines) - 1)
                            if display_idx != last_lyric_idx or manual_line_index is not None:
                                draw_uslt_window(
                                    lyric_row, exp_lines, exp_times,
                                    elapsed + uslt_time_offset,
                                    width=right_w, manual_idx=manual_line_index,
                                    max_row=last_size[1], col=right_col,
                                    bottom_row=art_bottom_row,
                                )
                                last_lyric_idx = display_idx

                    else:
                        current_idx = max(
                            (i for i, (_, ts) in enumerate(sylt_data) if ts <= elapsed_ms),
                            default=-1,
                        )

                        if (
                            not in_uslt_tail
                            and has_uslt
                            and sylt_handoff_end_s is not None
                            and current_idx == len(sylt_data) - 1
                            and elapsed >= sylt_handoff_end_s
                        ):
                            in_uslt_tail = True
                            uslt_time_offset = 0.0
                            _wrap_w = max(20, current_width - 8)
                            tail_lines = uslt_lines[uslt_handoff_idx:]
                            tail_raw_times = build_uslt_line_times(tail_lines, duration)
                            tail_raw_times = [
                                (t_start + sylt_handoff_end_s, t_end + sylt_handoff_end_s)
                                for t_start, t_end in tail_raw_times
                            ]
                            exp_lines, exp_times = expand_uslt_lines(
                                tail_lines, tail_raw_times, _wrap_w
                            )
                            last_lyric_idx = -1

                        if current_idx != last_lyric_idx:
                            draw_lyric_window(
                                lyric_row, sylt_data, current_idx,
                                width=right_w, max_row=last_size[1], col=right_col,
                                bottom_row=art_bottom_row,
                            )
                            last_lyric_idx = current_idx

                time.sleep(_LOOP_TICK_S)

        except KeyboardInterrupt:
            pass
        finally:
            mp.stop()
            log_listening_history(file_path, track_start, time.time())
            _restore_stderr(old_stderr)

    ui_utils.clear_screen()
    return {"status": "OK"}

if __name__ == "__main__":
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
        music_player(file_path=file_path)
    else:
        sys.stdout.write("Usage: python -m src.playback.playback <file_path>\n")
