"""Sorting in the menus: the artist/genre name orders, the shared sort chain's
presets, the s picker, and the level editor."""
from __future__ import annotations
from backbone import prompt
from backbone import ui as ui_utils
from src.music_library import (
    get_group_sort_key, sort_options, valid_levels, SORT_FIELDS, DEFAULT_SORT_LEVELS,
)
from src.config import library_name
from src.menus.common import _SETTINGS_COLUMNS, _commit, _idx_of


# --- Browse sort options -------------------------------------------------
# Artist and genre lists order their names; everything that holds albums and
# tracks follows the shared sort chain (music_library.sort_tracks), or a
# library's own. Both are chosen with `s` and saved.
_GROUP_SORTS = [("name", "Name (A-Z)"), ("name_desc", "Name (Z-A)"), ("tracks", "Most tracks")]


_BY_TRACK = [['disc', 'asc'], ['track', 'asc'], ['title', 'asc'], ['date', 'asc']]


_CHAIN_PRESETS = [
    ("Album A-Z, then oldest", DEFAULT_SORT_LEVELS),
    ("Oldest first", [['album_year', 'asc'], ['album', 'asc']] + _BY_TRACK),
    ("Newest first", [['album_year', 'desc'], ['album', 'asc']] + _BY_TRACK),
    ("Album artist, then oldest", [['album_artist', 'asc'], ['album_year', 'asc'], ['album', 'asc']] + _BY_TRACK),
    ("Broadcast order", [['date', 'asc'], ['album', 'asc'], ['disc', 'asc'], ['track', 'asc'], ['title', 'asc']]),
]


def _sort_groups(names: list, grouped: dict, cat_key: str, mode: str) -> list:
    """Order artist/genre names by the chosen mode; ties fall back to name order."""
    def nk(n: str) -> str:
        """Sort key for group n (also used as the tiebreaker for other modes)."""
        return get_group_sort_key(n, grouped[n], cat_key)
    if mode == "name_desc":
        return sorted(names, key=nk, reverse=True)
    if mode == "tracks":
        return sorted(names, key=lambda n: (-len(grouped[n]), nk(n)))
    return sorted(names, key=nk)  # "name" (default, alphabetical)


def _pick_sort(current: str, options: list, header) -> str:
    """Show a sort picker; return the chosen mode (unchanged if cancelled)."""
    choices = [
        prompt.Choice(title=f"{'✔ ' if v == current else '  '}{lbl}", value=v)
        for v, lbl in options
    ]
    sel = prompt.select("Sort by:", choices=choices, header=header,
                        index=_idx_of(choices, current))
    return sel or current


def _direction_label(field: str, direction: str) -> str:
    """How a level's direction reads for its kind of field."""
    kind = SORT_FIELDS[field][2]
    asc = direction == 'asc'
    if kind == 'text':
        return "A-Z" if asc else "Z-A"
    if field in ('album_year', 'date'):
        return "oldest first" if asc else "newest first"
    return "lowest first" if asc else "highest first"


def _chain_summary(levels: list) -> str:
    """A chain in a few words, for a settings row: its preset's name, or
    "Custom" and how many levels."""
    for label, preset in _CHAIN_PRESETS:
        if valid_levels(preset) == levels:
            return label
    return f"Custom, {ui_utils.plural(len(levels), 'level')}"


def _save_chain(cfg: dict, owner: str | None, levels: list | None) -> None:
    """Store a chain as the shared order (owner None) or a library's; None
    clears a library's so it follows the shared order again."""
    if owner is None:
        cfg["sort_levels"] = [list(lv) for lv in (levels or [])]
    else:
        libs = dict(cfg.get("library_sort_levels") or {})
        if levels:
            libs[owner] = [list(lv) for lv in levels]
        else:
            libs.pop(owner, None)
        cfg["library_sort_levels"] = libs
    _commit(cfg, "sort_levels" if owner is None else "library_sort_levels")


def _pick_chain(cfg: dict, owner: str | None, header, *, allow_shared: bool = False) -> bool:
    """The `s` / Settings sort picker for one chain: the presets, plus Custom…
    for the level editor. `allow_shared` adds "Use the shared order" (for a
    library). Returns True when something changed."""
    opts = sort_options(cfg)
    has_own = owner is not None and owner in opts['libraries']
    current = opts['libraries'][owner] if has_own else opts['levels']
    shared = owner is not None and not has_own
    whose = "shared order" if owner is None else f"{library_name(cfg, owner)}'s order"
    choices = []
    if allow_shared:
        choices.append(prompt.Choice(title=f"{'✔ ' if shared else '  '}Use the shared order", value="__shared__"))
    matched = False
    for label, levels in _CHAIN_PRESETS:
        on = not shared and valid_levels(levels) == current
        matched = matched or on
        choices.append(prompt.Choice(title=f"{'✔ ' if on else '  '}{label}", value=label))
    choices.append(prompt.Choice(title=f"{'✔ ' if not (matched or shared) else '  '}Custom…", value="__custom__"))
    start = next((i for i, c in enumerate(choices) if c.title.startswith('✔')), 0)
    sel = prompt.select(f"Sort by ({whose}):", choices=choices, header=header, index=start)
    if not sel:
        return False
    if sel == "__shared__":
        _save_chain(cfg, owner, None)
        return True
    if sel == "__custom__":
        edited = _edit_chain(current, header)
        if edited is None:
            return False
        _save_chain(cfg, owner, edited)
        return True
    _save_chain(cfg, owner, dict(_CHAIN_PRESETS)[sel])
    return True


def _edit_chain(levels: list, header) -> list | None:
    """Edit a sort chain in place: ↵ changes a level's field, space flips its
    direction, d removes it, J/K move it. Returns the new chain on Save
    changes, or None when backed out of."""
    chain = [list(lv) for lv in levels]
    cursor = 0

    def _move(field: str, delta: int) -> bool:
        i = next(k for k, lv in enumerate(chain) if lv[0] == field)
        j = i + delta
        if not 0 <= j < len(chain):
            return False
        chain[i], chain[j] = chain[j], chain[i]
        return True

    def _row(act: str):
        """A row key's callback: (act, level) on a level row, nothing elsewhere."""
        return lambda f: (act, f) if f in SORT_FIELDS else None

    while True:
        choices: list = [prompt.Choice(title=f"{SORT_FIELDS[f][0]}, {_direction_label(f, d)}", value=f)
                         for f, d in chain]
        choices.append(prompt.separator())
        if len(chain) < len(SORT_FIELDS):
            choices.append(prompt.Choice(title="Add a level…", value="__add__"))
        choices.append(prompt.Choice(title="Reset to default", value="__reset__"))
        choices.append(prompt.Choice(title="Save changes", value="__save__"))
        sel = prompt.select("Sort levels (first wins; ties go to the next):",
                            choices=choices, header=header, index=cursor, on_move=_move,
                            row_actions={'SPACE': _row("flip"), 'd': _row("remove"),
                                         'DELETE': _row("remove"), 'BACKSPACE': _row("remove")},
                            row_action_hints={"space": "flip direction", "d": "remove"})
        if sel is None:
            return None
        if isinstance(sel, tuple):
            act, f = sel
            i = next(k for k, lv in enumerate(chain) if lv[0] == f)
            if act == "flip":
                chain[i][1] = 'desc' if chain[i][1] == 'asc' else 'asc'
                cursor = i
            else:
                del chain[i]
                cursor = max(0, min(i, len(chain) - 1))
            continue
        cursor = _idx_of(choices, sel, cursor)
        if sel == "__save__":
            return chain or [list(lv) for lv in DEFAULT_SORT_LEVELS]
        if sel == "__reset__":
            chain = [list(lv) for lv in DEFAULT_SORT_LEVELS]
            continue
        if sel == "__add__":
            f = _pick_field([x for x in SORT_FIELDS if x not in {c[0] for c in chain}], header)
            if f:
                chain.append([f, 'asc'])
                cursor = len(chain) - 1        # onto the new level, not whatever
                                               # row slid into "Add a level…"'s place
            continue
        i = next(k for k, lv in enumerate(chain) if lv[0] == sel)
        nf = _pick_field([x for x in SORT_FIELDS if x == sel or x not in {c[0] for c in chain}], header)
        if nf:
            chain[i][0] = nf


def _pick_field(fields: list, header) -> str | None:
    """Choose a field to sort a level by."""
    kinds = {'album': "per album", 'track': "per track"}
    choices = [prompt.Choice(title=SORT_FIELDS[f][0], value=f,
                             cells=[SORT_FIELDS[f][0], kinds[SORT_FIELDS[f][1]]]) for f in fields]
    return prompt.select("Sort by:", choices=choices, header=header, columns=_SETTINGS_COLUMNS)
