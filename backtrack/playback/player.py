"""The interactive player *view* over the shared PlaybackSession (feature #14).

Audio ownership, the queue, and the track lifecycle live in ``session.py``; this
module is the foreground renderer/controller. Opening a track starts the shared
session and attaches this view; **leaving the view (b/Esc) keeps the session
playing in the background**; only Stop (s) ends it. The session's background
tick advances the queue and logs history whether or not this view is attached.
"""
from __future__ import annotations
import os
import sys

from backbone import nav
from backbone.nav import QuitToTerminal
import time
from contextlib import contextmanager

from backbone import keys, prompt, ui
from backtrack.lyrics import lyric_pane
from backtrack.lyrics.formats import (
    _parse_sylt, _parse_uslt, build_uslt_line_times, DialoguePlaybackState,
)
from backtrack.playback.player_ui import (
    _controls_line,
    draw_full_ui,
    update_progress_ui,
    _ui_state,
    toggle_metadata,
    toggle_help,
)
from backtrack.playback import player_ui
from backtrack.playback.player_geom import geom
from backtrack.playback.player_art import GROUP_COVER, IDLE_ART
from backbone.log import log, quietly
from backtrack.music_library import chapter_at, drop_moved, library_entry
from backbone.prompt import core as pc
from backtrack.playback.session import (
    SESSION, is_client, has_other_windows,
)
from backbone.terminal_input import (
    get_key_non_blocking,
    raw_mode,
)
from backtrack import accents, tuning as tune

_KEY_POLL_INTERVAL_S = tune.KEY_POLL_INTERVAL_S
_LOOP_TICK_S = tune.LOOP_TICK_S


def _show_load_error(file_path: str) -> None:
    """Show a 'could not load' screen and wait for a keypress."""
    ui.clear_screen()
    sys.stdout.write("\033[1;31mPlayback Error:\033[0m Could not load:\n")
    sys.stdout.write(f" → {file_path}\n\n")
    sys.stdout.write("Press any key to return...")
    sys.stdout.flush()
    with raw_mode(sys.stdin):
        while not get_key_non_blocking():
            time.sleep(_KEY_POLL_INTERVAL_S)


def music_player(file_path: str, is_grouping: bool = False,
                 queue_titles: list[str] | None = None, queue_index: int = 0,
                 queue_paths: list[str] | None = None, mode: str | None = None,
                 then: str | None = None, start_at: float | None = None, view: bool = True) -> dict:
    """Start the shared session on ``file_path`` (with its queue) and open the
    player view. Audio keeps playing after the view is left; only Stop ends it.
    ``then``: a track picked from the list ``queue_paths``, played as the
    after-a-picked-track setting says (session.AFTER_PICK), not as a new queue.
    ``start_at``: seconds into the track to start from (Resume); None resumes
    a started audiobook where it was left.
    Returns a status
    dict: ``DETACH`` (minimised, still playing), ``STOP``, ``OK`` (queue
    finished), or ``ERROR``; q raises QuitToTerminal."""
    paths = queue_paths if queue_paths else [file_path]
    kept = drop_moved(paths)
    if len(kept) != len(paths):              # moved/renamed since listed: said so already
        if not kept:
            return {"status": "ERROR"}
        keep = set(kept)
        order = [i for i, p in enumerate(paths) if p in keep]
        if queue_titles and len(queue_titles) == len(paths):
            queue_titles = [queue_titles[i] for i in order]
        queue_index = next((n for n, i in enumerate(order) if i >= queue_index), 0)
        paths = kept
        file_path = paths[queue_index]
        queue_paths = paths
    # In a joined (client) window the audio lives in the host process: send the
    # play there and stay in this window's menus (the host's now-playing box
    # updates via the mirror). Ctrl-O opens the client player view.
    from backtrack.playback.session import active_session
    session = active_session()

    def _play() -> bool:
        if then is not None:
            return session.edit('play_picked', paths=paths, index=queue_index, then=then,
                                titles=queue_titles)
        return session.start(file_path, queue=paths, titles=queue_titles, index=queue_index,
                             mode=mode, is_grouping=is_grouping, start_at=start_at)
    if is_client():
        _play()
        ui.show_status("Sent to the window that's playing.")
        return {"status": "OK"}
    ok = _play()
    if not ok:
        _show_load_error(file_path)
        return {"status": "ERROR"}
    SESSION.start_background_tick()
    return open_player_view() if view else {"status": "OK"}


NOW_PLAYING_TAB = "Now playing"


def show_player() -> dict | None:
    """Open the player on what's playing, here or in the session this window
    joined (Ctrl-O, and the Now playing tab). None when it can't open."""
    from backtrack.playback import session as sess
    snap = sess.current_now_playing()
    if not snap:
        ui.show_status("Nothing is playing.")
        return None
    holder = snap.get('view_holder')
    if holder and holder != sess.my_token():
        ui.show_status("The player is open in another window.")
        return None
    return open_client_player_view() if sess.is_client() else open_player_view()


def _tab_asked(key: str) -> int | None:
    """The tab a key or tab-bar click in the player asks for. Refused while
    the player is pinned open for another window, as leaving it is."""
    tab = nav.tab_for(key)
    if tab is not None and has_other_windows():
        ui.show_status('Close the other window to leave the player', tune.TOAST_MEDIUM_S)
        return None
    return tab


def _on_now_playing(view) -> dict:
    """Run a player view (this window's or a joined one's) on the Now playing
    tab: from any other tab, show that tab instead, returning once it's left.
    Leaving the view for another tab detaches it and shows that tab; it opens
    again when Now playing shows again (if anything still plays)."""
    if nav.go_to_tab(NOW_PLAYING_TAB):
        return {"status": "DETACH"}
    while True:
        result = view()
        if result.get("status") != "TAB":
            return result
        if result.get("browse"):                 # a, A: the track's album or artist
            from backtrack.menus.browse import open_in_browse
            open_in_browse(*result["browse"])
        else:
            nav.switch_to(result["tab"])


def open_player_view() -> dict:
    """Render + control the current session in the foreground until the user
    leaves. Does NOT stop the session on ``DETACH``: audio keeps playing.
    Claims the cross-window view lock; if another window holds it, just detaches."""
    return _on_now_playing(_host_view)


def _host_view() -> dict:
    """open_player_view's view: until it's left, or another tab asked for (``TAB``)."""
    from backtrack.playback.session import my_token
    if not SESSION.is_active():
        return {"status": "OK"}
    if not SESSION.acquire_view(my_token()):
        return {"status": "DETACH"}          # a client window has the player open
    SESSION.view_attached = True
    player_ui.view_chrome(True)
    try:
        while True:
            result = _player_view_loop()
            # Pinned open while another window browses (#14): when playback ends,
            # show the empty player until something plays or that window closes.
            if result["status"] == "DETACH" or not has_other_windows():
                return result
            if not _idle_view(SESSION.is_active, SESSION.get_volume()):
                return {"status": "OK"}
    finally:
        player_ui.view_chrome(False)
        SESSION.view_attached = False
        SESSION.release_view(my_token())
        ui.clear_screen()


# Seek actions shared by the host player and a joined window's: seconds to move.
_SEEKS = {'player.fwd_5': 5, 'player.back_5': -5, 'player.back_30': -30, 'player.fwd_30': 30,
          'player.back_1': -1, 'player.fwd_1': 1}


def _seek_step(key: str, duration: float, elapsed: float) -> tuple[float, str] | None:
    """The seek a player key asks for, as (seconds to move, toast), or None when
    the key isn't a seek. The near-end key, with Diagnostics on, jumps there."""
    act = keys.action(key, 'player') if key else None
    if act == 'player.near_end' and player_ui._ui_state['debug']:
        return (duration - tune.NEAR_END_JUMP_S) - elapsed, f'Skip to last {tune.NEAR_END_JUMP_S}s'
    elif act in _SEEKS:
        secs = _SEEKS[act]
    else:
        return None
    return secs, f"Seek {'Forward +' if secs > 0 else 'Backward -'}{abs(secs)}s"


def _speed_or_sleep(act: str | None, session, rate: float) -> bool:
    """The speed and sleep-timer keys, for the host player and a joined window's.
    Returns whether `act` was one."""
    if act in ('player.slower', 'player.faster'):
        r = session.set_rate(rate + (tune.RATE_STEP if act == 'player.faster' else -tune.RATE_STEP))
        ui.show_status(f'Speed ×{r:g}' if r is not None else 'Speed is for audiobooks', tune.TOAST_SHORT_S)
        return True
    if act == 'player.sleep':
        mode = session.cycle_sleep()
        ui.show_status('Sleep timer off' if mode is None else 'Sleep at the end of the chapter'
                       if mode == 'chapter' else f'Sleep in {mode} min', tune.TOAST_MEDIUM_S)
        return True
    return False


def _track_key(act: str | None, path: str | None, session) -> bool | dict:
    """e, a and A on the playing track, for the host player and a joined
    window's: its tag editor (True: the player is then drawn afresh), or
    its album or artist in Browse (the result for the view to return, to
    leave it as a tab key does). False when `act` isn't one."""
    if not path or act not in ('player.edit', 'player.album', 'player.artist'):
        return False
    if act != 'player.edit':
        if nav.TABS:
            return {"status": "TAB", "tab": None,
                    "browse": ('albums' if act == 'player.album' else 'artists', path)}
        ui.show_status("Browse is in the app, not this window.", tune.TOAST_MEDIUM_S)
        return True
    from backtrack.id3.browser import inspect_tag_loop
    from backtrack.music_library import live_library
    ui.clear_screen()
    with _out_of_view(session):
        inspect_tag_loop(path, library_metadata=library_entry(path), library=live_library())
    sys.stdout.write("\033[?1000h\033[?1006h")      # the editor had the mouse
    ui.clear_screen()
    return True


def _choose_panels(has: dict) -> None:
    """`w`: which panels show, ticked in a box over the player (one this track
    has nothing for says so, and stays as set for the tracks that do), and
    Arrange, to move them around."""
    rows = [prompt.Choice(title=label if has.get(kind) else f"{label}  {ui.Colors.DIM}none here{ui.Colors.RESET}",
                          value=kind) for kind, label in player_ui.PANELS]
    before = player_ui.shown_panels()
    ticked, picked = prompt.overlay_checklist("Panels", rows, set(before),
                                              [prompt.Choice(title="Arrange panels…", value='arrange')])
    kinds = [k for k in before if k in ticked] + [k for k, _l in player_ui.PANELS if k in ticked and k not in before]
    if kinds != before:
        player_ui.set_panels(kinds)
    if picked == 'arrange':
        player_ui.arrange_start()


def _queue_wheel(key: str) -> bool:
    """A wheel or trackpad scroll over the queue (or chapters) panel: its
    cursor moves a row, as the arrow keys move it. Whether it was one."""
    at = pc.wheel_at() if key in ('SCROLL_UP', 'SCROLL_DOWN') else None
    if not (at and player_ui.in_queue(*at)):
        return False
    player_ui.move_queue_cursor(-1 if key == 'SCROLL_UP' else 1)
    return True


def _queue_key(key: str, session, queue: list, index: int, chapters: list | None = None) -> bool:
    """A queue-panel key (player_queue scope, live while the panel shows):
    move the cursor, or edit the queue at it through `session.edit` (this
    window's session, or the one joined). With the chapters showing instead,
    only the cursor and going to a chapter apply. Returns whether it was one."""
    act = keys.action(key, 'player_queue') if key else None
    if not (act and act.startswith('player_queue.')):
        return False
    name = act.split('.', 1)[1]
    if name in ('up', 'down'):
        player_ui.move_queue_cursor(-1 if name == 'up' else 1)
        return True
    if player_ui.chapters_listed():
        cur = player_ui.queue_cursor()
        if name != 'play':
            return False
        if cur is not None and 0 <= cur < len(chapters or []):
            session.seek_to(chapters[cur][0])
            player_ui.set_queue_cursor(None)
        return True
    cur = player_ui.queue_cursor()
    pos = index if cur is None else cur
    if name in ('shuffle', 'clear', 'undo'):
        session.edit({'shuffle': 'shuffle_upcoming', 'clear': 'clear_upcoming', 'undo': 'undo'}[name])
    elif 0 <= pos < len(queue):
        if name == 'play':
            session.edit('jump', pos=pos, path=queue[pos])
            player_ui.set_queue_cursor(None)
        elif name in ('move_up', 'move_down'):
            delta = -1 if name == 'move_up' else 1
            if 0 <= pos + delta < len(queue):
                session.edit('move', pos=pos, delta=delta, path=queue[pos])
                player_ui.set_queue_cursor(pos + delta)
        elif name == 'remove':
            session.edit('remove', pos=pos, path=queue[pos])
    return True


@contextmanager
def _out_of_view(session):
    """A screen opened from the player (the tag editor, the command line):
    the view let go of meanwhile, so the background tick carries the queue on
    and that screen shows the now-playing box and the app's chrome."""
    from backtrack.playback.session import my_token
    session.release_view(my_token())
    if session is SESSION:
        SESSION.view_attached = False
    player_ui.view_chrome(False)
    drop = pc.screen_backdrop(None)       # the screen it opens is a screen like any other, small windows and all
    try:
        yield
    finally:
        drop()
        player_ui.view_chrome(True)
        session.acquire_view(my_token())
        if session is SESSION:
            SESSION.view_attached = True


def _command_line_detached(session) -> bool:
    """`:` in the player: the command line, its box over the player; out of
    the view only for a screen it opens. Whether it ran (the player is then
    drawn again: its art is an image)."""
    return prompt.open_command_line(around=lambda: _out_of_view(session)) is not None


def _idle_view(woken, volume: int, own_tab: bool = False):
    """The empty player a pinned window shows while nothing plays and another
    window browses the session (#14). Returns True once ``woken()`` says there's
    something to show, False when the other window has gone (back to browse).

    `own_tab`: it's the Now playing tab with nothing playing, which can be left
    like the player (DETACH, or TAB for another tab) and resumes (r) what the
    last run left."""
    from backtrack.playback.session import saved_queue
    from mutagen.id3 import ID3, TIT2, TPE1
    placeholder = ID3()
    placeholder.add(TIT2(text=["Not playing"]))
    # A blank braille cell survives the tag strip, so the artist row keeps its place.
    placeholder.add(TPE1(text=["\u2800"]))
    player_ui.set_queue_context([], 0, [])
    last_sig = None
    if own_tab and saved_queue():
        ui.show_status(f"{keys.label('player.resume', first=True)}: resume where the last run left off",
                       tune.TOAST_MEDIUM_S)
    _drop_backdrop = pc.screen_backdrop(None, small=True)   # it redraws itself at a new size, any size
    with raw_mode(sys.stdin):
      try:
        while True:
            if woken():
                return True
            if not own_tab and not has_other_windows():
                return False
            size = ui.get_terminal_size()
            if size != last_sig or ui.consume_resize():   # a new size, or asked to (a box was over it)
                prog_row, _c, _l, width, _b = draw_full_ui(
                    "", placeholder, IDLE_ART, size, is_paused=True, volume=volume)
                update_progress_ui(prog_row, 0, 0, width)
                last_sig = size
            if pc.float_tick():                # the volume's box went: the art it covered back
                player_ui.redraw_art_image()
            pc.render_status_bar()
            key = get_key_non_blocking() or ''
            act = keys.action(key, 'player') if key else None
            if own_tab and key and (tab := _tab_asked(key)) is not None:
                return {"status": "TAB", "tab": tab}
            if act == 'player.back' and own_tab:
                return {"status": "DETACH"}
            if act == 'player.back':
                ui.show_status('Close the other window to leave the player', tune.TOAST_MEDIUM_S)
            elif act == 'player.panel':
                _choose_panels({})              # nothing playing: each says so, and shows once something does
                last_sig = None
            elif act == 'player.resume' and own_tab:
                from backtrack.menus.play import resume_queue
                resume_queue(view=False)        # it plays; woken() opens the player
                last_sig = None
            elif key == 'FOCUS_IN':
                last_sig = None
            elif key == ':' and (opened := prompt.open_command_line()) is not None:
                sys.stdout.write("\033[?1000h\033[?1006h")   # the command's screens had the mouse
                if opened:
                    ui.clear_screen()
                    last_sig = None
                else:                                          # just the box, put back: the art under it too
                    player_ui.redraw_art_image()
            elif act == 'player.quit':
                raise QuitToTerminal()
            elif pc.is_hints_key(key, key_free=True):
                ui.clear_screen()
                toggle_help()
                last_sig = None
            time.sleep(_LOOP_TICK_S)
      finally:
        _drop_backdrop()


def _empty_view() -> dict:
    """Now playing with nothing playing: the empty player, until something
    plays (PLAYING, for the player to open), it's left (DETACH) or another
    tab is asked for (TAB)."""
    from backtrack.playback import session as sess
    player_ui.view_chrome(True)
    try:
        r = _idle_view(lambda: bool(sess.current_now_playing()), SESSION.get_volume(), own_tab=True)
    finally:
        player_ui.view_chrome(False)
        ui.clear_screen()
    return {"status": "PLAYING"} if r is True else r


_SEEK_STEPS = {'back_1': -1, 'fwd_1': 1, 'back_5': -5, 'fwd_5': 5, 'back_30': -30, 'fwd_30': 30}


def _cramped_key(key: str) -> bool:
    """A player transport key in the miniplayer, acted on: play/pause, next,
    previous, the seek steps and volume. Whether it was one."""
    from backtrack.playback import session as sess
    act = (keys.action(key, 'player') or '').removeprefix('player.')
    a = sess.active_session()
    if act == 'playpause':
        a.pause_toggle()
    elif act in ('next', 'prev'):
        a.next() if act == 'next' else a.prev()
    elif act in _SEEK_STEPS:
        a.seek(_SEEK_STEPS[act])
    elif act in ('vol_up', 'vol_down'):
        a.set_volume(max(0, min(100, a.get_volume() + (5 if act == 'vol_up' else -5))))
        player_ui.volume_changed()
    else:
        return False
    return True


def cramped_view() -> None:
    """A window too small for any screen's boxes: the miniplayer (the
    player's tiny transport, boxed while it can be) until they fit again.
    Its keys work (play/pause, next, previous) and `:`; nothing reaches the
    screen it stands in for."""
    from backtrack.playback.session import current_now_playing
    fd = sys.stdin.fileno()
    drawn: list = [None]
    prog_row = [1]

    def _draw() -> None:
        np = current_now_playing() or {}
        size, paused = ui.get_terminal_size(), bool(np.get('paused', True))
        if (size, paused) != drawn[0]:
            prog_row[0] = player_ui._draw_tiny(size, paused)[0]
            drawn[0] = (size, paused)
        player_ui.update_progress_ui(prog_row[0], float(np.get('elapsed') or 0.0),
                                     float(np.get('duration') or 0.0), size[0])

    def _redrawn() -> None:
        drawn[0] = None
        _draw()

    drop = pc.screen_backdrop(_redrawn, small=True)      # the miniplayer is what's under `:` here
    try:
        while not pc.box_fits():
            _draw()
            if not pc._wait_for_keypress(_LOOP_TICK_S):
                continue
            key = pc._read_key(fd)
            if key == ':':
                if prompt.open_command_line() is not None:
                    drawn[0] = None                      # the box, or a screen it opened, came and went
            elif not _cramped_key(key):
                prompt.consume_chrome(key, {})           # the app's transport keys; anything else does nothing
    finally:
        drop()


def now_playing_tab() -> None:
    """The Now playing tab: the player, or the empty player while nothing
    plays, which opens into the player when something starts."""
    from backtrack.playback import session as sess
    while True:
        if sess.current_now_playing():
            show_player()
            return
        if _on_now_playing(_empty_view).get("status") != "PLAYING":
            return


def _step_volume(remote, target, delta: int):
    """A +/- press in a joined window. The host's volume only reaches this
    window with its next snapshot (a quarter-second later), so quick presses
    step from the level just asked for, not the stale one, so five fast presses
    are +25, not +5. Returns the new (level, time)."""
    now = time.time()
    base = target[0] if target and now - target[1] < 1.0 else remote.get_volume()
    level = max(0, min(100, base + delta))
    remote.set_volume(level)
    return (level, now)


def open_client_player_view() -> dict:
    """The player view for a *joined* window; see _client_view."""
    return _on_now_playing(_client_view)


def _client_view() -> dict:
    """Full player view for a *joined* window (#14): renders the host's
    current track from its snapshots + the track file on the shared disk, with
    transport routed to the host. Elapsed is interpolated between snapshots for a
    smooth progress bar. No lyrics pane on a client (see player_ui `lyrics_pane`)."""
    from backtrack.playback import session as sess

    remote = sess.active_session()
    np0 = remote.now_playing()
    if np0 is None:
        return {"status": "OK"}
    token = sess.my_token()
    # Exactly one player view may exist across the whole session. If another
    # window already holds it, don't open a second one (#14).
    holder = np0.get('view_holder')
    if holder and holder != token:
        ui.show_status("The player is open in another window.")
        return {"status": "DETACH"}
    remote.acquire_view(token)

    last_sig = None
    audio = None
    duration = 0.0
    chapters: list = []
    chapter_i = -1
    next_scroll = 0.0                      # when a scrolling title next moves (ui.marquee)
    vol_target = None                      # (level, time) of the last +/- press
    volume = sess.clamp_volume(np0.get('volume'))
    prog_row = 0
    ctrl_row = 0
    width = ui.get_terminal_size()[0]
    player_ui._ui_state['lyrics_pane'] = False
    player_ui.refresh_player_settings()
    player_ui.view_chrome(True)
    _drop_backdrop = pc.screen_backdrop(None, small=True)   # it redraws itself at a new size, any size
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
                    # Host stopped / no track. Pinned open while the other window
                    # browses (#14): wait on the empty player, keeping the view lock.
                    if has_other_windows() and _idle_view(
                            lambda: sess.active_session() is not remote or remote.now_playing() is not None,
                            volume):
                        last_sig = None
                        continue
                    return {"status": "OK"}
                volume = sess.clamp_volume(np.get('volume'))
                # Another window won a simultaneous grab or took the view: never
                # show a second player; step back to the mirror (#14).
                if np.get('view_holder') not in (None, token):
                    return {"status": "DETACH"}
                fp = np['file_path']
                size = ui.get_terminal_size()
                if ui.consume_resize():              # asked to lay out again (a box was over it)
                    last_sig = None
                player_ui.set_queue_context(np.get('titles') or [], int(np.get('index') or 0), np.get('queue') or [])
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
                scroll_due = _ui_state.get('scrolling') and time.monotonic() >= next_scroll
                if sig != last_sig or scroll_due:
                    next_scroll = time.monotonic() + ui.MARQUEE_STEP_S
                    if last_sig is None or fp != last_sig[0] or np.get('generation') != last_sig[3]:
                        audio = sess.player_tags(fp)       # as the host player reads it
                        with quietly():
                            accents.apply(fp)              # this window follows the host's track
                        chapters = library_entry(fp).get('chapters') or []
                        chapter_i = chapter_at(chapters, float(np.get('elapsed') or 0.0))
                        player_ui.set_chapter(chapters, chapter_i)
                    _ui_state['book'] = bool(np.get('is_book'))
                    duration = float(np.get('duration') or 0.0)
                    prog_row, ctrl_row, _lr, width, _br = draw_full_ui(
                        fp, audio, None, size, is_paused=bool(np.get('paused')),
                        volume=volume)
                    last_sig = sig
                if pc.float_tick():                # the volume's box went: the art it covered back
                    player_ui.redraw_art_image()
                pc.render_status_bar()

                elapsed = float(np.get('elapsed') or 0.0)
                rate = float(np.get('rate') or 1.0)
                if not np.get('paused'):
                    elapsed += rate * max(0.0, time.time() - (remote.latest_at() or time.time()))
                if duration:
                    elapsed = min(elapsed, duration)
                if chapter_at(chapters, elapsed) != chapter_i:
                    chapter_i = chapter_at(chapters, elapsed)
                    player_ui.set_chapter(chapters, chapter_i)
                    last_sig = None                        # the chapter line and panel move on
                update_progress_ui(prog_row, elapsed, duration, width, chapters,
                                   player_ui.timer_extra(rate, np.get('sleep_left')))

                key = get_key_non_blocking()
                if key and player_ui.arrange_key(key):     # arranging the panels: its keys first
                    last_sig = None
                    continue
                if key and (tab := _tab_asked(key)) is not None:
                    return {"status": "TAB", "tab": tab}
                if key:
                    act = None
                    if key.startswith('MOUSE_CLICK:'):
                        _mp = key.split(':'); _mr = int(_mp[2]); _mc = int(_mp[3])
                        _act = player_ui.transport_click_action(_mr, _mc, ctrl_row)
                        _qi = player_ui.queue_click_index(_mr, _mc)
                        _q = np.get('queue') or []
                        if _act in ('prev', 'next', 'playpause'):
                            act, key = f'player.{_act}', ''
                        elif _qi is not None and _qi[0] == 'chapters':
                            if _qi[1] < len(chapters):
                                remote.seek_to(chapters[_qi[1]][0])
                            key = ''
                        elif _qi is not None and _qi[1] < len(_q):
                            remote.edit('jump', pos=_qi[1], path=_q[_qi[1]]); key = ''
                        else:
                            _frac = player_ui.progress_from_click(_mr, _mc)
                            if _frac is not None and duration:
                                # Click anywhere on the bar to jump there (the
                                # session only takes relative seeks).
                                remote.seek(_frac * duration - elapsed); key = ''
                            elif chapters and player_ui.time_clicked(_mr, _mc):
                                act, key = 'player.chapter_time', ''
                            else:
                                _hk = player_ui.hint_click_key(_mr, _mc)
                                key = _hk or ''
                    if _ui_state.get('show_queue') and _queue_wheel(key):
                        last_sig = None                # redrawn with the cursor where it moved
                        continue
                    if _ui_state.get('show_queue') and _queue_key(
                            key, remote, np.get('queue') or [], int(np.get('index') or 0), chapters):
                        last_sig = None                # the host's edit arrives in the next snapshot
                        continue
                    act = act or (keys.action(key, 'player') if key else None)
                    if key == 'FOCUS_IN':
                        last_sig = None                # force a full redraw
                    elif key == ':' and _command_line_detached(remote):
                        sys.stdout.write("\033[?1000h\033[?1006h")
                        ui.clear_screen()
                        last_sig = None
                    elif act == 'player.playpause':
                        remote.pause_toggle()
                    elif (step := _seek_step(key, duration, elapsed)) is not None:
                        remote.seek(step[0])
                    elif act == 'player.next':
                        remote.next()
                    elif act == 'player.prev':
                        remote.prev()
                    elif _speed_or_sleep(act, remote, rate):
                        pass
                    elif done := _track_key(act, fp, remote):
                        if isinstance(done, dict):
                            return done
                        last_sig = None
                    elif act == 'player.chapter_time' and chapters:
                        player_ui.toggle_chapter_time()
                        last_sig = None
                    elif act == 'player.vol_up':
                        vol_target = _step_volume(remote, vol_target, +5)
                        player_ui.volume_changed()
                    elif act == 'player.vol_down':
                        vol_target = _step_volume(remote, vol_target, -5)
                        player_ui.volume_changed()
                    elif act == 'player.back':
                        # Pinned open while another window browses this session
                        # (#14): the two windows stay specialised until one closes.
                        if has_other_windows():
                            ui.show_status('Close the other window to leave the player', tune.TOAST_MEDIUM_S)
                        else:
                            return {"status": "DETACH"}
                    elif act == 'player.stop':
                        remote.stop()
                        if not has_other_windows():
                            return {"status": "STOP"}  # else the next snapshot goes idle
                    elif act == 'player.quit':
                        raise QuitToTerminal()
                    elif pc.is_hints_key(key, key_free=True) or act == 'player.meta':
                        ui.clear_screen()
                        (toggle_metadata if act == 'player.meta' else toggle_help)()
                        last_sig = None                # redraw with the new layout
                    elif act == 'player.tabs' and nav.TABS:
                        ui.clear_screen()
                        player_ui.toggle_tabs()
                        last_sig = None
                    elif act == 'player.panel' and audio is not None:
                        _choose_panels({'lyrics': False, 'people': bool(audio.getall('TMCL') or audio.getall('TIPL')),
                                        'queue': player_ui.has_queue(), 'chapters': bool(chapters)})
                        ui.clear_screen()
                        last_sig = None
                time.sleep(_LOOP_TICK_S)
    finally:
        _drop_backdrop()
        player_ui._ui_state['lyrics_pane'] = True
        player_ui.view_chrome(False)
        sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse on exit
        sys.stdout.flush()
        remote.release_view(token)
        ui.clear_screen()


def _player_view_loop() -> dict:
    """The foreground render + input loop for the attached session."""
    mp = SESSION.mp
    assert mp is not None

    # --- per-track render state (rebuilt by _prepare() on every track) ---
    track_path = ""
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
    chapters: list = []
    chapter_i = -1

    # --- view / loop state ---
    last_size = ui.get_terminal_size()
    resize_pending = False
    resize_timer = 0.0
    prog_row = ctrl_row = lyric_row = art_bottom_row = 0
    pane = None
    current_width = last_size[0]
    last_q_sig: tuple | None = None
    next_scroll = 0.0                    # when a scrolling title next moves (ui.marquee)

    def _prepare() -> None:
        """(Re)load per-track render state from the session for the current track."""
        nonlocal track_path, audio, duration, pre_art, sylt_data, uslt_lines, line_times
        nonlocal is_uslt, has_credits, has_lyrics, dialogue_state
        nonlocal pane, chapters, chapter_i
        t = SESSION.track()
        fp = track_path = t.file_path
        audio = t.audio
        duration = t.duration
        chapters = t.chapters
        chapter_i = chapter_at(chapters, SESSION.elapsed())
        player_ui.set_chapter(chapters, chapter_i)
        _ui_state['book'] = t.is_book
        # Keep the in-player queue pane ('w' cycle) in sync with the session queue.
        player_ui.set_queue_context(t.titles, t.index, t.queue)
        pre_art = GROUP_COVER if t.is_grouping else None
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
        # choosing it, so the 'm' panel then names the real source rather than a
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
        player_ui.set_lyric_sources(fp, _srcs, _est)
        pane = lyric_pane.Pane(_tl) if _tl else None
        # Say it out loud. Paced-out timing looks right for the first minute and is
        # a line adrift by the last, which is not something a still screen shows.
        if dialogue_state.is_active() and dialogue_state.timing_source == 'estimated':
            ui.show_status("No transcript, so the lyric timing is a guess", tune.TOAST_LONG_S)

        if audio and audio.getall('EQU2'):
            ui.show_status("Equaliser on", tune.TOAST_LONG_S)

    def _redraw_full() -> None:
        """Full-screen redraw for the current track + view state; sets row positions."""
        nonlocal prog_row, ctrl_row, lyric_row, current_width, art_bottom_row
        vol = SESSION.get_volume()
        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
            track_path, audio, pre_art, last_size,
            is_paused=SESSION.is_paused(), volume=vol,
        )

        if pane and player_ui.lyrics_laid_out():
            # A full redraw has just wiped the screen, so what the pane last
            # painted is gone. Its record is of rows it wrote, not rows that
            # survived, so it is told, and repaints in step with the rest of the
            # UI rather than a tick later.
            _mp = getattr(SESSION, 'mp', None)
            _ms = _mp.get_time() if _mp is not None else 0
            pane.paint(sys.stdout, (_ms / 1000.0) if _ms and _ms > 0 else 0.0,
                       _pane_geometry(), force=True)
            sys.stdout.flush()


    def _pane_geometry() -> lyric_pane.Geometry:
        """Where the last full redraw left room for the lyric pane."""
        return lyric_pane.Geometry(
            row=lyric_row,
            col=geom.lyric_left or geom.right_left or 1,
            width=geom.lyric_width or geom.right_width or current_width,
            bottom=art_bottom_row or last_size[1],
            centre=geom.lyric_centre)

    def update_ctrl_ui() -> None:
        """Redraw just the transport/status line in place (see #86)."""
        status_ln, _ = _controls_line(
            is_uslt, SESSION.is_paused(), SESSION.get_volume(),
            has_lyrics=has_lyrics, has_credits=has_credits)
        player_ui.write_controls(ctrl_row, status_ln)     # through the painter, cell by cell
        sys.stdout.flush()

    player_ui.set_resizing(False)      # in case the last visit ended mid-resize
    with raw_mode(sys.stdin):
        sys.stdout.write("\033[?1000h\033[?1006h")   # enable mouse (click + scroll)
        sys.stdout.flush()
        _prepare()
        _redraw_full()
        last_track_sig = SESSION.generation

        def _relaid() -> None:
            """The whole player at the current size: the frame, the controls
            and the progress, as the loop draws them (under a box over it)."""
            nonlocal last_size
            last_size = ui.get_terminal_size()
            _redraw_full()
            update_ctrl_ui()
            ms = mp.get_time()
            update_progress_ui(prog_row, ms / 1000.0 if ms >= 0 else 0.0, duration, current_width, chapters,
                               player_ui.timer_extra(SESSION.rate, SESSION.sleep_left()))
        _drop_backdrop = pc.screen_backdrop(_relaid, small=True)   # the player lays out a small window itself
        try:
          while True:
            if not SESSION.is_active():
                return {"status": "OK"}

            # Redraw at every size the window passes through, as soon as it's
            # seen: the gap in which the terminal shows the old frame reflowed
            # is one loop tick, not a debounce window. Mid-resize the art image
            # is only the quick preview; once the size has held still for
            # ART_FULL_IMAGE_SETTLE_S, the full-quality image goes on top.
            current_size = ui.get_terminal_size()
            if current_size != last_size:
                last_size = current_size
                resize_pending = True
                resize_timer = time.time()
                player_ui.set_resizing(True)
                # The terminal reflowed the old frame. No separate clear: the
                # painter sees the new size and wipes in the same write as the
                # new rows, so a drag doesn't flash blank at every step.
                _t0 = time.monotonic()
                _redraw_full()
                log.debug("player redraw at %sx%s took %.0f ms (%.0f ms after the signal)",
                          last_size[0], last_size[1], (time.monotonic() - _t0) * 1000,
                          ui.ms_since_resize_signal())
            elif resize_pending and (time.time() - resize_timer > tune.ART_FULL_IMAGE_SETTLE_S):
                resize_pending = False
                player_ui.set_resizing(False)
                player_ui.redraw_art_image()
                log.debug("player resize settled at %sx%s", last_size[0], last_size[1])
            elif not resize_pending and player_ui.art_image_incomplete():
                # A resize cut the full image short but didn't change the size
                # (or already settled): send it again.
                player_ui.redraw_art_image()

            if pc.float_tick():                # the volume's box went: the art it covered back
                player_ui.redraw_art_image()
            pc.render_status_bar()

            current_track_sig = SESSION.generation
            if current_track_sig != last_track_sig:
                last_track_sig = current_track_sig
                _prepare()
                _redraw_full()
                continue

            # End-of-track / auto-advance is owned by the session.
            adv = SESSION.tick()
            if adv == 'stopped':
                return {"status": "OK"}
            if adv == 'slept':
                update_ctrl_ui()
            if adv == 'changed':
                last_track_sig = SESSION.generation
                _prepare()
                _redraw_full()
                continue

            # Live-refresh the queue pane when the queue changes mid-track (e.g.
            # another window queued a song), not only on track change.
            t = SESSION.track()
            q_sig = (tuple(t.titles), t.index)
            if q_sig != last_q_sig:
                last_q_sig = q_sig
                player_ui.set_queue_context(t.titles, t.index, t.queue)
                if _ui_state.get('show_queue'):
                    _redraw_full()

            elapsed_ms = mp.get_time()
            elapsed = elapsed_ms / 1000.0 if elapsed_ms >= 0 else 0.0
            if chapter_at(chapters, elapsed) != chapter_i:
                chapter_i = chapter_at(chapters, elapsed)
                player_ui.set_chapter(chapters, chapter_i)
                _redraw_full()                     # the chapter line and panel move on

            key = get_key_non_blocking()
            if key and player_ui.arrange_key(key):         # arranging the panels: its keys first
                _redraw_full()
                continue
            if key and (tab := _tab_asked(key)) is not None:
                return {"status": "TAB", "tab": tab}
            if key:
                act = None
                if key.startswith('MOUSE_CLICK:'):
                    # Map clicks on the transport icons to their action, and on the
                    # volume bar, progress bar or hint glyphs to what they do.
                    _mp = key.split(':'); _mr = int(_mp[2]); _mc = int(_mp[3])
                    _act = player_ui.transport_click_action(_mr, _mc, ctrl_row)
                    _qi = player_ui.queue_click_index(_mr, _mc)
                    if _act in ('prev', 'next', 'playpause'):
                        act, key = f'player.{_act}', ''
                    elif _qi is not None and _qi[0] == 'chapters':
                        if _qi[1] < len(chapters):
                            SESSION.seek_to(chapters[_qi[1]][0])
                        key = ''
                    elif _qi is not None:
                        # A track row in the queue pane: play it. The track
                        # change is picked up (and redrawn) at the top of the loop.
                        _t = SESSION.track()
                        if _qi[1] != _t.index and _qi[1] < len(_t.queue):
                            SESSION.edit('jump', pos=_qi[1], path=_t.queue[_qi[1]])
                        key = ''
                    else:
                        _frac = player_ui.progress_from_click(_mr, _mc)
                        if _frac is not None and duration:
                            # Click anywhere on the bar to jump there; SESSION only
                            # takes relative seeks, so aim from where we are.
                            _tgt = _frac * duration
                            SESSION.seek(_tgt - elapsed)
                            ui.show_status(f'Seek to {ui.format_time(int(_tgt))}', tune.TOAST_SHORT_S)
                            update_ctrl_ui()
                            key = ''
                        elif chapters and player_ui.time_clicked(_mr, _mc):
                            act, key = 'player.chapter_time', ''
                        else:
                            _hk = player_ui.hint_click_key(_mr, _mc)
                            key = _hk or ''

                if _ui_state.get('show_queue'):
                    if _queue_wheel(key):
                        _redraw_full()
                        continue
                    _t = SESSION.track()
                    if _queue_key(key, SESSION, _t.queue, _t.index, chapters):
                        _t = SESSION.track()
                        player_ui.set_queue_context(_t.titles, _t.index, _t.queue)
                        last_q_sig = (tuple(_t.titles), _t.index)
                        _redraw_full()
                        continue
                act = act or (keys.action(key, 'player') if key else None)
                if key == 'FOCUS_OUT':
                    pass
                elif key == 'FOCUS_IN':
                    _redraw_full()
                elif key == ':' and _command_line_detached(SESSION):
                    sys.stdout.write("\033[?1000h\033[?1006h")   # the command's screens had the mouse
                    ui.clear_screen()
                    _redraw_full()
                elif act == 'player.playpause':
                    SESSION.pause_toggle()
                    time.sleep(_KEY_POLL_INTERVAL_S)
                    update_ctrl_ui()
                elif (step := _seek_step(key, duration, elapsed)) is not None:
                    SESSION.seek(step[0])
                    ui.show_status(step[1], tune.TOAST_SHORT_S)
                    update_ctrl_ui()
                elif act == 'player.next':        # skip, stay in the view
                    if SESSION.next(manual=True) is None:
                        return {"status": "OK"}   # was the last track: queue finished
                    _prepare(); _redraw_full(); continue
                elif act == 'player.prev':        # previous track (or restart this one)
                    if SESSION.prev() is not None:
                        _prepare(); _redraw_full()
                    continue
                elif act == 'player.back':        # minimise, keep playing (#14)
                    # Pinned open while another window is browsing this session:
                    # the two windows stay specialised until one closes (#14).
                    if has_other_windows():
                        ui.show_status('Close the other window to leave the player', tune.TOAST_MEDIUM_S)
                        update_ctrl_ui(); continue
                    return {"status": "DETACH"}
                elif act == 'player.stop':
                    SESSION.stop()
                    return {"status": "STOP"}
                elif act == 'player.quit':
                    raise QuitToTerminal()
                elif _speed_or_sleep(act, SESSION, SESSION.rate):
                    pass
                elif done := _track_key(act, track_path, SESSION):
                    if isinstance(done, dict):
                        return done
                    _prepare()                     # the tags may have changed
                    _redraw_full()
                elif act == 'player.chapter_time' and chapters:
                    player_ui.toggle_chapter_time()
                    update_ctrl_ui()
                elif act == 'player.vol_up':
                    SESSION.set_volume(SESSION.get_volume() + 5)
                    player_ui.volume_changed()
                elif act == 'player.vol_down':
                    SESSION.set_volume(SESSION.get_volume() - 5)
                    player_ui.volume_changed()
                elif pc.is_hints_key(key, key_free=True):
                    ui.clear_screen()
                    toggle_help()
                    _redraw_full()
                elif act == 'player.panel':
                    _choose_panels({'lyrics': has_lyrics, 'people': has_credits,
                                    'queue': player_ui.has_queue(), 'chapters': bool(chapters)})
                    ui.clear_screen()
                    _redraw_full()
                elif act == 'player.meta':
                    ui.clear_screen()
                    toggle_metadata()
                    _redraw_full()
                elif act == 'player.tabs' and nav.TABS:
                    ui.clear_screen()
                    player_ui.toggle_tabs()
                    _redraw_full()

            update_progress_ui(prog_row, elapsed, duration, current_width, chapters,
                               player_ui.timer_extra(SESSION.rate, SESSION.sleep_left()))
            if _ui_state.get('scrolling') and time.monotonic() >= next_scroll:
                next_scroll = time.monotonic() + ui.MARQUEE_STEP_S
                _redraw_full()                    # a title too long for its room moves on

            if pane and player_ui.lyrics_laid_out():
                # Every tick takes the same path: the frame is a function of the
                # clock and the geometry and nothing else. A seek is just a
                # different number arriving, a resize just a different box;
                # neither is an event anyone has to notice, and neither can leave
                # the words disagreeing with the audio. Only rows that differ get
                # written, so an unchanged frame costs nothing.
                pane.paint(sys.stdout, elapsed, _pane_geometry())
                sys.stdout.flush()

            time.sleep(_LOOP_TICK_S)
        finally:
            _drop_backdrop()
            sys.stdout.write("\033[?1000l\033[?1006l")   # disable mouse on exit
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        music_player(file_path=sys.argv[1])
    else:
        sys.stdout.write("Usage: python -m backtrack.playback.player <file_path>\n")
