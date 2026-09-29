"""The Settings screen and the editors it opens: music directories, tag name preferences, the Browse menu, rescans."""
from __future__ import annotations
import os
from src.utils import prompt
from src.utils import ui_utils
from src.utils.ui_utils import Colors as C
from src.music_library import (
    build_library, save_library_cache, start_background_sync, sort_options,
)
from src.history import get_history, clear_history
from src.config import load_config, music_dirs, set_music_dirs, library_name, update_config, changed_keys
from src.id3.tag_registry import TAG_REGISTRY
from src.menus.activity import activity_centre
from src.menus.common import BROWSE_CATEGORIES, DEFAULT_BROWSE_MENU, OFF_GLYPH, ON_GLYPH, _SETTINGS_COLUMNS, _commit, _idx_of, _menu_header, _space_toggles, _state_glyph, browse_menu_keys
from src.menus.sorting import _chain_summary, _pick_chain
from src.config import setting


def _handle_tag_name_preferences() -> None:
    """Settings sub-screen: edit preferred friendly names for tags via list_edit."""
    config = load_config()
    prefs: dict = dict(setting(config, 'tag_name_preferences'))
    initial: list = [(tag_id, prefs.get(tag_id, "")) for tag_id in TAG_REGISTRY]

    def _tag_pref_hints(col: int, row: list) -> list:
        """Suggest the tag registry's default display name as a hint for the preferred-name column."""
        if col != 1:
            return []
        tag_id = row[0].strip() if row else ""
        info = TAG_REGISTRY.get(tag_id)
        return info.name if info else []

    result = prompt.list_edit(
        "Tag name preferences: TAG · PREFERRED NAME (blank name = use default):",
        initial_items=initial,
        headers=("TAG", "PREFERRED NAME"),
        col_ratios=(1, 4),
        col_hints=_tag_pref_hints,
        fixed_rows=True,
        locked_cols={0},
    )
    if result is None:
        return

    new_prefs: dict = {}
    for row in result:
        cols = list(row) if isinstance(row, (list, tuple)) else [str(row), ""]
        while len(cols) < 2:
            cols.append("")
        tag_id, name = str(cols[0]).strip(), str(cols[1]).strip()
        if tag_id and name and tag_id in TAG_REGISTRY:
            new_prefs[tag_id] = name

    changed = sum(1 for k, v in new_prefs.items() if prefs.get(k) != v) + \
              sum(1 for k in prefs if k not in new_prefs)
    config['tag_name_preferences'] = new_prefs
    _commit(config, 'tag_name_preferences')
    ui_utils.show_status(f"Tag name preferences saved ({changed} changed).")


def _rescan_library(config: dict, library_ref: list) -> int:
    """Rebuild the cache from every configured directory and restart the sync."""
    ui_utils.show_status("Re-scanning library…")
    new_lib = build_library(
        music_dirs(config),
        ignore_hidden=setting(config, "ignore_hidden_files"),
    )
    save_library_cache(new_lib, _async=False)
    existing = library_ref[0]
    existing.clear()
    existing.extend(new_lib)
    start_background_sync(existing)
    return len(new_lib)


def _music_dirs_menu(config: dict, library_ref: list) -> None:
    """Add / remove the directories the library is built from.

    Several roots are supported (a local folder plus an external drive, say);
    they may nest, and a root that is temporarily missing keeps its tracks in the
    cache rather than losing them.
    """
    _cursor = 0
    while True:
        dirs = music_dirs(config)
        _choices: list = [prompt.Choice(title="＋  Add a directory…", value="__add__")]
        for d in dirs:
            missing = "" if os.path.isdir(d) else "  (not available)"
            _choices.append(prompt.Choice(title=f"{library_name(config, d)}  {d}{missing}", value=d,
                                          cells=[library_name(config, d), f"{d}{missing}"]))
        if dirs:
            _choices.append(prompt.separator())
            _choices.append(prompt.Choice(title="Re-scan now", value="__rescan__"))

        _sub = f"{len(dirs)} director{'y' if len(dirs) == 1 else 'ies'}"
        choice = prompt.select("", choices=_choices,
                               header=_menu_header("Music Directories", _sub),
                               columns=_SETTINGS_COLUMNS, index=_cursor)
        if not choice:
            return

        _cursor = _idx_of(_choices, choice)

        if choice == "__add__":
            new_root = prompt.path("Directory to add:")
            if not new_root:
                continue
            full = os.path.abspath(os.path.expanduser(new_root))
            if not os.path.isdir(full):
                ui_utils.show_status("Not a directory.")
                continue
            if any(os.path.normcase(full) == os.path.normcase(d) for d in dirs):
                ui_utils.show_status("Already in the list.")
                continue
            set_music_dirs(config, dirs + [full])
            _commit(config, "music_directories", "music_directory")
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Added: {n} tracks.")
            continue

        if choice == "__rescan__":
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Done: {n} tracks.")
            continue

        # An existing directory row: rename it, give it its own sort order, or
        # remove it (its tracks leave the library).
        if choice in dirs:
            name = library_name(config, choice)
            own = choice in sort_options(config)['libraries']
            act = prompt.select("", choices=[
                prompt.Choice(title="Name…", value="name", cells=["Name…", name]),
                prompt.Choice(title="Sort order…", value="sort",
                              cells=["Sort order…", _chain_summary(sort_options(config)['libraries'][choice])
                                     if own else "shared"]),
                prompt.Choice(title="Remove…", value="remove", cells=["Remove…", ""]),
            ], columns=_SETTINGS_COLUMNS, header=_menu_header(name, choice))
            if act == "name":
                new = prompt.text("Name shown in Browse:", default=name)
                if new is not None:
                    names = dict(config.get("library_names") or {})
                    if new.strip() and new.strip() != os.path.basename(choice.rstrip(os.sep)):
                        names[choice] = new.strip()
                    else:
                        names.pop(choice, None)          # blank → back to the folder name
                    config["library_names"] = names
                    _commit(config, "library_names")
                continue
            if act == "sort":
                _pick_chain(config, choice, _menu_header(name, choice), allow_shared=True)
                continue
            if act != "remove":
                continue
            if len(dirs) == 1 and not prompt.confirm(
                    "That's the only directory. Remove it and empty the library?"):
                continue
            if len(dirs) > 1 and not prompt.confirm(f"Remove {os.path.basename(choice) or choice}?"):
                continue
            set_music_dirs(config, [d for d in dirs if d != choice])
            # Its name and own sort order go with it, not left behind in config.
            config["library_names"] = {k: v for k, v in (config.get("library_names") or {}).items()
                                       if k != choice}
            config["library_sort_levels"] = {k: v for k, v in
                                             (config.get("library_sort_levels") or {}).items()
                                             if k != choice}
            _commit(config, "music_directories", "music_directory",
                    "library_names", "library_sort_levels")
            n = _rescan_library(config, library_ref)
            ui_utils.show_status(f"Removed: {n} tracks.")


# Settings rows that are on/off switches: space flips them, like ↵ does.
_SETTINGS_TOGGLES = {"history", "autoplay", "meta_editor", "lyrics_editor",
                     "plain_text", "sort_tags", "hidden", "key_hints", "inline_art", "debug",
                     "player_meta"}


def _accent_swatch(value) -> list:
    """A block of the colour itself, or a hatched gap when there is none."""
    code = ui_utils.accent_code(value)
    return [(f"{code}████{C.RESET}", 'normal')] if code else [("░░░░", 'dim')]


def _accent_name(value, name: str):
    """The colour's name, written in that colour."""
    code = ui_utils.accent_code(value)
    return [(f"{code}{name}{C.RESET}", 'normal')] if code else name


_ACCENT_COLUMNS = [
    prompt.Column(style='normal'),                  # swatch
    prompt.Column(style='normal'),                  # name, in its own colour
    prompt.Column(style='dynamic-dim', flex=True),  # ● on the one in use
]


def _pick_accent(config: dict) -> None:
    """Settings → Accent colour. Every colour is shown in itself, and picking one
    applies it at once, so the screen around the list is the preview; esc when
    it looks right. The terminal-theme colours follow the terminal's palette."""
    place = prompt.ListPlace()
    current = config.get('accent_colour') or ui_utils.DEFAULT_ACCENT
    place.value = '__custom__' if current.startswith('#') else current
    while True:
        current = config.get('accent_colour') or ui_utils.DEFAULT_ACCENT
        custom = current if current.startswith('#') else None

        def _row(value, name: str, colour) -> prompt.Choice:
            in_use = value == current or (value == '__custom__' and custom)
            return prompt.Choice(title=name, value=value,
                                 cells=[_accent_swatch(colour), _accent_name(colour, name),
                                        ON_GLYPH if in_use else ""])

        choices: list = [prompt.separator("Your terminal's colours")]
        for i, (key, name, _colour) in enumerate(ui_utils.ACCENT_PRESETS):
            if i == 6:
                choices.append(prompt.separator("Fixed colours"))
            choices.append(_row(key, name, key))
        choices += [prompt.separator(), _row('__custom__', 'Custom colour…', custom)]

        choice = prompt.select("", choices=choices, columns=_ACCENT_COLUMNS, place=place,
                               header=_menu_header("Accent colour", ui_utils.accent_label(current)))
        if not choice:
            return
        if choice == '__custom__':
            typed = prompt.text("Hex colour (e.g. #4FC3F7):", default=custom or "#")
            if typed is None:
                continue
            rgb = ui_utils.parse_hex_colour(typed)
            if rgb is None:
                ui_utils.show_status("That isn't a colour. Use #RRGGBB, like #4FC3F7.")
                continue
            choice = "#%02X%02X%02X" % rgb
        config['accent_colour'] = choice
        ui_utils.set_accent(choice)
        ui_utils.show_status(f"Accent colour: {ui_utils.accent_label(choice)}")


def handle_settings(library_ref: list) -> None:
    """Run the interactive settings menu loop, applying and persisting each toggled option."""
    import copy
    _cursor = 0

    while True:
        # A fresh copy each time round, and only what this action changed is
        # saved (update_config), never the whole dict back over newer changes.
        config = load_config()
        _before = copy.deepcopy(config)

        def _bool(key: str) -> str:
            """Right-column glyph for an on/off setting."""
            return _state_glyph(setting(config, key))

        _tasks = len(ui_utils.BACKGROUND_TASKS)
        _prefs = len(config.get("tag_name_preferences") or {})
        _hist = len(get_history(limit=10 ** 9))
        _accent = (config.get("accent_colour") if ui_utils.accent_code(config.get("accent_colour"))
                   else ui_utils.DEFAULT_ACCENT)

        # (value, label, current state); separators are plain strings.
        _rows: list = [
            prompt.separator("Playback"),
            ("lead_in",      "Lyric lead-in…",       f"{float(config['lyric_lead_in']):g}s"),
            ("autoplay",     "Auto-play on select",  _bool("autoplay_on_select")),
            ("key_hints",    "Key hints",            _state_glyph(prompt.hints_visible())),
            ("inline_art",   "Image album art (iTerm2)", _bool("art_inline_images")),
            ("player_meta",  "Track details in player", _bool("player_show_metadata")),
            prompt.separator("Appearance"),
            ("accent",       "Accent colour…",
             _accent_swatch(_accent) + ["  " + ui_utils.accent_label(_accent)]),
            prompt.separator("Library"),
            ("music_dirs",   "Music directories…",   ui_utils.plural(len(music_dirs(config)), "folder")),
            ("activity",     "Activity centre…",     f"{_tasks} running" if _tasks else "idle"),
            ("hidden",       "Hidden file filter",   _bool("ignore_hidden_files")),
            ("browse_menu",  "Browse menu…",         f"{len(browse_menu_keys(config))} of {len(BROWSE_CATEGORIES)} shown"),
            prompt.separator("Sorting"),
            ("sort_order",   "Sort order…",          _chain_summary(sort_options(config)['levels'])),
            ("sort_tags",    "Use sort-order tags",  _bool("sort_use_tags")),
            ("sort_words",   "Ignored leading words…",
             ", ".join(config.get("sort_ignore_words") or []) or "none"),
            prompt.separator("Editors"),
            ("meta_editor",  "Metadata editor",      _bool("show_metadata_editor")),
            ("lyrics_editor", "Lyrics editor",       _bool("show_lyrics_editor")),
            ("plain_text",   "Plain-text editing",   _bool("plain_text_editing")),
            ("tag_names",    "Tag name preferences…", ui_utils.plural(_prefs, "override") if _prefs else "none"),
            ("delimiter",    "Sort list delimiter…", setting(config, "sort_list_delimiter")),
            prompt.separator("Diagnostics"),
            ("debug",        "Diagnostics log",      _bool("debug")),
            prompt.separator("History"),
            ("history",      "Listening history",    _bool("history_enabled")),
            ("clear_history", "Clear history log…",  ui_utils.plural(_hist, "entry", "entries")),
        ]
        _labels = {r[0]: r[1] for r in _rows if isinstance(r, tuple)}
        _choices = [r if not isinstance(r, tuple)
                    else prompt.Choice(title=r[1], value=r[0], cells=[r[1], r[2]])
                    for r in _rows]

        choice = prompt.select(
            "",
            choices=_choices,
            columns=_SETTINGS_COLUMNS,
            header=_menu_header("Settings"),
            index=_cursor,
            extra_hints={"space": "toggle"},
            **_space_toggles(_SETTINGS_TOGGLES),
        )

        if not choice:
            break
        if isinstance(choice, tuple):              # space on an on/off row
            choice = choice[1]

        # Stay on the row that was just acted on, rather than jumping to the top.
        _cursor = _idx_of(_choices, choice, _cursor)

        def _toggled(key: str, note: str = "") -> None:
            """Flip an on/off setting and report its new state the same way everywhere."""
            config[key] = not setting(config, key)
            glyph = _state_glyph(config[key])
            ui_utils.show_status(f"{_labels[choice]} {glyph}{note}")

        if choice == "activity":
            activity_centre()

        elif choice == "history":
            _toggled("history_enabled")

        elif choice == "clear_history":
            if _hist == 0:
                ui_utils.show_status("History is already empty.")
            elif prompt.confirm(f"Delete all {_hist} history entries? Cannot be undone."):
                ui_utils.show_status("History cleared." if clear_history()
                                     else "Could not clear history.")

        elif choice == "debug":
            from src.utils.log import configure, log_path
            _toggled("debug", f", writing to {log_path()}" if not config.get("debug") else "")
            configure(bool(config.get("debug")))

        elif choice == "inline_art":
            _toggled("art_inline_images")
            if (config.get("art_inline_images") and os.environ.get("TERM_PROGRAM") != "iTerm.app"
                    and os.environ.get("LC_TERMINAL") != "iTerm2"):
                ui_utils.show_status("On, but this isn't iTerm2, so the player keeps the text art.")

        elif choice == "key_hints":
            prompt.toggle_hints()          # the same switch as `i` / the corner

        elif choice == "autoplay":
            _toggled("autoplay_on_select")

        elif choice == "player_meta":
            _toggled("player_show_metadata")

        elif choice == "accent":
            _pick_accent(config)

        elif choice == "lead_in":
            val = prompt.text("Lead-in seconds:", default=str(config["lyric_lead_in"]))
            if val is not None:
                try:
                    seconds = round(max(0.0, float(val)), 2)
                    config["lyric_lead_in"] = seconds
                    ui_utils.show_status(f"Lyric lead-in set to {seconds:g}s.")
                except ValueError:
                    ui_utils.show_status("Enter a number (e.g. 2 or 1.5).")

        elif choice == "meta_editor":
            _toggled("show_metadata_editor")

        elif choice == "lyrics_editor":
            _toggled("show_lyrics_editor")

        elif choice == "plain_text":
            _toggled("plain_text_editing")

        elif choice == "tag_names":
            _handle_tag_name_preferences()
            config = load_config()  # pick up changes written by the sub-handler
            continue

        elif choice == "delimiter":
            current = setting(config, "sort_list_delimiter")
            picked = prompt.select(
                f"Delimiter for multi-artist sort values (current: {current!r}):",
                choices=["/ (slash)", "| (pipe)", "; (semicolon)", ", (comma)"],
            )
            if picked:
                delim = picked.split()[0]
                config["sort_list_delimiter"] = delim
                ui_utils.show_status(f"Sort list delimiter set to {delim!r}.")

        elif choice == "browse_menu":
            _edit_browse_menu(config)

        elif choice == "sort_order":
            _pick_chain(config, None, _menu_header("Sort order"))

        elif choice == "sort_tags":
            _toggled("sort_use_tags")

        elif choice == "sort_words":
            cur = ", ".join(config.get("sort_ignore_words") or [])
            new = prompt.text("Words ignored at the start of names (comma-separated):", default=cur)
            if new is not None:
                config["sort_ignore_words"] = [w.strip() for w in new.split(",") if w.strip()]

        elif choice == "music_dirs":
            _music_dirs_menu(config, library_ref)

        elif choice == "hidden":
            # Both states need a re-scan before the library reflects the change.
            _toggled("ignore_hidden_files", ", re-scan to apply.")

        if changed_keys(_before, config):
            update_config(changed_keys(_before, config))


def _edit_browse_menu(config: dict) -> None:
    """Settings → Browse menu: ↵ or space shows or hides a category, J/K move a
    shown one. Shown ones are listed first, in menu order; changes save as
    they're made."""
    cursor = 0
    follow = None          # the row to keep the cursor on once the list reorders

    def _move(key: str, delta: int) -> bool:
        shown = browse_menu_keys(config)
        if key not in shown:
            return False
        i = shown.index(key)
        j = i + delta
        if not 0 <= j < len(shown):
            return False
        shown[i], shown[j] = shown[j], shown[i]
        config["browse_menu"] = shown
        _commit(config, "browse_menu")
        return True

    while True:
        shown = browse_menu_keys(config)
        hidden = [k for k in BROWSE_CATEGORIES if k not in shown]
        choices: list = [prompt.Choice(title=BROWSE_CATEGORIES[k][0], value=k,
                                       cells=[BROWSE_CATEGORIES[k][0], ON_GLYPH]) for k in shown]
        if hidden:
            choices.append(prompt.separator("Hidden"))
            choices += [prompt.Choice(title=BROWSE_CATEGORIES[k][0], value=k,
                                      cells=[BROWSE_CATEGORIES[k][0], OFF_GLYPH]) for k in hidden]
        choices += [prompt.separator(), prompt.Choice(title="Reset to default", value="__reset__")]
        if follow:
            cursor, follow = _idx_of(choices, follow, cursor), None
        sel = prompt.select("", choices=choices, columns=_SETTINGS_COLUMNS,
                            header=_menu_header("Browse menu", f"{len(shown)} of {len(BROWSE_CATEGORIES)} shown"),
                            index=cursor, extra_hints={"space": "show/hide"}, on_move=_move,
                            **_space_toggles(set(BROWSE_CATEGORIES)))
        if not sel:
            return
        if sel == "__reset__":
            config["browse_menu"] = list(DEFAULT_BROWSE_MENU)
            _commit(config, "browse_menu")
            continue
        key = sel[1] if isinstance(sel, tuple) else sel
        shown = browse_menu_keys(config)             # J/K may have reordered it
        if key not in shown:
            shown.append(key)                        # shown again, at the end
        elif len(shown) > 1:
            shown.remove(key)
        else:
            ui_utils.show_status("Browse needs at least one category.")
        config["browse_menu"] = shown
        _commit(config, "browse_menu")
        follow = key
