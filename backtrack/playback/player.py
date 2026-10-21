"""The interactive player *view* over the shared PlaybackSession (feature #14).

Audio ownership, the queue, and the track lifecycle live in ``session.py``; this
module is the foreground renderer/controller. Opening a track starts the shared
session and attaches this view; **leaving the view (b/Esc) keeps the session
playing in the background**; only Stop (s) ends it. The session's background
tick advances the queue and logs history whether or not this view is attached.
"""
from __future__ import annotations
import sys

from backbone import nav
from backbone.prompt import disable_mouse, enable_mouse
from backbone.nav import QuitToTerminal
import time
from contextlib import contextmanager
from dataclasses import dataclass

from backbone import keys, prompt, ui
from backtrack.lyrics import lyric_pane
from backtrack.playback.player_ui import (
    _controls_line,
    draw_full_ui,
    update_progress_ui,
    _ui_state,
    toggle_metadata,
    toggle_help,
)
from backtrack.playback import panels, player_ui
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
from backtrack import theme, tuning as tune
from backtrack.config import load_config, setting

_KEY_POLL_INTERVAL_S = tune.KEY_POLL_INTERVAL_S
_LOOP_TICK_S = tune.LOOP_TICK_S


def _show_load_error(file_path: str) -> None:
    """Show a 'could not load' screen and wait for a keypress."""
    ui.clear_screen()
    sys.stdout.write(f"{ui.C.ERROR}Playback Error:{ui.C.RESET} Could not load:\n")
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
    tab: from any other tab, show that tab instead, returning once that tab is
    shown again.
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
            if not _idle_view(SESSION.is_active):
                return {"status": "OK"}
    finally:
        player_ui.view_chrome(False)
        SESSION.view_attached = False
        SESSION.release_view(my_token())
        ui.clear_screen()


def _seek_step(key: str, duration: float, elapsed: float) -> tuple[float, str] | None:
    """The seek a player key asks for, as (seconds to move, toast), or None when
    the key isn't a seek. The near-end key, with Diagnostics on, jumps there."""
    act = keys.action(key, 'player') if key else None
    if act == 'player.near_end' and player_ui._ui_state['debug']:
        return (duration - tune.NEAR_END_JUMP_S) - elapsed, f'Skip to last {tune.NEAR_END_JUMP_S}s'
    elif (act or '').removeprefix('player.') in tune.SEEK_STEPS_S:
        secs = tune.SEEK_STEPS_S[act.removeprefix('player.')]
    else:
        return None
    return secs, f"Seek {'Forward +' if secs > 0 else 'Backward -'}{abs(secs)}s"


def _speed_or_sleep(act: str | None, session, rate: float, chapters: list) -> bool:
    """The speed and sleep-timer keys, for the host player and a joined window's.
    Returns whether `act` was one."""
    if act in ('player.slower', 'player.faster'):
        r = session.set_rate(rate + (tune.RATE_STEP if act == 'player.faster' else -tune.RATE_STEP))
        ui.show_status(f'Speed ×{r:g}' if r is not None else 'Speed is for audiobooks', tune.TOAST_SHORT_S)
        return True
    if act == 'player.sleep':
        mode = session.cycle_sleep()
        end = 'chapter' if chapters else 'track'
        ui.show_status('Sleep timer off' if mode is None else f'Sleep at the end of the {end}'
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
    enable_mouse()      # the editor had the mouse
    ui.clear_screen()
    return True


def _panel_act(act: str | None) -> bool:
    """A key for one of the panels (the visualiser's style, the booklet's
    pages): done, and whether the frame wants redrawing."""
    if act == 'player.visualiser' and 'visualiser' in player_ui.shown_panels():
        ui.show_status(f"Visualiser: {player_ui.cycle_visualiser()}", tune.TOAST_SHORT_S)
        return True
    if act in ('player.page_prev', 'player.page_next'):
        return player_ui.turn_page(1 if act == 'player.page_next' else -1)
    return False


def _panel_clicked(row: int, col: int) -> bool:
    """A click on the booklet turns its page; whether it was one (and the frame
    wants redrawing)."""
    step = player_ui.page_click(row, col)
    return step is not None and player_ui.turn_page(step)


@dataclass
class _Input:
    """What a key or click in the player acts on: this window's session (its
    own, or a link to the host's) and the track as it stands."""
    session: object
    joined: bool
    elapsed: float
    duration: float
    rate: float
    chapters: list
    ctrl_row: int
    queue: list
    index: int
    path: str
    audio: object
    has_lyrics: bool


def _clicked(key: str, v: _Input) -> tuple:
    """A click in the player as (action, key, outcome): a transport icon's
    action; a chapter, queue row, booklet half or the progress bar acted on at
    once; a hint's key to carry on with."""
    _mp = key.split(':'); row, col = int(_mp[2]), int(_mp[3])
    transport = player_ui.transport_click_action(row, col, v.ctrl_row)
    listed = player_ui.queue_click_index(row, col)
    if transport in ('prev', 'next', 'playpause'):
        return f'player.{transport}', '', None
    if listed is not None and listed[0] == 'chapters':
        if listed[1] < len(v.chapters):
            v.session.seek_to(v.chapters[listed[1]][0])
        return None, '', None
    if _panel_clicked(row, col):
        return None, '', 'redraw'
    if listed is not None:
        # A track row in the queue: play it (the track change is picked up as
        # any other).
        if listed[1] != v.index and listed[1] < len(v.queue):
            v.session.edit('jump', pos=listed[1], path=v.queue[listed[1]])
        return None, '', None
    frac = player_ui.progress_from_click(row, col)
    if frac is not None and v.duration:
        # Anywhere on the bar jumps there; sessions only take relative seeks.
        target = frac * v.duration
        v.session.seek(target - v.elapsed)
        ui.show_status(f'Seek to {ui.format_time(int(target))}', tune.TOAST_SHORT_S)
        return None, '', 'ctrl'
    if v.chapters and player_ui.time_clicked(row, col):
        return 'player.chapter_time', '', None
    return None, player_ui.hint_click_key(row, col) or '', None


def _player_input(key: str, v: _Input):
    """One key or click in the player, for the window playing and a joined one
    alike. What it did, for the loop to follow up: None, 'ctrl' (the controls
    changed), 'redraw' (the frame), 'queue' (the queue was edited), 'track'
    (another track, or this one's tags), or a dict to leave the player with."""
    if player_ui.arrange_key(key):                       # arranging the panels: its keys first
        return 'redraw'
    if (tab := _tab_asked(key)) is not None:
        return {"status": "TAB", "tab": tab}
    act, outcome = None, None
    if key.startswith('MOUSE_CLICK:'):
        act, key, outcome = _clicked(key, v)
        if outcome:
            return outcome
    if _ui_state.get('show_queue'):
        if _queue_wheel(key):
            return 'redraw'
        if _queue_key(key, v.session, v.queue, v.index, v.chapters):
            return 'queue'
    act = act or (keys.action(key, 'player') if key else None)
    s = v.session
    if key == 'FOCUS_IN':
        return 'redraw'
    if keys.pressed(key, 'global.command_line') and _command_line_detached(s):
        enable_mouse()      # the command's screens had the mouse
        ui.clear_screen()
        return 'redraw'
    if act == 'player.playpause':
        s.pause_toggle()
        if not v.joined:
            time.sleep(_KEY_POLL_INTERVAL_S)             # VLC's state, so the icon's right
        return 'ctrl'
    if (step := _seek_step(key, v.duration, v.elapsed)) is not None:
        s.seek(step[0])
        ui.show_status(step[1], tune.TOAST_SHORT_S)
        return 'ctrl'
    if act == 'player.next':                             # skip, stay in the view
        if s.next(manual=True) is None and not v.joined:
            return {"status": "OK"}                      # was the last track: queue finished
        return 'track'
    if act == 'player.prev':                             # previous track (or restart this one)
        return 'track' if s.prev() is not None or v.joined else None
    if act == 'player.back':                             # minimise, keep playing (#14)
        # Pinned open while another window is browsing this session: the two
        # windows stay specialised until one closes (#14).
        if has_other_windows():
            ui.show_status('Close the other window to leave the player', tune.TOAST_MEDIUM_S)
            return 'ctrl'
        return {"status": "DETACH"}
    if act == 'player.stop':
        s.stop()
        if v.joined and has_other_windows():
            return 'redraw'                              # the next snapshot goes idle
        return {"status": "STOP"}
    if act == 'player.quit':
        raise QuitToTerminal()
    if _speed_or_sleep(act, s, v.rate, v.chapters):
        return None
    if done := _track_key(act, v.path, s):
        # The tags may have changed, and the queue may have ended while the
        # editor was open.
        return done if isinstance(done, dict) else 'track'
    if act == 'player.chapter_time' and v.chapters:
        player_ui.toggle_chapter_time()
        return 'ctrl'
    if keys.pressed(key, 'volume.up') or keys.pressed(key, 'volume.down'):
        player_ui.step_volume(s, keys.pressed(key, 'volume.up'))
        return None
    if pc.is_hints_key(key, key_free=True) or act == 'player.meta':
        ui.clear_screen()
        (toggle_metadata if act == 'player.meta' else toggle_help)()
        return 'redraw'
    if act == 'player.tabs' and nav.TABS:
        ui.clear_screen()
        player_ui.toggle_tabs()
        return 'redraw'
    if act == 'player.panel' and v.audio is not None:
        _choose_panels(player_ui.track_has(v.audio, v.path, any(player_ui.people_of(v.audio)), v.has_lyrics),
                       player_ui.panel_notes(v.audio))
        return 'redraw'                                  # the panels as ticked
    if _panel_act(act):
        return 'redraw'
    return None


def _choose_panels(has: dict, notes: dict | None = None) -> None:
    """`w`: which panels show, ticked in a box over the player (one this track
    has nothing for says so, one that would only repeat another is greyed with
    why, and each stays as set for the tracks that do), and Arrange, to move
    them around."""
    dim, reset = ui.Colors.DIM, ui.Colors.RESET
    notes = notes or {}

    def title(kind: str, label: str) -> str:
        if kind in notes:
            return f"{dim}{label}  {notes[kind]}{reset}"
        return label if has.get(kind) else f"{label}  {dim}none here{reset}"
    rows = [prompt.Choice(title=title(kind, label), value=kind) for kind, label in player_ui.PANELS]
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
    the view only for a screen it opens. Whether one opened (the player is
    then drawn again; for just the box, what it covered is put back)."""
    return bool(prompt.open_command_line(around=lambda: _out_of_view(session)))


def _idle_view(woken, own_tab: bool = False):
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
    def _relaid() -> None:
        """The empty player at the current size (under a box over it, too)."""
        nonlocal last_sig
        size = ui.get_terminal_size()
        prog_row, _c, _l, width, _b = draw_full_ui("", placeholder, IDLE_ART, size, is_paused=True)
        update_progress_ui(prog_row, 0, 0, width)
        last_sig = size

    _drop_backdrop = pc.screen_backdrop(_relaid, small=True)   # it lays out a small window itself
    with raw_mode(sys.stdin):
      try:
        while True:
            if woken():
                return True
            if not own_tab and not has_other_windows():
                return False
            if ui.get_terminal_size() != last_sig or ui.consume_resize():   # a new size, or asked to
                _relaid()
            pc.render_status_bar()
            key = get_key_non_blocking() or ''
            if key and player_ui.arrange_key(key):     # arranging the panels: its keys first
                last_sig = None
                continue
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
            elif keys.pressed(key, 'global.command_line') and prompt.open_command_line():   # a screen opened (just the box: put back already)
                enable_mouse()   # the command's screens had the mouse
                ui.clear_screen()
                last_sig = None
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
        r = _idle_view(lambda: bool(sess.current_now_playing()), own_tab=True)
    finally:
        player_ui.view_chrome(False)
        ui.clear_screen()
    return {"status": "PLAYING"} if r is True else r


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
    elif act in tune.SEEK_STEPS_S:
        a.seek(tune.SEEK_STEPS_S[act])
    elif keys.pressed(key, 'volume.up') or keys.pressed(key, 'volume.down'):
        player_ui.step_volume(a, keys.pressed(key, 'volume.up'))
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
            if keys.pressed(key, 'global.command_line'):
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
    prog_row = 0
    ctrl_row = 0
    width = ui.get_terminal_size()[0]
    player_ui._ui_state['lyrics_pane'] = False
    player_ui.refresh_player_settings()
    player_ui.view_chrome(True)
    elapsed = 0.0
    shown: dict = {}                       # what the last frame drew: the track, and whether it was paused

    def _relaid() -> None:
        """The joined player at the current size as last drawn (under a box over it, too)."""
        nonlocal prog_row, ctrl_row, width, last_sig
        if not shown:
            return
        size = ui.get_terminal_size()
        prog_row, ctrl_row, _lr, width, _br = draw_full_ui(shown['fp'], audio, None, size,
                                                           is_paused=shown['paused'])
        update_progress_ui(prog_row, elapsed, duration, width, chapters)
        if last_sig is not None:
            last_sig = (*last_sig[:-1], size)

    _drop_backdrop = pc.screen_backdrop(_relaid, small=True)   # it lays out a small window itself
    try:
        with raw_mode(sys.stdin):
            enable_mouse()
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
                            lambda: sess.active_session() is not remote or remote.now_playing() is not None):
                        last_sig = None
                        continue
                    return {"status": "OK"}
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
                    np.get('generation'),
                    int(np.get('index') or 0),
                    len(np.get('queue') or []),
                    tuple(np.get('titles') or []),
                    size,
                )
                scroll_due = _ui_state.get('scrolling') and time.monotonic() >= next_scroll
                if sig != last_sig or scroll_due:
                    next_scroll = time.monotonic() + ui.MARQUEE_STEP_S
                    if last_sig is None or fp != last_sig[0] or np.get('generation') != last_sig[2]:
                        audio = sess.player_tags(fp)       # as the host player reads it
                        player_ui.set_track_context(fp, float(np.get('duration') or 0.0))
                        with quietly():
                            theme.apply(fp)              # this window follows the host's track
                        chapters = library_entry(fp).get('chapters') or []
                        chapter_i = chapter_at(chapters, float(np.get('elapsed') or 0.0))
                        player_ui.set_chapter(chapters, chapter_i)
                    _ui_state['book'] = bool(np.get('is_book'))
                    duration = float(np.get('duration') or 0.0)
                    prog_row, ctrl_row, _lr, width, _br = draw_full_ui(
                        fp, audio, None, size, is_paused=bool(np.get('paused')))
                    shown.update(fp=fp, paused=bool(np.get('paused')))
                    last_sig = sig
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
                if audio is not None:
                    panels.LIVE.paint(sys.stdout, elapsed, player_ui.live_context(), force=last_sig is None)
                    sys.stdout.flush()

                key = get_key_non_blocking()
                if key:
                    out = _player_input(key, _Input(
                        session=remote, joined=True, elapsed=elapsed, duration=duration, rate=rate,
                        chapters=chapters, ctrl_row=ctrl_row, queue=np.get('queue') or [],
                        index=int(np.get('index') or 0), path=fp, audio=audio, has_lyrics=False))
                    if isinstance(out, dict):
                        return out
                    if out is not None:
                        last_sig = None                # redrawn next pass; the host's change arrives in a snapshot
                time.sleep(_LOOP_TICK_S)
    finally:
        _drop_backdrop()
        player_ui._ui_state['lyrics_pane'] = True
        player_ui.view_chrome(False)
        disable_mouse()                 # the app's screen keeps it on (ui.enter_alt_screen)
        sys.stdout.flush()
        remote.release_view(token)
        ui.clear_screen()


def _player_view_loop() -> dict:
    """The foreground render + input loop for the attached session."""
    # --- per-track render state (rebuilt by _prepare() on every track) ---
    track_path = ""
    audio = None
    duration = 0.0
    pre_art = None
    has_credits = False
    has_lyrics = False
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
        nonlocal track_path, audio, duration, pre_art
        nonlocal has_credits, has_lyrics
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
        has_credits = bool(audio and any(player_ui.people_of(audio)))

        # Whatever this track's words come from, say so in the same breath as
        # choosing it, so the 'm' panel then names the real source rather than a
        # guess reconstructed later from the file names.
        _cfg = load_config()
        _tl, _srcs, _est = lyric_pane.timeline_for(fp, audio, duration, setting(_cfg, 'lyrics_source'),
                                                   float(setting(_cfg, 'lyrics_show_early')))
        has_lyrics = _tl is not None
        player_ui.set_lyric_sources(fp, _srcs, _est)
        pane = lyric_pane.Pane(_tl) if _tl else None
        player_ui.set_track_context(fp, duration)
        # Say it out loud. Paced-out timing looks right for the first minute and is
        # a line adrift by the last, which is not something a still screen shows.
        if _est and any(src.endswith('.md') for src in _srcs):
            ui.show_status("No transcript, so the lyric timing is a guess", tune.TOAST_LONG_S)

        if audio and audio.getall('EQU2'):
            ui.show_status("Equaliser on", tune.TOAST_LONG_S)

    def _redraw_full() -> None:
        """Full-screen redraw for the current track + view state; sets row positions."""
        nonlocal prog_row, ctrl_row, lyric_row, current_width, art_bottom_row
        prog_row, ctrl_row, lyric_row, current_width, art_bottom_row = draw_full_ui(
            track_path, audio, pre_art, last_size, is_paused=SESSION.is_paused())

        if pane and player_ui.lyrics_laid_out():
            # A full redraw has just wiped the screen, so what the pane last
            # painted is gone. Its record is of rows it wrote, not rows that
            # survived, so it is told, and repaints in step with the rest of the
            # UI rather than a tick later.
            pane.paint(sys.stdout, SESSION.elapsed(), _pane_geometry(), force=True)
            sys.stdout.flush()
        if audio is not None:
            panels.LIVE.paint(sys.stdout, SESSION.elapsed(), player_ui.live_context(), force=True)
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
            SESSION.is_paused(),
            has_lyrics=has_lyrics, has_credits=has_credits)
        player_ui.write_controls(ctrl_row, status_ln)     # through the painter, cell by cell
        sys.stdout.flush()

    player_ui.set_resizing(False)      # in case the last visit ended mid-resize
    with raw_mode(sys.stdin):
        enable_mouse()
        _prepare()
        _redraw_full()
        last_track_sig = SESSION.generation
        last_output = ""                 # takeover's Output line, as the Info panel last drew it

        def _relaid() -> None:
            """The whole player at the current size: the frame, the controls
            and the progress, as the loop draws them (under a box over it)."""
            nonlocal last_size
            last_size = ui.get_terminal_size()
            _redraw_full()
            update_ctrl_ui()
            update_progress_ui(prog_row, SESSION.elapsed(), duration, current_width, chapters,
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

            pc.render_status_bar()

            # The output is only known once mpv has opened it, a moment into
            # the track: the Info panel (drawn with the frame) shows it then.
            if (output := SESSION.output_note()) != last_output:
                last_output = output
                _redraw_full()

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

            elapsed = SESSION.elapsed()
            if chapter_at(chapters, elapsed) != chapter_i:
                chapter_i = chapter_at(chapters, elapsed)
                player_ui.set_chapter(chapters, chapter_i)
                _redraw_full()                     # the chapter line and panel move on

            key = get_key_non_blocking()
            if key:
                t = SESSION.track()
                out = _player_input(key, _Input(
                    session=SESSION, joined=False, elapsed=elapsed, duration=duration, rate=SESSION.rate,
                    chapters=chapters, ctrl_row=ctrl_row, queue=t.queue, index=t.index,
                    path=track_path, audio=audio, has_lyrics=has_lyrics))
                if isinstance(out, dict):
                    return out
                if out == 'track':                   # a new track, or new tags: the top of the loop sees to it
                    last_track_sig = None
                    continue
                if out == 'queue':
                    t = SESSION.track()
                    player_ui.set_queue_context(t.titles, t.index, t.queue)
                    last_q_sig = (tuple(t.titles), t.index)
                    _redraw_full()
                    continue
                if out == 'redraw':
                    _redraw_full()
                elif out == 'ctrl':
                    update_ctrl_ui()
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
            if audio is not None:
                panels.LIVE.paint(sys.stdout, elapsed, player_ui.live_context())
                sys.stdout.flush()

            time.sleep(_LOOP_TICK_S)
        finally:
            _drop_backdrop()
            disable_mouse()                 # the app's screen keeps it on (ui.enter_alt_screen)
            sys.stdout.flush()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        music_player(file_path=sys.argv[1])
    else:
        sys.stdout.write("Usage: python -m backtrack.playback.player <file_path>\n")
