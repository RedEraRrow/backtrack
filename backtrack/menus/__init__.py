"""Top-level TUI menu hierarchy: browse, search, history, settings, and playback."""
from __future__ import annotations

from backbone import prompt
from backtrack.menus.common import _idx_of, _menu_header
from backtrack.menus.browse import handle_browse
from backtrack.menus.history import handle_history
from backtrack.menus.search import handle_search
from backtrack.menus.settings import handle_settings


def main_menu(library_ref: list) -> None:
    """Run the top-level main menu loop, dispatching to browse/search/history/settings."""
    _cursor = 0
    _opts = ["Browse", "Search", "Listening History", "Settings", "Exit"]
    while True:
        choice = prompt.select(
            "",
            choices=_opts,
            header=_menu_header("Music Player"),
            index=_cursor,
            allow_back=False,   # top level: no ←/b/Esc exit, only Enter or q/Exit
        )

        if not choice or choice == "Exit":
            break
        _cursor = _idx_of(_opts, choice)

        if choice == "Browse":
            handle_browse(library_ref)
        elif choice == "Search":
            handle_search(library_ref[0])
        elif choice == "Listening History":
            handle_history(library_ref[0])
        elif choice == "Settings":
            handle_settings(library_ref)
