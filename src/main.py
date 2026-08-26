"""Application entry point: config, cache, library build, and menu launch."""
import os
import time

from src.config import load_config, save_config, music_dirs, set_music_dirs
from src.playback.session import SESSION
from src.music_library import (
    build_library, load_library_cache, save_library_cache,
    start_background_sync
)
from src.menus import main_menu
from src.id3.tag_registry import TAG_REGISTRY
from src.state import QuitToTerminal
from src.utils import prompt, ui_utils


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
    window then can't open the player until this one leaves it, and vice-versa —
    the player view is single-instance across the session. Does nothing if the
    session is idle or another window already has the view."""
    from src.playback.playback import open_client_player_view
    if not info.get("now_playing"):
        return                                   # session was idle — nothing to open
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
    res = open_client_player_view()
    if isinstance(res, dict) and res.get("status") == "QUIT_ALL":
        raise QuitToTerminal()


def _maybe_join_session() -> None:
    """If other Backtrack windows are already running, offer to join one of their
    sessions (mirror + control it) or start a fresh one (#14 Phase 2)."""
    try:
        from src.playback import ipc
        from src.playback import session as sess
    except Exception:
        return
    sessions = ipc.list_sessions()
    if not sessions:
        return
    choices = [prompt.Choice(title="Start a new session (this window plays its own audio)",
                             value="__new__")]
    for s in sessions:
        np = s.get("now_playing") or {}
        now = f" — ▶ {np.get('title')}" if np and np.get("title") else " — (idle)"
        choices.append(prompt.Choice(title=f"Join: {s.get('label', 'Session')}{now}", value=s))
    pick = prompt.select("Another Backtrack session is running:", choices=choices)
    if not isinstance(pick, dict):           # None/back or "__new__" → host a new one
        return
    from typing import cast
    info = cast(dict, pick)
    sock = info["socket"]
    sid = info.get("id", "")
    # If the host goes away, elect a new host / reconnect (#14 Phase 2d). Each
    # mirrored snapshot repaints this window's now-playing box immediately, so a
    # joined window stays live without needing a keystroke (#14).
    link = ipc.SessionClient(
        sock,
        on_snapshot=lambda _snap: ui_utils.pulse_now_playing(),
        on_disconnect=lambda: sess.attempt_handoff(sid, sock),
    )
    if link.connect():
        sess.set_client_link(link)
        ui_utils.show_status(f"Joined session: {info.get('label', '')}")
        # Take the player view for this window if it's free (see docstring).
        _take_player_if_free(link, info)
    else:
        ui_utils.show_status("Could not join that session — starting a new one.")


def _run(config: dict) -> None:
    """Load or build the library and hand off to the main menu; first run prompts
    for a music directory and builds the cache from scratch."""
    _maybe_join_session()
    # The player owns the volume: bind the live dict so the level it restores (and
    # any change made while playing) is what a later save writes.
    SESSION.bind_config(config)
    config = _init_tag_preferences(config)
    # Persist immediately so first-run tag preferences survive an instant quit;
    # the settings menu also autosaves, so "save & quit" (q) needs nothing more.
    save_config(config)

    library = load_library_cache()

    if library:
        ui_utils.show_status(f"Library: {len(library)} tracks.")
        # Keep the cache fresh in the background (adds/removes/edits).
        start_background_sync(library)

        library_ref = [library]
        main_menu(library_ref)
        save_config(config)
        return

    # First run: prompt for music directory
    ui_utils.clear_screen()
    roots = music_dirs(config)
    if not roots:
        picked = prompt.path("Select your Music Directory:")
        roots = [os.path.abspath(os.path.expanduser(picked))] if picked else []

    roots = [r for r in roots if os.path.isdir(r)]
    if not roots:
        ui_utils.show_loading("No valid directory selected.")
        time.sleep(1.5)
        return

    # More can be added later in Settings → Music Directories.
    set_music_dirs(config, roots)
    save_config(config)

    ui_utils.show_loading("Building library…")
    library = build_library(
        roots,
        ignore_hidden=config.get("ignore_hidden_files", False)
    )

    save_library_cache(library, _async=False)
    ui_utils.show_status(f"Library built: {len(library)} tracks.")

    start_background_sync(library)

    main_menu([library])


def _wire_playback() -> None:
    """Register the now-playing bar provider, the Ctrl-O player opener, and the
    Ctrl-P/N/B transport hotkeys (#14), so menus/browse can show background audio
    and control (or reopen) the player from anywhere."""
    from src.playback import playback_ui
    from src.playback.playback import open_player_view

    ui_utils.set_now_playing_provider(playback_ui.format_now_playing_bar)

    def _open_player() -> None:
        from src.playback import session as sess
        from src.playback.playback import open_client_player_view
        snap = sess.current_now_playing()
        if not snap:
            ui_utils.show_status("Nothing is playing.")
            return
        holder = snap.get('view_holder')
        if holder and holder != sess.my_token():
            ui_utils.show_status("The player is open in another window.")
            return
        res = open_client_player_view() if sess.is_client() else open_player_view()
        if isinstance(res, dict) and res.get('status') == 'QUIT_ALL':
            raise QuitToTerminal()

    prompt.set_player_opener(_open_player)

    def _transport(action: str) -> None:
        """Route a global transport hotkey to the active session (local host or
        joined client), then repaint the now-playing box immediately."""
        from src.playback import session as sess
        a = sess.active_session()
        if action == 'playpause':
            a.pause_toggle()
        elif action == 'next':
            a.next()
        elif action == 'prev':
            a.prev()
        ui_utils.pulse_now_playing()

    prompt.set_transport_handler(_transport)

    # Clicking the status-bar activity beacon opens the live activity centre.
    from src.menus import notification_centre
    prompt.set_notification_opener(notification_centre)


def main() -> None:
    """Program entry point: set up the terminal, load config, and run the app."""
    # Enable ANSI escape processing on Windows consoles (no-op elsewhere).
    try:
        import colorama
        colorama.just_fix_windows_console()
    except Exception:
        pass

    config = load_config()
    _wire_playback()
    ui_utils.enter_alt_screen()
    try:
        _run(config)
    except QuitToTerminal:
        pass  # Shift-Q from anywhere in the menus — unwind straight to the shell.
    finally:
        # Stop any background audio / restore stderr, and drop any joined-session
        # client link (#14).
        try:
            from src.playback import session as sess
            if sess.is_client():
                sess._client_link.close()  # type: ignore[union-attr]
                sess.set_client_link(None)
            sess.SESSION.shutdown()
        except Exception:
            pass
        ui_utils.exit_alt_screen()


if __name__ == "__main__":
    main()
