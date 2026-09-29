"""What the bulk operations share: the step walker, the sort helpers, and the
column layouts and token list more than one of them shows."""
from __future__ import annotations
from backbone import prompt
from src.id3 import bulk_ops as bo
from src.id3 import tag_registry as _reg
from src.id3 import file_namer as fnm
from backbone import numbering


# Sentinel: this step does not apply to the answers so far: step over it,
# whichever way the walk is going.
_SKIP = object()


def _walk(steps: list) -> bool:
    """Walk a bulk operation's screens, forwards on ↵ and backwards on back.

    Each step is a callable that asks its question and returns True to advance,
    False to go back one screen, or `_SKIP` when the answers so far make it
    irrelevant (a template question after choosing regex detection). Back out of
    the first screen and the whole walk returns False: the operation is off. Any
    other back returns to the screen before, which still holds what was decided
    there: an accidental ↵ costs one keystroke, not the whole automation.

    Skipped steps are stepped over in the direction of travel, so a question that
    does not apply never traps the walk going forwards or back.
    """
    i, direction = 0, 1
    while 0 <= i < len(steps):
        result = steps[i]()
        if result is _SKIP:
            i += direction                 # keep going whichever way we were
            continue
        direction = 1 if result else -1
        i += direction
    return i >= len(steps)


_SORT_BASE = bo.sort_base()


_num_pair = bo._num_pair


_sort_value = bo._sort_value


_plan_write = bo.plan_write


# source text frame → sort frame, for the standalone "apply sort orders" op:
# every sort tag, composer included.
_SORT_SRC = [(t.field, t.source, t.frame) for t in _reg.SORT_TAGS]


_RENUMBER_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4, priority=1),  # position (index), drops first
    prompt.Column(style='primary', flex=True),                       # file (kept)
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # old → new (the change, kept)
]


# Pattern picker: pattern · example/description.
_RENAME_PICK_COLUMNS = [
    prompt.Column(style='primary'),
    prompt.Column(style='dynamic-dim', flex=True),
]


# Token reference: %token% · description.
_TOKENS_COLUMNS = [
    prompt.Column(style='primary'),
    prompt.Column(style='dynamic-dim', flex=True),
]


def _show_tokens(header) -> None:
    """Read-only reference of every %token% the pattern accepts."""
    rows: list = [prompt.Choice(title=f"%{t}%", value=t, disabled=True,
                                cells=[f"%{t}%", desc]) for t, desc in fnm.TOKENS.items()]
    # Number styles apply to any numeric token: %track:r%, %disc:en%, %track:r:l%.
    rows.append(prompt.separator())
    for style, desc in numbering.STYLES.items():
        suffix = "" if style == 'n' else f":{style}"
        rows.append(prompt.Choice(title=f"%track{suffix}%", value=f"__style_{style}",
                                  disabled=True,
                                  cells=[f"%track{suffix}%", f"{desc}, any numeric token"]))
    rows.append(prompt.Choice(title="%track:r:l%", value="__style_case", disabled=True,
                              cells=["%track:r:l%", f"case: {numbering.CASES}"]))
    rows.append(prompt.separator())
    rows.append(prompt.Choice(title="Back", value="__back__"))
    prompt.select("Available tokens:", choices=rows, columns=_TOKENS_COLUMNS,
                  header=header("token reference"))


def preview_and_apply(plan, library: list, header, writer, verb: str, *, count: str,
                      changing: str, unchanged, skipped: int = 0,
                      skipped_note: str = "unsupported skipped",
                      columns=None, positions: bool = True) -> None:
    """Preview a plan and write what is ticked: every file listed, the changing
    ones ticked, live counts in the header, then the one-line summary. Only a
    row that changes can be ticked: the rest are listed greyed out, saying
    `unchanged(c)`, so the selection is visibly complete."""
    from backbone import ui as ui_utils
    if not plan.changed:
        ui_utils.show_status("Nothing to change.")
        return
    choices = [
        prompt.Choice(title=name, value=c.path, checked=c.changed, disabled=not c.changed,
                      cells=([str(pos)] if positions else []) + [name, why or unchanged(c)])
        for (pos, name, why), c in zip(bo.position_rows(plan), plan.changes)]

    def _header():
        ticked = sum(1 for ch in choices if ch.checked)
        bits = [count, f"{len(plan.changed)} {changing}", f"{ticked} ticked"]
        if skipped:
            bits.append(f"{skipped} {skipped_note}")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview (↵ applies):", choices=choices,
                        columns=columns or _RENUMBER_COLUMNS, header=_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel) & {c.path for c in plan.changed}
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return
    applied = bo.apply_changes(plan, library, writer, selected=apply_set)
    ui_utils.show_status(bo.summarise(applied, verb, skipped_note=skipped_note))
