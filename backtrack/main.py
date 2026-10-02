"""Application entry point: config, cache, library build, and menu launch."""
import os
import time

from backtrack.config import load_config, music_dirs, set_music_dirs, setting, update_config
from backbone.log import log, configure as log_setup, quietly
from backtrack.playback.session import SESSION
from backtrack.music_library import (
    build_library, load_library_cache, save_library_cache,
    start_background_sync
)
from backtrack.menus import main_menu
from backtrack.id3.tag_registry import TAG_REGISTRY
from backbone.nav import QuitToTerminal
from backbone import prompt, ui


def _init_tag_preferences(config: dict) -> dict:
    """Seed tag_name_preferences from the tag registry's default names on first run."""
    if not config.get('tag_name_preferences'):
        config['tag_name_preferences'] = {
            tag_id: info.name[0]
            for tag_id, info in TAG_REGISTRY.items()
        }
    return config


def _take_player_if_free(link, info: dict) -> None:
    """On joining, if the session is playing and no window currently holds the
    player view, open it here and lock it to this window (#14). The existing
    window then can't open the player until this one leaves it, and vice versa:
    the player view is single-instance across the session. Does nothing if the
    session is idle or another window already has the view."""
    from backtrack.playback.player import open_client_player_view
    if not info.get("now_playing"):
        return                                   # session was idle, nothing to open
    # Session was playing when listed: get the freshest view_holder before
    # deciding (wait briefly for the first mirrored push, else fall back).
    snap = None
    for _ in range(20):                          # up to ~0.5 s for the first push
        snap = link.latest()
        if snap is not None:
            break
        time.sleep(0.025)
    snap = snap or info.get("now_playing")
    if not snap or snap.get("view_holder"):
        return                                   # nothing playing, or already taken
    open_client_player_view()


def _maybe_join_session() -> bool:
    """If other Backtrack windows are already running, offer to join one of their
    sessions (mirror + control it) or start a fresh one (#14). Returns whether
    any were running."""
    try:
        from backtrack.playback import ipc
        from backtrack.playback import session as sess
    except Exception:
        return False
    sessions = ipc.list_sessions()
    if not sessions:
        return False
    choices = [prompt.Choice(title="Start a new session (this window plays its own audio)",
                             value="__new__")]
    for s in sessions:
        np = s.get("now_playing") or {}
        now = f", ▶ {np.get('title')}" if np and np.get("title") else " (idle)"
        choices.append(prompt.Choice(title=f"Join: {s.get('label', 'Session')}{now}", value=s))
    pick = prompt.select("Another Backtrack session is running:", choices=choices)
    if not isinstance(pick, dict):           # None/back or "__new__" → host a new one
        return True
    from typing import cast
    info = cast(dict, pick)
    sock = info["socket"]
    sid = info.get("id", "")
    # If the host goes away, the link elects a new host or reconnects (#14). Each
    # mirrored snapshot repaints this window's now-playing box immediately, so a
    # joined window stays live without needing a keystroke (#14).
    link = sess.client_link(sock, sid)
    if link.connect():
        sess.set_client_link(link)
        ui.show_status(f"Joined session: {info.get('label', '')}")
        # Take the player view for this window if it's free (see docstring).
        _take_player_if_free(link, info)
    else:
        ui.show_status("Could not join that session, starting a new one.")
    return True


def _maybe_resume() -> None:
    """If the last run left a queue, offer to pick it up where it stopped or
    start fresh, as joining a session is offered. Esc decides later: the queue
    is kept and offered again next time."""
    from backtrack.playback.session import forget_saved_queue, saved_queue
    from backtrack.menus.play import resume_queue
    saved = saved_queue()
    if not saved:
        return
    i, n = saved['index'], len(saved['queue'])
    at = f" at {ui.format_time(int(saved['elapsed']))}" if saved['elapsed'] >= 1 else ""
    pick = prompt.select("You were listening to:", choices=[
        prompt.Choice(title=f"Resume  {saved['titles'][i]}{at} · {i + 1} of {n}", value="resume"),
        prompt.Choice(title="Start fresh (forget it)", value="fresh"),
    ])
    if pick == "resume":
        resume_queue()
    elif pick == "fresh":
        forget_saved_queue()


def _run(config: dict) -> None:
    """Load or build the library and hand off to the main menu; first run prompts
    for a music directory and builds the cache from scratch."""
    others = _maybe_join_session()
    # The player owns the volume: bind the live dict so the level it restores (and
    # any change made while playing) is what a later save writes.
    SESSION.bind_config(config)
    config = _init_tag_preferences(config)
    # Persist immediately so first-run tag preferences survive an instant quit;
    # the settings menu autosaves, so quitting needs nothing more.
    update_config({'tag_name_preferences': config['tag_name_preferences']})

    library = load_library_cache()

    if library:
        ui.show_status(f"Library: {ui.plural(len(library), 'track')}.")
        # Keep the cache fresh in the background (adds/removes/edits).
        start_background_sync(library)
        # A running session's queue is its own, live: offered as a join above.
        if not others:
            _maybe_resume()

        library_ref = [library]
        # No save on the way out: everything that changes a setting saves it
        # as it goes, and this dict is the one loaded at startup, and saving it here
        # put back every setting as it was when the app opened.
        main_menu(library_ref)
        return

    # First run: prompt for music directory
    ui.clear_screen()
    roots = music_dirs(config)
    if not roots:
        picked = prompt.path("Select your Music Directory:")
        roots = [os.path.abspath(os.path.expanduser(picked))] if picked else []

    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        ui.show_loading("No valid directory selected.")
        time.sleep(1.5)
        return

    # More can be added later in Settings → Music Directories.
    set_music_dirs(config, roots)
    update_config({k: config[k] for k in ('music_directories', 'music_directory')})

    ui.show_loading("Building library…")
    library = build_library(
        roots,
        ignore_hidden=setting(config, "ignore_hidden_files")
    )

    save_library_cache(library, _async=False)
    ui.show_status(f"Library built: {ui.plural(len(library), 'track')}.")

    start_background_sync(library)

    main_menu([library])


def _wire_keyboard() -> None:
    """Teach the search matcher which keyboard is in front of the user, so a typo
    that lands on a neighbouring key ranks above one that needs an unrelated
    letter. Best-effort: unknown or unreadable settings leave it on QWERTY, and
    BACKTRACK_KEYBOARD overrides both (the only thing that works over SSH)."""
    from backbone import keyboard
    from backtrack import search
    search.use_layout(keyboard.rows())


def _wire_playback() -> None:
    """Register the now-playing bar provider, the Ctrl-O player opener, and the
    Ctrl-P/N/B transport hotkeys (#14), so menus/browse can show background audio
    and control (or reopen) the player from anywhere."""
    from backtrack.playback import now_playing_box
    from backtrack.playback.player import open_player_view

    ui.set_footer_provider(now_playing_box.format_now_playing_bar)

    def _open_player() -> None:
        from backtrack.playback import session as sess
        from backtrack.playback.player import open_client_player_view
        snap = sess.current_now_playing()
        if not snap:
            ui.show_status("Nothing is playing.")
            return
        holder = snap.get('view_holder')
        if holder and holder != sess.my_token():
            ui.show_status("The player is open in another window.")
            return
        if sess.is_client():
            open_client_player_view()
        else:
            open_player_view()

    prompt.set_player_opener(_open_player)

    def _transport(action: str) -> None:
        """Route a global transport hotkey to the active session (local host or
        joined client), then repaint the now-playing box immediately."""
        from backtrack.playback import session as sess
        a = sess.active_session()
        if action == 'playpause':
            a.pause_toggle()
        elif action == 'next':
            a.next()
        elif action == 'prev':
            a.prev()
        ui.pulse_footer()

    prompt.set_transport_handler(_transport)

    # Clicking the status-bar activity beacon opens the live activity centre.
    from backtrack.menus.activity import activity_centre
    prompt.set_activity_opener(activity_centre)


def main() -> int | None:
    """Program entry point.

    Bare `backtrack` opens the app, which is what it has always done and what
    the console-script exists for. Any argument means the command-line
    interface instead, and its exit code becomes the process's.
    """
    import sys
    if len(sys.argv) > 1:
        from backtrack import cli
        return cli.main(sys.argv[1:])
    return _run_app()


def _run_app() -> None:
    """Set up the terminal, load config, and run the interactive app."""
    # What it can't run without (VLC), said plainly before the UI starts.
    from backbone import deps
    from backtrack.deps import DEPS
    deps.require(DEPS, "backtrack")
    # Enable ANSI escape processing on Windows consoles (no-op elsewhere).
    with quietly():
        import colorama
        colorama.just_fix_windows_console()

    config = load_config()
    log_setup(bool(setting(config, "debug")))
    ui.set_accent(config.get("accent_colour"))
    _wire_keyboard()
    _wire_playback()
    ui.enter_alt_screen()
    try:
        _run(config)
    except QuitToTerminal:
        pass  # q from anywhere: unwind straight to the shell.
    except Exception:
        log.exception("crashed")
        raise
    finally:
        # Stop any background audio / restore stderr, and drop any joined-session
        # client link (#14).
        with quietly():
            from backtrack.playback import session as sess
            if sess.is_client():
                sess._client_link.close()  # type: ignore[union-attr]
                sess.set_client_link(None)
            sess.SESSION.shutdown()
        ui.exit_alt_screen()


if __name__ == "__main__":
    raise SystemExit(main())
