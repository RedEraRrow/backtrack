"""The app's tabs: Now playing, Browse, Directory, Search and Settings."""
from __future__ import annotations

from backbone import nav, prompt
from backtrack.menus.browse import handle_browse
from backtrack.menus.directory import handle_directory
from backtrack.menus.search import handle_search
from backtrack.menus.settings import handle_settings


def main_menu(library_ref: list, on_start=None) -> None:
    """Run the app as tabs, starting on Browse, until q (or Settings → Exit).
    `on_start()` runs first, on Browse (the offer to resume), so what it opens
    has the tabs too."""
    from backtrack.playback.player import NOW_PLAYING_TAB
    from backtrack.menus.command_line import command_line
    prompt.set_command_line(lambda: command_line(library_ref))
    pending = [on_start]

    def browse() -> None:
        if pending[0]:
            first, pending[0] = pending[0], None
            first()
        handle_browse(library_ref)

    nav.run_tabs([
        (NOW_PLAYING_TAB, _now_playing),
        ("Browse", browse),
        ("Directory", lambda: handle_directory(library_ref)),
        ("Search", lambda: handle_search(library_ref[0])),
        ("Settings", lambda: handle_settings(library_ref)),
    ], home=1)


def _now_playing() -> None:
    """Now playing: the player (the empty one while nothing plays), until it's
    left, back to the tab before."""
    from backtrack.playback.player import now_playing_tab
    now_playing_tab()
