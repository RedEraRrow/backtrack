"""Bulk ID3 tag operations across multiple files."""
from __future__ import annotations
import os
import re
import mutagen.id3
from mutagen.id3 import ID3
from src.utils import prompt
from src.id3.id3_tag_handler import (
    collect_tag_data,
    prompt_for_value,
    apply_bulk_edit,
    get_tag_info,
    get_tag_category,
    display_tag_id,
    summarize_tag_value,
    create_apic_frame,
    create_frame,
    rename_frame,
    save_id3,
    apply_bulk_operation_to_files,
    _prompt_for_image_metadata,
    _prompt_for_picture_type,
    _PICTURE_TYPES,
    pick_nearby_cover,
    CLEAR_COVER,
)
from src.id3.tag_registry import parse_composite_tag_id
from src.id3 import filename_parser as fp
from src.id3 import tag_writer as tw
from src.id3 import file_namer as fnm
from src.id3 import cover_matcher as cm
from src import bulk_pattern as bp
from src.music_library import format_value_list
from src.utils import numbering
from src.utils import ui_utils
from src.utils.ui_utils import get_terminal_width, Colors as C
from src.music_library import refresh_library_entry

from collections import Counter
import textwrap

from mutagen.id3._frames import APIC

# Structured columns for the bulk tag picker. Column 1 holds the tag id AND the
# friendly name as two styled segments (TAG bright + friendly dim) in one column.
_BULK_COLUMNS = [
    prompt.Column(style='primary'),                                     # TAG (friendly)
    prompt.Column(style='dynamic-dim', priority=1),                     # type / category — drops first
    prompt.Column(style='normal', flex=True),                           # value summary (kept)
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=2),  # count / total — drops next
]


# Field → short label shown in the preview / detail view.
_DERIVE_LABELS = {'title': 'title', 'artist': 'artist', 'album_artist': 'albumartist',
                  'album': 'album', 'track': 'trk', 'disc': 'disc',
                  'disc_subtitle': 'discsub', 'year': 'year'}

# Per-field column spec for the preview (only *varying* fields get a column;
# `title` flexes to absorb leftover width, the rest are bounded and truncate).
_DERIVE_FIELD_COL = {
    'title':        {'style': 'primary', 'flex': True},
    'artist':       {'style': 'dynamic-dim', 'max_frac': 0.30},
    'album':        {'style': 'dynamic-dim', 'max_frac': 0.30},
    'album_artist': {'style': 'dynamic-dim', 'max_frac': 0.30},
    'disc_subtitle': {'style': 'dynamic-dim', 'max_frac': 0.25},
    # Short numeric fields pin to the right edge so they stay visible when the
    # row is squeezed (the flexible title column absorbs the truncation instead).
    'track':        {'style': 'dynamic-dim', 'align': 'right', 'pin': True, 'max_width': 8, 'gap': 2},
    'disc':         {'style': 'dynamic-dim', 'align': 'right', 'pin': True, 'max_width': 8, 'gap': 2},
    'year':         {'style': 'dynamic-dim', 'align': 'right', 'pin': True, 'max_width': 11, 'gap': 2},
}

# Detail view (per file): field · full value · action.
_DETAIL_COLUMNS = [
    prompt.Column(style='primary'),                              # field label
    prompt.Column(style='normal', flex=True),                   # full value
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # write / kept
]


def _detail_view(path, derived, plan: dict, present: dict, apply_fields: set,
                 overwrite: bool, header) -> None:
    """Full, untruncated breakdown of one file's derivation (write vs kept)."""
    d = derived.as_dict()
    supported = tw.writable_fields(path)
    rows: list = []
    for f in tw.FIELDS:
        if f not in apply_fields:
            continue
        val = d.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if f == 'track':
            val = _num_pair(d['track'], d.get('total_tracks'))
        elif f == 'disc':
            val = _num_pair(d['disc'], d.get('total_discs'))
        if f not in supported:
            action = 'n/a (MP4)'
        elif f in plan:
            action = 'write'
        else:
            action = 'kept' if present.get(f) else '—'
        rows.append(prompt.Choice(title=_DERIVE_LABELS[f], value=f, disabled=True,
                                  cells=[_DERIVE_LABELS[f], str(val), action]))
    if derived.compilation and 'album_artist' in apply_fields:
        act = 'write' if not present.get('compilation') or overwrite else 'kept'
        rows.append(prompt.Choice(title='compilation', value='__c__', disabled=True,
                                  cells=['compilation', 'Various Artists flag', act]))
    # Sort-order tags: only written for a base field that itself writes.
    if 'sort' in apply_fields:
        for base, base_id in _SORT_BASE:
            if base not in apply_fields:
                continue
            sv = _sort_value(base_id, str(d.get(base) or ''))
            if not sv:
                continue
            act = 'write' if base in plan else '(with ' + base + ')'
            rows.append(prompt.Choice(title=f'{base} sort', value=f'{base}_sort',
                                      disabled=True,
                                      cells=[f'{_DERIVE_LABELS[base]} sort', sv, act]))
    rows.append(prompt.separator())
    rows.append(prompt.Choice(title='Back', value='__back__'))
    prompt.select(os.path.basename(path), choices=rows, columns=_DETAIL_COLUMNS,
                  header=header('Full derivation'))


# Sentinel: this step does not apply to the answers so far — step over it,
# whichever way the walk is going.
_SKIP = object()


def _walk(steps: list) -> bool:
    """Walk a bulk operation's screens, forwards on ↵ and backwards on back.

    Each step is a callable that asks its question and returns True to advance,
    False to go back one screen, or `_SKIP` when the answers so far make it
    irrelevant (a template question after choosing regex detection). Back out of
    the first screen and the whole walk returns False: the operation is off. Any
    other back returns to the screen before, which still holds what was decided
    there — an accidental ↵ costs one keystroke, not the whole automation.

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


def _num_pair(num, total) -> str:
    """Format a "num/total" string, or just "num" when total is falsy."""
    return f"{num}/{total}" if total else f"{num}"


# base field → sort frame id, used to compute sort-order strings via the #42 engine.
_SORT_BASE = [('artist', 'TSOP'), ('album_artist', 'TSO2'),
              ('album', 'TSOA')]          # no title sort — a title sorts on itself


def _sort_value(base_id: str, raw: str) -> str | None:
    """Top smart sort-order candidate for a value, or None when none is needed.

    Reuses the #42 engine (name-inversion for artists, article-move for
    album/title). "Various Artists" sorts as itself, so no tag is generated."""
    from src.id3.id3_tag_handler import is_placeholder_name
    if not raw or is_placeholder_name(raw):
        return None                     # derived names sort as themselves
    from src.id3.id3_browser import _sort_candidates
    cands = _sort_candidates(base_id, str(raw))
    return cands[0] if cands else None


def _augment_sort(vals: dict, apply_fields: set) -> dict:
    """Add '<base>_sort' entries for the derived fields being written."""
    for base, base_id in _SORT_BASE:
        if base in apply_fields and vals.get(base):
            sv = _sort_value(base_id, str(vals[base]))
            if sv:
                vals[f'{base}_sort'] = sv
    return vals


def _plan_write(derived, apply_fields: set, overwrite: bool,
                present: dict, path: str) -> dict:
    """Fields that would actually be written for one file: {field: value_str}.

    Honours fill-blanks (skip fields already present unless overwrite), only
    includes fields with a derived value, and — crucially — only fields the
    file's *format* can store (so the preview never claims a write it can't
    perform, e.g. disc subtitle on MP4). Pure — used for the preview and tests.
    """
    supported = tw.writable_fields(path)
    d = derived.as_dict()
    planned: dict = {}
    for f in tw.FIELDS:
        if f not in apply_fields or f not in supported:
            continue
        val = d.get(f)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if present.get(f) and not overwrite:
            continue
        if f == 'track':
            planned[f] = _num_pair(d['track'], d.get('total_tracks'))
        elif f == 'disc':
            planned[f] = _num_pair(d['disc'], d.get('total_discs'))
        else:
            planned[f] = str(val)
    return planned


# Columns for the bulk people editor: role · name · coverage/state.
_PEOPLE_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.45),           # role / character (kept)
    prompt.Column(style='normal', flex=True),                           # name / actor (kept)
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=1),  # N/total or state — drops first
]


def _people_apply(current: list, edits: dict, deletes: set, adds: list) -> list:
    """One file's new people list: apply edits (replace in place), deletes, and
    adds (appended if absent), preserving order and de-duplicating. Pure."""
    out: list = []
    for e in current:
        if e in deletes:
            continue
        out.append(edits.get(e, e))
    for a in adds:
        if a not in out:
            out.append(a)
    seen: set = set()
    return [e for e in out if not (e in seen or seen.add(e))]


def _read_people(path: str, tag_id: str) -> list | None:
    """The (role, name) pairs for a people tag on one MP3, or None if unreadable."""
    try:
        audio = ID3(path)
    except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
        return []
    except (OSError, IOError):
        return None
    fr = audio.get(tag_id)
    if fr is None or not hasattr(fr, 'people'):
        return []
    return [(str(r).strip(), str(n).strip()) for r, n in fr.people]


def bulk_people_editor(paths: list, tag_id: str, library: list, header) -> None:
    """Edit a people list (TMCL/TIPL) across many files by *entry*.

    Aggregates the tag's distinct ``role → name`` entries across the selected
    MP3s with an ``N/total`` coverage count. Editing or removing an entry acts on
    every file that has that exact pair; adding puts it in every file that lacks
    it. Per-file ordering is preserved (edits replace in place)."""
    info = get_tag_info(tag_id)
    label = info.name[0] if info else tag_id

    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    per_file: dict = {}
    for p in mp3s:
        pl = _read_people(p, tag_id)
        if pl is not None:
            per_file[p] = pl
    if not per_file:
        ui_utils.show_status(f"No writable MP3s for {label}.")
        return
    total = len(per_file)

    # Distinct entries in first-seen order, with coverage counts.
    counts: dict = {}
    order: list = []
    for pl in per_file.values():
        for e in pl:
            if e not in counts:
                counts[e] = 0
                order.append(e)
            counts[e] += 1
    rows = [{'orig': e, 'role': e[0], 'name': e[1], 'deleted': False} for e in order]

    while True:
        choices: list = []
        for i, r in enumerate(rows):
            if r['deleted']:
                state = 'removing'
            elif r['orig'] is None:
                state = 'add all'
            else:
                state = f"{counts[r['orig']]}/{total}"
            choices.append(prompt.Choice(title=f"{r['role']} → {r['name']}", value=i,
                                         cells=[r['role'] or '—', r['name'] or '—', state]))
        choices.append(prompt.separator())
        choices.append(prompt.Choice(title="＋  Add person to all files…", value="__add__"))
        choices.append(prompt.Choice(title="✔ Save changes", value="__save__"))

        sub = f"{total} file(s) · Enter a row to edit/remove"
        sel = prompt.select(f"Bulk edit {label}:", choices=choices,
                            columns=_PEOPLE_COLUMNS, header=header(sub),
                            shortcuts={'a': '__add__'}, extra_hints={'a': 'add'})
        if sel is None:
            return                                          # cancel — no writes
        if sel == '__add__':
            role = prompt.text(f"{label} — role / character:")
            if role is None:
                continue
            name = prompt.text(f"{label} — name / person:")
            if name is None:
                continue
            if role.strip() or name.strip():
                rows.append({'orig': None, 'role': role.strip(), 'name': name.strip(),
                             'deleted': False})
            continue
        if sel == '__save__':
            break
        # A row was chosen (its value is the int index) → edit / remove submenu.
        r = rows[int(sel)]
        act = prompt.select(f"{r['role']} → {r['name']}:",
                            choices=["Edit", ("Keep" if r['deleted'] else "Remove"), "Cancel"])
        if act == "Edit":
            nrole = prompt.text("Role / character:", default=r['role'])
            if nrole is None:
                continue
            nname = prompt.text("Name / person:", default=r['name'])
            if nname is None:
                continue
            r['role'], r['name'] = nrole.strip(), nname.strip()
        elif act in ("Remove", "Keep"):
            r['deleted'] = not r['deleted']

    # --- Build the change set, then apply per file (order preserved) ---
    edits: dict = {}      # orig pair → new pair
    deletes: set = set()
    adds: list = []
    for r in rows:
        new = (r['role'], r['name'])
        if r['orig'] is None:
            if not r['deleted'] and (r['role'] or r['name']):
                adds.append(new)
        elif r['deleted']:
            deletes.add(r['orig'])
        elif new != r['orig']:
            edits[r['orig']] = new

    if not (edits or deletes or adds):
        ui_utils.show_status("No changes.")
        return

    changed = 0
    for p, current in per_file.items():
        deduped = _people_apply(current, edits, deletes, adds)
        if deduped == current:
            continue
        try:
            audio = ID3(p)
        except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
            audio = ID3()
        except (OSError, IOError):
            continue
        audio.delall(tag_id)
        frame = create_frame(tag_id, deduped) if deduped else None
        if frame is not None:
            audio.add(frame)
        try:
            save_id3(audio, p)
            changed += 1
            refresh_library_entry(library, p)
        except Exception:
            pass

    ui_utils.show_status(f"Updated {label} in {changed} file(s).")


def bulk_fraction_editor(paths: list, tag_id: str, library: list, header) -> None:
    """Edit a fraction tag (``n/N`` — TRCK / TPOS / MVIN) across many files.

    Whichever half the selection already agrees on is seeded and editable; the
    half that differs between files is shown as a dim ``──  (varies)`` and, left
    alone, keeps each file's own value.  So you can set one common disc total
    across tracks whose disc *numbers* differ — or renumber the discs while
    leaving mixed totals intact — without the editor flattening the other half to
    whatever the first file happened to hold.
    """
    info = get_tag_info(tag_id)
    label = info.name[0] if info else tag_id
    base_id, _, _ = parse_composite_tag_id(tag_id)

    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    skipped_fmt = len(paths) - len(mp3s)
    existing: dict = {}
    for p in mp3s:
        try:
            audio = ID3(p)
        except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
            continue
        fr = audio.get(tag_id)
        raw = str(fr.text[0]) if (fr is not None and getattr(fr, 'text', None)) else ''
        cur, _, tot = raw.partition('/')
        existing[p] = (cur.strip(), tot.strip())
    if not existing:
        ui_utils.show_status(f"No MP3s carry {label}.")
        return

    currents = {c for c, _ in existing.values()}
    totals = {t for _, t in existing.values()}
    varies = set()
    if len(currents) > 1:
        varies.add('current')
    if len(totals) > 1:
        varies.add('total')

    seed_cur = next(iter(currents)) if len(currents) == 1 else ''
    seed_tot = next(iter(totals)) if len(totals) == 1 else ''
    seed = f"{seed_cur}/{seed_tot}" if seed_tot else seed_cur
    note = {frozenset(): 'all files agree',
            frozenset({'current'}): 'the numbers differ, the total is shared',
            frozenset({'total'}): 'the totals differ, the number is shared',
            frozenset({'current', 'total'}): 'numbers and totals both differ',
            }[frozenset(varies)]
    res = prompt.fraction_edit(f"{label} across {len(existing)} file(s) — {note}:",
                               tag=base_id, value=seed, varies=varies)
    if res is None or res is prompt.MODE_TOGGLE:
        return
    new_cur, new_tot = res.get('current'), res.get('total')

    plan: dict = {}
    for p, (cur, tot) in existing.items():
        # None means "this half varied and was left alone" — keep the file's own.
        c = cur if new_cur is None else new_cur.strip()
        t = tot if new_tot is None else new_tot.strip()
        if not c:
            continue                       # nothing to write without an index
        plan[p] = f"{c}/{t}" if t else c

    ordered = [p for p in mp3s if p in plan]
    pos = {p: i + 1 for i, p in enumerate(ordered)}
    choices, n_changed = [], 0
    for p in ordered:
        cur, tot = existing[p]
        old = f"{cur}/{tot}" if tot else (cur or '—')
        changed = old != plan[p]
        n_changed += changed
        choices.append(prompt.Choice(
            title=os.path.basename(p), value=p, checked=changed,
            cells=[str(pos[p]), os.path.basename(p),
                   f"{old} → {plan[p]}" if changed else 'no change']))
    if not choices:
        ui_utils.show_status("Nothing to write.")
        return

    def _frac_header():
        """Live counts for the fraction preview."""
        nk = sum(1 for ch in choices if ch.checked)
        bits = [f"{label}", ui_utils.plural(len(choices), "file"), f"{n_changed} changing", f"{nk} ticked"]
        if skipped_fmt:
            bits.append(f"{skipped_fmt} non-MP3 skipped")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview — ↵ applies:", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=_frac_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    count = errors = 0
    for p in ordered:
        if p not in apply_set:
            continue
        try:
            try:
                audio = ID3(p)
            except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
                audio = ID3()
            frame = create_frame(tag_id, plan[p])
            if frame is None:
                continue
            audio.delall(tag_id)
            audio.add(frame)
            save_id3(audio, p)
            count += 1
            try:
                refresh_library_entry(library, p)
            except Exception:
                pass
        except Exception:
            errors += 1

    msg = f"Set {label} on {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} non-MP3 skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def _derive_regex_base(paths: list) -> str:
    """Base directory for folder-path regex matching: the configured library
    root when every file is under it, otherwise the files' common ancestor."""
    abspaths = [os.path.abspath(p) for p in paths]
    try:
        from src.config import music_dirs
        roots = music_dirs()
    except Exception:
        roots = []
    # With several library roots, the deepest one containing every selected file
    # wins — that's the folder path the user thinks in.
    for music_dir in sorted(roots, key=len, reverse=True):
        if all(p.startswith(music_dir + os.sep) for p in abspaths):
            return music_dir
    try:
        return os.path.commonpath(abspaths)
    except ValueError:
        return ''


def _compute_set_value(spec: dict, frame, path: str):
    """Per-file value for a regex-driven "Set Common Value", or None to skip.

    'replace' mode runs re.sub over each existing text value of the frame (so
    multi-value frames transform value-by-value). 'filename' mode matches the
    file name (or library-relative folder path) and expands the template with
    the captured groups (``\\1`` / ``\\g<name>``). Returns a list[str] (replace)
    or str (filename); None means leave the frame untouched."""
    if spec['mode'] == 'replace':
        if frame is None or not hasattr(frame, 'text'):
            return None
        try:
            out = [spec['rx'].sub(spec['repl'], str(t)) for t in frame.text]
        except re.error:
            return None
        out = [s for s in (v.strip() for v in out) if s]
        return out or None
    # 'filename' mode
    m = spec['rx'].search(fp._regex_target(path, spec.get('base')))
    if not m:
        return None
    try:
        val = m.expand(spec['tmpl']).strip()
    except (re.error, IndexError):
        return None
    return val or None


def derive_from_filename(paths: list, library: list, header) -> None:
    """Bulk-derive title/track/disc/album/artist from file names & folders.

    Title is pre-selected (the blank-file baseline); the rest are opt-in. Shows a
    preview the user confirms (and can deselect files from) before writing.
    Writes MP3 (fresh header if blank) and MP4 natively; other formats are skipped.
    """
    writable = [p for p in paths if tw.is_writable(p)]
    skipped_fmt = len(paths) - len(writable)
    if not writable:
        ui_utils.show_status("No MP3/MP4 files here to derive from.")
        return

    # 1) Which fields to write (Title on by default — the automatic baseline).
    _FIELDS = [("title", "Title  — from file name"),
               ("track", "Track number (+ total)"),
               ("disc", "Disc number (+ total)"),
               ("disc_subtitle", "Disc subtitle — from folder (MP3 only)"),
               ("album", "Album — from folder"),
               ("album_artist", "Album artist — from parent folder"),
               ("artist", "Track artist — from file name / folder"),
               ("year", "Year / date — from folder or file name"),
               ("sort", "Sort-order tags — for the fields ticked above")]
    _MODES = ["Fill blanks only", "Overwrite existing"]
    _DETECTS = ["Auto-detect", "Use a naming template", "Use a regex"]
    _TARGETS = ["File name", "Folder path"]

    # Every answer lives here so a screen reopened by going back shows what it
    # was left holding.
    state: dict = {'fields': {'title'}, 'mode': _MODES[0], 'detect': _DETECTS[0],
                   'target': _TARGETS[0], 'template': '', 'regex': '', 'apply_paths': None}

    def _ask_fields() -> bool:
        """Which tags to derive."""
        picked = prompt.select(
            "Fields to derive & write:", multi=True, header=header(),
            choices=[prompt.Choice(title=label, value=f, checked=f in state['fields'])
                     for f, label in _FIELDS])
        if not picked:
            return False
        state['fields'] = set(picked)
        return True

    def _ask_mode() -> bool:
        """Fill blanks, or overwrite what is already there."""
        picked = prompt.select("When a tag already has a value:", choices=_MODES,
                               index=_MODES.index(state['mode']), header=header())
        if not picked:
            return False
        state['mode'] = picked
        return True

    def _ask_detect() -> bool:
        """How to read the names: guess, a template, or a regex."""
        picked = prompt.select("Detection:", choices=_DETECTS,
                               index=_DETECTS.index(state['detect']), header=header())
        if not picked:
            return False
        state['detect'] = picked
        return True

    def _ask_template():
        """The naming template — only for that detection mode."""
        if state['detect'] != "Use a naming template":
            return _SKIP
        while True:
            raw = prompt.text("Template (e.g. %disc%-%track% %title%; tokens: %track% "
                              "%disc% %title% %artist% %albumartist% %album% %year% "
                              "%date% %season% %episode% %ignore%):",
                              default=state['template'])
            if not raw:
                return False
            state['template'] = raw          # kept even when it needs fixing
            try:
                fp.compile_template(raw)
            except fp.TemplateError as e:
                ui_utils.show_status(f"Invalid template: {e}")
                continue                     # ask again, don't leave the screen
            return True

    def _ask_regex_target():
        """File name or folder path — only for regex detection."""
        if state['detect'] != "Use a regex":
            return _SKIP
        # A vs B: match the file name, or the path from the library root so the
        # regex can capture folder levels (Artist/Album/…).
        picked = prompt.select("Match regex against:", choices=_TARGETS,
                               index=_TARGETS.index(state['target']), header=header())
        if not picked:
            return False
        state['target'] = picked
        return True

    def _ask_regex():
        """The regex itself — only for regex detection."""
        if state['detect'] != "Use a regex":
            return _SKIP
        base = _derive_regex_base(writable) if state['target'] == "Folder path" else None
        sample = fp._regex_target(writable[0], base)
        while True:
            raw = prompt.text(
                rf"Regex, named groups (matches e.g. '{sample}'; use / between folders); "
                "groups: track disc title artist albumartist album year date season episode:",
                default=state['regex'])
            if not raw:
                return False
            state['regex'] = raw             # kept even when it needs fixing
            try:
                compiled = fp.compile_regex(raw)
            except fp.TemplateError as e:
                ui_utils.show_status(f"Invalid regex: {e}")
                continue                     # ask again, don't leave the screen
            unknown = fp.unrecognised_regex_groups(compiled)
            if unknown:
                ui_utils.show_status(
                    f"Ignoring unrecognised group(s): {', '.join(unknown)}")
            return True

    def _ask_preview() -> bool:
        """Derive everything, then show what each file would get."""
        apply_fields = state['fields']
        overwrite = (state['mode'] == "Overwrite existing")
        template = state['template'] if state['detect'] == "Use a naming template" else None
        regex = state['regex'] if state['detect'] == "Use a regex" else None
        regex_base = (_derive_regex_base(writable)
                      if state['detect'] == "Use a regex" and state['target'] == "Folder path"
                      else None)
        derived = fp.derive_all(writable, template=template, regex=regex, regex_base=regex_base)
        present_cache = {p: tw.present_fields(p) for p in writable}
        plans = {p: _plan_write(derived[p], apply_fields, overwrite, present_cache[p], p)
                 for p in writable}
        to_write = [p for p in writable if plans[p]]

        if not to_write:
            ui_utils.show_status("Nothing to write — selected fields are already set "
                                 "(try Overwrite).")
            return False                     # back to the questions, not out

        # 4) Preview. Fields whose value is identical across every changed file are
        # lifted into the header (shown once); only the *varying* fields become
        # per-row columns, so wide rows don't overflow. `d` opens a full detail view.
        field_vals: dict = {}
        for p in to_write:
            for f, v in plans[p].items():
                field_vals.setdefault(f, set()).add(v)
        any_comp = any(derived[p].compilation for p in to_write) and 'album_artist' in apply_fields
        uniform = {f for f, vals in field_vals.items() if len(vals) == 1}
        varying = [f for f in tw.FIELDS if f in field_vals and f not in uniform]

        # Header: uniform fields + counts.
        header_bits = [ui_utils.plural(len(to_write), "file")]
        for f in tw.FIELDS:
            if f in uniform:
                header_bits.append(f"{_DERIVE_LABELS[f]}: {next(iter(field_vals[f]))}")
        if any_comp:
            header_bits.append("compilation")
        n_skip_existing = sum(
            1 for p in writable for f in apply_fields
            if present_cache[p].get(f) and not overwrite
            and derived[p].as_dict().get(f) not in (None, ""))
        if n_skip_existing and not overwrite:
            header_bits.append(f"{n_skip_existing} existing kept")
        if skipped_fmt:
            header_bits.append(f"{skipped_fmt} non-MP3/MP4 skipped")
        # Called out explicitly (not silent): disc subtitle can't be stored on MP4.
        if 'disc_subtitle' in apply_fields:
            n_mp4 = sum(1 for p in writable if tw.format_kind(p) == 'mp4')
            if n_mp4:
                header_bits.append(f"disc subtitle N/A on {ui_utils.plural(n_mp4, 'MP4 file')}")
        if 'sort' in apply_fields:
            header_bits.append("+ sort orders")
        sub = " · ".join(header_bits)

        # Columns: file name + one truncating column per varying field.
        prev_cols = [prompt.Column(style='primary', max_frac=0.35)]
        for f in varying:
            prev_cols.append(prompt.Column(**_DERIVE_FIELD_COL.get(
                f, {'style': 'dynamic-dim', 'max_frac': 0.3})))

        preview_choices = []
        for p in to_write:
            cells = [os.path.basename(p)] + [plans[p].get(f, "") for f in varying]
            preview_choices.append(prompt.Choice(
                title=os.path.basename(p), value=p, cells=cells,
                checked=state['apply_paths'] is None or p in state['apply_paths']))

        def _show_detail(path) -> None:
            """Open the full derivation detail view for one previewed file."""
            _detail_view(path, derived[path], plans[path], present_cache[path],
                         apply_fields, overwrite, header)

        selected = prompt.select(
            "Preview — ↵ applies:",
            choices=preview_choices, columns=prev_cols,
            header=header(sub), multi=True,
            extra_hints={'d': 'details'}, on_inspect=_show_detail)
        if selected is None:
            return False
        state.update(derived=derived, plans=plans, to_write=to_write,
                     apply_paths=set(selected))
        return True

    if not _walk([_ask_fields, _ask_mode, _ask_detect, _ask_template,
                  _ask_regex_target, _ask_regex, _ask_preview]):
        return

    apply_fields = state['fields']
    overwrite = (state['mode'] == "Overwrite existing")
    derived, plans = state['derived'], state['plans']
    to_write, apply_paths = state['to_write'], state['apply_paths']
    if not apply_paths:
        ui_utils.show_status("No files selected.")
        return

    # 5) Apply.
    count = 0
    errors = 0
    for p in to_write:
        if p not in apply_paths:
            continue
        vw = derived[p].as_dict()
        if 'sort' in apply_fields:
            _augment_sort(vw, apply_fields)
        r = tw.write_fields(p, vw, apply_fields, overwrite=overwrite)
        if r.error:
            errors += 1
            continue
        if r.written:
            count += 1
            try:
                refresh_library_entry(library, p)
            except Exception:
                pass

    msg = f"Derived tags for {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} non-MP3/MP4 skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


# Preview columns for the pattern assignment: position · file · assigned value.
# Position is just a list index, so it drops first; file and the assigned value
# (the pending change) are kept.
# Per-range schedule table. START holds a whole "YYYY-MM-DD HH:MM:SS" (19 cols)
# at any terminal width; FROM/TO/EVERY/STEP are short and shrink around it.
_SCHEDULE_COL_MINS = (4, 4, 19, 5, 5)
# The numeric columns sit at their minimums and START gets just enough for a
# whole stamp; whatever the terminal has left over falls into the last column as
# trailing space, where it reads as a margin rather than a gap mid-row.
_SCHEDULE_COL_RATIOS = (4, 4, 21, 5, 46)

_PATTERN_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4, priority=1),
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='normal', max_frac=0.42),
]


# source text frame → sort frame, for the standalone "apply sort orders" op.
_SORT_SRC = [
    ('artist',       'TPE1', 'TSOP'),
    ('album_artist', 'TPE2', 'TSO2'),
    ('composer',     'TCOM', 'TSOC'),
    ('album',        'TALB', 'TSOA'),
]

_RENUMBER_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4, priority=1),  # position (index) — drops first
    prompt.Column(style='primary', flex=True),                       # file (kept)
    prompt.Column(style='dynamic-dim', align='right', pin=True),  # old → new (the change, kept)
]


def renumber_tracks_op(paths: list, library: list, header) -> None:
    """Renumber track numbers per-disc (disc-relative) ↔ continuous (album-
    relative / movement systems). Works for MP3 and MP4 via tag_writer."""
    # Disc/track numbering is read from the files, not the library cache. Taking
    # the discs from a stale cache silently flattened a multi-disc selection into
    # one group, so a *per-disc* renumber wrote a single continuous 1…N run over
    # every disc — the exact thing you reach for this after inserting a disc.
    songs = []
    for p in paths:
        if not tw.is_writable(p):
            continue
        pairs = tw.read_number_pairs(p)
        songs.append({'path': p, 'disc': pairs['disc'], 'track': pairs['track']})
    skipped_fmt = len(paths) - len(songs)
    writable = bp.order_tracks(songs)
    if not writable:
        ui_utils.show_status("No MP3/MP4 tracks to renumber.")
        return

    _MODES = ["Continuous (album-relative) — 1…N across all discs",
              "Per-disc (disc-relative) — restart at 1 each disc"]
    state: dict = {'mode_sel': _MODES[0]}

    def _ask_mode() -> bool:
        """Which numbering to lay down."""
        sel = prompt.select("Renumber to:", choices=_MODES,
                            index=_MODES.index(state['mode_sel']),
                            header=header(f"{len(writable)} tracks in disc/track order"))
        if not sel:
            return False
        state['mode_sel'] = sel
        return True

    def _ask_preview() -> bool:
        """Show what each file becomes and take the selection."""
        mode = 'continuous' if state['mode_sel'].startswith("Continuous") else 'per_disc'
        plan = bp.renumber_tracks(writable, mode)    # {path: (track, total)}
        state['plan'] = plan
        pos = {s['path']: i + 1 for i, s in enumerate(writable)}
        choices = []
        for s in writable:
            trk, total = plan[s['path']]
            was = str(s.get('track', '') or '?')
            choices.append(prompt.Choice(
                title=os.path.basename(s['path']), value=s['path'],
                checked=s['path'] in state.get('apply_set', {s['path']}),
                cells=[str(pos[s['path']]), os.path.basename(s['path']),
                       f"{was} → {trk}/{total}"]))
        sub = ui_utils.plural(len(choices), "file") + (
            f" · {skipped_fmt} unsupported skipped" if skipped_fmt else "")
        sel = prompt.select("Preview — ↵ applies:", choices=choices,
                            columns=_RENUMBER_COLUMNS, header=header(sub), multi=True)
        if sel is None:
            return False
        state['apply_set'] = set(sel)
        return True

    if not _walk([_ask_mode, _ask_preview]):
        return
    plan = state['plan']
    apply_set = state['apply_set']
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    count = errors = 0
    for s in writable:
        p = s['path']
        if p not in apply_set:
            continue
        trk, total = plan[p]
        r = tw.write_fields(p, {'track': trk, 'total_tracks': total}, {'track'}, overwrite=True)
        if r.error:
            errors += 1
        elif r.written:
            count += 1
            try:
                refresh_library_entry(library, p)
            except Exception:
                pass

    msg = f"Renumbered {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} unsupported skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def reflow_discs_op(paths: list, library: list, header) -> None:
    """Re-flow disc numbering after a disc is inserted, removed or appended.

    Renumbering the distinct disc values onto a dense 1…N handles all three edits
    with one rule: a disc parked at ``1.5`` becomes 2 and everything above shifts
    up; a deleted disc closes its gap; an appended disc keeps its number and only
    the totals move. "Totals only" fixes the "of N" half without renumbering, for
    deliberately sparse discs. MP3 and MP4 both write, via tag_writer.
    """
    # Numbering comes from the files, not the library cache: you reach for this
    # right after hand-numbering a disc 1.5, when the cache has not caught up.
    songs = []
    for p in paths:
        if not tw.is_writable(p):
            continue
        pairs = tw.read_number_pairs(p)
        songs.append({'path': p, 'disc': pairs['disc'], 'track': pairs['track'],
                      'total_discs': pairs['total_discs'],
                      'total_tracks': pairs['total_tracks']})
    skipped_fmt = len(paths) - len(songs)
    writable = bp.order_tracks(songs)
    if not writable:
        ui_utils.show_status("No MP3/MP4 tracks to reflow.")
        return

    runs = bp.disc_ranges(writable)
    disc_list = ', '.join(lab for _, _, lab in runs[:8]) + ('…' if len(runs) > 8 else '')
    sub = f"{len(writable)} tracks · discs {disc_list}"

    mode_sel = prompt.select(
        "Disc numbering:",
        choices=[f"Reflow — renumber the {len(runs)} disc(s) to 1…{len(runs)} and set totals",
                 "Totals only — set the disc total, keep the numbers as they are"],
        header=header(sub))
    if not mode_sel:
        return
    renumber = mode_sel.startswith("Reflow")

    which = prompt.select(
        "Totals to update:",
        choices=[prompt.Choice(title="Disc totals (the 'of N' in disc n/N)",
                               value='disc', checked=True),
                 prompt.Choice(title="Track totals (each disc's own track count)",
                               value='track')],
        header=header(sub), multi=True)
    if which is None:
        return
    disc_totals, track_totals = 'disc' in which, 'track' in which
    if not renumber and not disc_totals and not track_totals:
        ui_utils.show_status("Nothing to change — pick a total to update.")
        return

    plan = bp.reflow_discs(writable, renumber=renumber,
                           disc_totals=disc_totals, track_totals=track_totals)

    def _old_pair(s, cur_key, tot_key) -> str:
        """The track's existing 'n/N' for a pair field, as stored."""
        raw = str(s.get(cur_key, '') or '').strip()
        if '/' in raw:
            return raw
        tot = str(s.get(tot_key, '') or '').strip()
        return f"{raw or '?'}/{tot}" if tot else (raw or '?')

    def _new_pair(f: dict, cur_key: str, tot_key: str) -> str:
        """The planned 'n/N' for a pair field."""
        return f"{f[cur_key]}/{f[tot_key]}" if tot_key in f else str(f[cur_key])

    pos = {s['path']: i + 1 for i, s in enumerate(writable)}
    choices, n_changed = [], 0
    for s in writable:
        f = plan[s['path']]
        bits = []
        d_old, d_new = _old_pair(s, 'disc', 'total_discs'), _new_pair(f, 'disc', 'total_discs')
        if d_old != d_new:
            bits.append(f"disc {d_old} → {d_new}")
        if track_totals:
            t_old = _old_pair(s, 'track', 'total_tracks')
            t_new = _new_pair(f, 'track', 'total_tracks')
            if t_old != t_new:
                bits.append(f"track {t_old} → {t_new}")
        changed = bool(bits)
        n_changed += changed
        # Unchanged rows stay listed but unticked — no point rewriting a file
        # whose numbering the reflow leaves exactly as it was.
        choices.append(prompt.Choice(
            title=os.path.basename(s['path']), value=s['path'], checked=changed,
            cells=[str(pos[s['path']]), os.path.basename(s['path']),
                   ' · '.join(bits) if changed else 'no change']))

    if not n_changed:
        # Already dense with the right totals — say so, rather than showing an
        # all-unticked preview that ends in "No tracks selected".
        ui_utils.show_status(
            f"Disc numbering is already 1…{len(runs)} with matching totals — nothing to do.")
        return

    def _reflow_header():
        """Live counts for the reflow preview."""
        nk = sum(1 for ch in choices if ch.checked)
        bits = [ui_utils.plural(len(writable), "track"), f"{n_changed} changing", f"{nk} ticked"]
        if skipped_fmt:
            bits.append(f"{skipped_fmt} unsupported skipped")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview — ↵ applies:", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=_reflow_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return

    fields = {'disc'} | ({'track'} if track_totals else set())
    count = errors = 0
    for s in writable:
        p = s['path']
        if p not in apply_set:
            continue
        r = tw.write_fields(p, plan[p], fields, overwrite=True)
        if r.error:
            errors += 1
        elif r.written:
            count += 1
            try:
                refresh_library_entry(library, p)
            except Exception:
                pass

    msg = f"Reflowed disc numbering on {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} unsupported skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def _picture_type_name(pic_type) -> str:
    """Human label for an APIC picture-type byte ("Cover (front)", "Other"…)."""
    return dict(_PICTURE_TYPES).get(int(pic_type), f"type {pic_type}")


def set_picture_type_op(paths: list, library: list, header) -> None:
    """Set the picture type on art that's already embedded.

    Rippers routinely tag a front cover as "Other" (type 0), which anything
    looking specifically for a front cover then misses. This retypes in bulk
    without touching the image. MP3 only — MP4's `covr` atom has no type field.
    """
    art = []
    for path in paths:
        if not path.lower().endswith('.mp3'):
            continue
        try:
            tags = ID3(path)
        except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
            continue
        frames = [tags[k] for k in tags if k.startswith('APIC')]
        if frames:
            art.append({'path': path,
                        'types': [int(getattr(f, 'type', 3)) for f in frames]})
    skipped = len(paths) - len(art)
    if not art:
        ui_utils.show_status("No MP3s with embedded art in this selection.")
        return

    counts = Counter(t for a in art for t in a['types'])
    seen = ' · '.join(f"{_picture_type_name(t)} ×{n}" for t, n in counts.most_common())

    pic_type = _prompt_for_picture_type(
        initial=3, header=lambda: header(f"{len(art)} file(s) with art · {seen}")())
    if pic_type is None:
        return

    pos = {a['path']: i + 1 for i, a in enumerate(art)}
    choices, n_changed = [], 0
    for a in art:
        stale = [t for t in a['types'] if t != pic_type]
        n_changed += bool(stale)
        was = ' · '.join(_picture_type_name(t) for t in a['types'])
        choices.append(prompt.Choice(
            title=os.path.basename(a['path']), value=a['path'], checked=bool(stale),
            cells=[str(pos[a['path']]), os.path.basename(a['path']),
                   f"{was} → {_picture_type_name(pic_type)}" if stale else "already correct"]))

    if not n_changed:
        ui_utils.show_status(f"Every image is already {_picture_type_name(pic_type)}.")
        return

    def _type_header():
        """Live counts for the preview."""
        nk = sum(1 for ch in choices if ch.checked)
        bits = [f"{len(art)} file(s) with art", f"{n_changed} to retype", f"{nk} ticked"]
        if skipped:
            bits.append(f"{skipped} without art or not MP3")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview — ↵ applies:", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=_type_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return

    count = errors = 0
    for a in art:
        if a['path'] not in apply_set:
            continue
        r = tw.retype_cover(a['path'], pic_type)
        if r.error:
            errors += 1
        elif r.written:
            count += 1
            try:
                refresh_library_entry(library, a['path'])
            except Exception:
                pass

    msg = f"Set {_picture_type_name(pic_type)} on {count} file(s)."
    if skipped:
        msg += f" {skipped} without art or not MP3."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def strip_single_disc_op(paths: list, library: list, header) -> None:
    """Remove the disc number from tracks that are disc 1 of 1.

    A single-disc release doesn't need a disc tag: "1/1" is noise that shows up as
    a disc header in browse lists and in file names derived from tags. Bare "1"
    (no total) counts too, but only when nothing in the selection sits on another
    disc — on a real multi-disc album an untotalled "1" is meaningful.
    """
    songs = []
    for path in paths:
        if not tw.is_writable(path):
            continue
        pairs = tw.read_number_pairs(path)
        songs.append({'path': path, 'disc': pairs['disc'].strip(),
                      'total_discs': pairs['total_discs'].strip(),
                      'track': pairs['track'], 'total_tracks': pairs['total_tracks']})
    skipped_fmt = len(paths) - len(songs)
    ordered = bp.order_tracks(songs)
    if not ordered:
        ui_utils.show_status("No MP3/MP4 tracks to change.")
        return

    # Is every disc value in this selection either absent or 1?
    single_disc = not {s['disc'] for s in ordered} - {'', '1'}

    def _why(s: dict) -> str:
        """Why this track is (or isn't) a candidate."""
        disc, total = s['disc'], s['total_discs']
        if not disc:
            return ""                                  # nothing to remove
        if total == '1':
            return f"disc {disc}/{total} → —"
        if not total and disc == '1' and single_disc:
            return "disc 1 → —"
        return ""

    pos = {s['path']: i + 1 for i, s in enumerate(ordered)}
    choices, n_changed = [], 0
    for s in ordered:
        why = _why(s)
        n_changed += bool(why)
        stored = s['disc'] + (f"/{s['total_discs']}" if s['total_discs'] else "")
        choices.append(prompt.Choice(
            title=os.path.basename(s['path']), value=s['path'], checked=bool(why),
            cells=[str(pos[s['path']]), os.path.basename(s['path']),
                   why or (f"keeps disc {stored}" if stored else "no disc number")]))

    if not n_changed:
        ui_utils.show_status("No tracks are disc 1 of 1 — nothing to remove.")
        return

    def _strip_header():
        """Live counts for the preview."""
        nk = sum(1 for ch in choices if ch.checked)
        bits = [ui_utils.plural(len(ordered), "track"), f"{n_changed} with 1/1", f"{nk} ticked"]
        if skipped_fmt:
            bits.append(f"{skipped_fmt} unsupported skipped")
        return header(' · '.join(bits))()

    sel = prompt.select("Preview — ↵ applies:", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=_strip_header, multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No tracks selected.")
        return

    count = errors = 0
    for s in ordered:
        if s['path'] not in apply_set:
            continue
        r = tw.clear_fields(s['path'], {'disc'})
        if r.error:
            errors += 1
        elif r.written:
            count += 1
            try:
                refresh_library_entry(library, s['path'])
            except Exception:
                pass

    msg = f"Removed the disc number from {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} unsupported skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


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
# Rename preview: position · old name · new name. Position (index) drops first;
# both names are the pending change and are kept.
_RENAME_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4, priority=1),
    prompt.Column(style='dynamic-dim', flex=True),
    prompt.Column(style='primary', flex=True),
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
                                  cells=[f"%track{suffix}%", f"{desc} — any numeric token"]))
    rows.append(prompt.Choice(title="%track:r:l%", value="__style_case", disabled=True,
                              cells=["%track:r:l%", f"case: {numbering.CASES}"]))
    rows.append(prompt.separator())
    rows.append(prompt.Choice(title="Back", value="__back__"))
    prompt.select("Available tokens:", choices=rows, columns=_TOKENS_COLUMNS,
                  header=header("token reference"))


def rename_files_op(paths: list, library: list, header) -> None:
    """Rename files on disk from their tags via a %token% pattern (the inverse of
    "Derive from filename"). Preset or custom pattern; the default includes
    %artist% when track artists vary. Collision-safe two-phase rename that keeps
    the library paths in sync. MP3 + MP4."""
    writable = [p for p in paths if fnm.is_supported(p)]
    skipped_fmt = len(paths) - len(writable)
    if not writable:
        ui_utils.show_status("No MP3/MP4 files to rename.")
        return

    tokens = {p: fnm.read_tokens(p) for p in writable}

    # Smart default: fold the artist into the name only when it varies across
    # the selection (a compilation / mixed artists); a single-artist album omits it.
    vary = fnm.artists_vary(writable, tokens)
    default_pattern = '%track% %artist% - %title%' if vary else '%track% %title%'

    state: dict = {'pattern': None, 'apply_set': None}

    def _ask_pattern() -> bool:
        """Pick a preset, type a pattern, or browse the token reference."""
        while True:
            choices: list = []
            for pat, example in fnm.PRESETS:
                tail = f"e.g. {example}" + ("   ★ suggested" if pat == default_pattern else "")
                choices.append(prompt.Choice(title=pat, value=pat, cells=[pat, tail]))
            choices.append(prompt.separator())
            choices.append(prompt.Choice(title="Custom pattern…", value="__custom__",
                                         cells=["Custom pattern…", "type your own %token% pattern"]))
            choices.append(prompt.Choice(title="Show all tokens", value="__tokens__",
                                         cells=["Show all tokens", f"{len(fnm.TOKENS)} available"]))
            # Reopen on whatever was chosen last, else on the suggested preset.
            _prev = state['pattern']
            default_idx = next((i for i, (pat, _) in enumerate(fnm.PRESETS)
                                if pat == (_prev or default_pattern)), 0)
            sub = ui_utils.plural(len(writable), "file") + (
                " · artists vary → artist suggested" if vary else "")
            sel = prompt.select("File-name pattern:", choices=choices,
                                columns=_RENAME_PICK_COLUMNS, header=header(sub),
                                index=default_idx)
            if not sel:
                return False
            if sel == "__tokens__":
                _show_tokens(header)
                continue
            if sel == "__custom__":
                raw = prompt.text(
                    "Pattern (e.g. %disc%-%track% %title% — 'Show all tokens' lists them):",
                    default=_prev or default_pattern)
                if not raw:
                    continue                 # back out of typing → the preset list
                unk = fnm.unknown_tokens(raw)
                if unk:
                    ui_utils.show_status(
                        f"Unknown token(s) will render blank: {', '.join(unk)}")
                state['pattern'] = raw
            else:
                state['pattern'] = sel
            return True

    def _ask_preview() -> bool:
        """Show old → new for every file the pattern changes."""
        plan = fnm.plan_renames(writable, state['pattern'], tokens)   # [(path, old, new)]
        changed = [(p, o, n) for (p, o, n) in plan if o != n]
        state['changed'] = changed
        if not changed:
            ui_utils.show_status("File names already match the pattern.")
            return False                     # back to the pattern, not out
        keep = state['apply_set']
        choices = [prompt.Choice(title=os.path.basename(p), value=p,
                                 checked=keep is None or p in keep,
                                 cells=[str(i + 1), o, n])
                   for i, (p, o, n) in enumerate(changed)]
        sub = f"{len(changed)} to rename" + (
            f" · {skipped_fmt} unsupported skipped" if skipped_fmt else "")
        sel = prompt.select("Preview — ↵ renames:", choices=choices,
                            columns=_RENAME_COLUMNS, header=header(sub), multi=True)
        if sel is None:
            return False
        state['apply_set'] = set(sel)
        return True

    if not _walk([_ask_pattern, _ask_preview]):
        return
    changed = state['changed']
    apply_set = state['apply_set']
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    import tempfile
    todo = [(p, os.path.join(os.path.dirname(p), n)) for (p, o, n) in changed if p in apply_set]

    # Phase 1: move each source to a unique temp name, so a target that equals
    # another (not-yet-moved) selected file's current name can't clobber it.
    staged: list[tuple[str, str, str]] = []   # (orig, temp, final)
    errors = 0
    for orig, final in todo:
        d = os.path.dirname(orig)
        try:
            fd, tmp = tempfile.mkstemp(prefix='.rn_', dir=d, suffix=os.path.splitext(orig)[1])
            os.close(fd)
            os.replace(orig, tmp)
            staged.append((orig, tmp, final))
        except OSError as e:
            errors += 1
            ui_utils.show_status(f"Could not rename {os.path.basename(orig)}: {e}")

    # Phase 2: move each temp to its final name and keep the library in sync.
    count = 0
    for orig, tmp, final in staged:
        try:
            os.replace(tmp, final)
        except OSError as e:
            errors += 1
            ui_utils.show_status(f"Could not rename to {os.path.basename(final)}: {e}")
            try:
                os.replace(tmp, orig)   # roll this one back
            except OSError:
                pass
            continue
        count += 1
        for track in library:
            if track.get('path') == orig:
                track['path'] = final
                break
        try:
            refresh_library_entry(library, final)
        except Exception:
            pass

    msg = f"Renamed {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} unsupported skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


# Per-file album art: track · matched cover image · confidence. Confidence is
# advisory, so it drops first; track and the matched image (the change) are kept.
_COVER_PREVIEW_COLUMNS = [
    prompt.Column(style='primary', max_frac=0.42),
    prompt.Column(style='normal', flex=True),
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=1),
]

# Image-name patterns offered for the "%token%" match mode.
_COVER_PATTERN_PRESETS = [
    ('%track%', '01'),
    ('%track% %title%', '01 Song'),
    ('%title%', 'Song'),
    ('%tracknopad%', '1'),
    ('%album% %track%', 'Album 01'),
]


def _img_label(image_path: str, track_dir: str) -> str:
    """Image name, prefixed with its subfolder when it isn't beside the track."""
    d = os.path.dirname(os.path.abspath(image_path))
    if os.path.abspath(track_dir) != d:
        return f"{os.path.basename(d)}/{os.path.basename(image_path)}"
    return os.path.basename(image_path)


def _cover_pattern_prompt(header) -> str | None:
    """Pick / type a %token% pattern for matching image file names."""
    choices: list = []
    for pat, ex in _COVER_PATTERN_PRESETS:
        choices.append(prompt.Choice(title=pat, value=pat, cells=[pat, f"e.g. {ex}"]))
    choices.append(prompt.separator())
    choices.append(prompt.Choice(title="Custom pattern…", value="__custom__",
                                 cells=["Custom pattern…", "type your own %token%"]))
    choices.append(prompt.Choice(title="Show all tokens", value="__tokens__",
                                 cells=["Show all tokens", f"{len(fnm.TOKENS)} available"]))
    while True:
        sel = prompt.select("Image-name pattern (matched against the image files):",
                            choices=choices, columns=_RENAME_PICK_COLUMNS,
                            header=header("cover-name pattern"))
        if not sel:
            return None
        if sel == "__tokens__":
            _show_tokens(header)
            continue
        if sel == "__custom__":
            raw = prompt.text("Pattern (e.g. %track% %title% — 'Show all tokens' lists them):")
            if not raw:
                continue
            unk = fnm.unknown_tokens(raw)
            if unk:
                ui_utils.show_status(f"Unknown token(s) render blank: {', '.join(unk)}")
            return raw
        return sel


def set_album_art_op(paths: list, library: list, header) -> None:
    """Embed a per-track cover image, pairing each track with the image file that
    belongs to it (a folder of ``01 - Song.jpg`` … or ``covers/1.png`` …).

    Four pairing modes (auto / file name / track order / %token% pattern); a
    preview where ``d`` opens a ranked per-file picker to choose or change the
    cover; fill-blanks by default with per-row checkboxes as the final word.
    MP3 (APIC) and MP4 (``covr``; JPEG/PNG only) both write."""
    writable = [p for p in paths if tw.is_writable(p)]
    skipped_fmt = len(paths) - len(writable)
    if not writable:
        ui_utils.show_status("No MP3/MP4 files here to set art on.")
        return

    tokens = {p: fnm.read_tokens(p) for p in writable}

    # Candidate images per directory (a box set's disc folders keep their own art).
    dir_images: dict[str, list] = {}
    for p in writable:
        d = os.path.dirname(os.path.abspath(p))
        if d not in dir_images:
            dir_images[d] = cm.find_images(d)
    if not any(dir_images.values()):
        ui_utils.show_status("No images found beside these tracks "
                             "(looked in the folder + artwork/covers/scans).")
        return
    n_images = len({img for imgs in dir_images.values() for img in imgs})

    # 1) Matching strategy. Each answer is kept in `state`, so a screen reopened
    # by walking back holds what it was left holding — including covers chosen by
    # hand in the preview, which survive the plan being rebuilt.
    _MODES = ["Auto-detect (recommended)",
              "One cover per disc / series / work",
              "Matching file name",
              "Track number / order",
              "Name pattern (%token%)"]
    _GROUPS = {"Auto (disc, else series, else work)": 'auto',
               "Disc number": 'disc',
               "Series / season (SxxExx)": 'season',
               "Work / grouping": 'work'}
    _POLICIES = ["Fill blanks only", "Overwrite existing"]
    state: dict = {'mode': _MODES[0], 'group': list(_GROUPS)[0], 'pattern': None,
                   'policy': _POLICIES[0], 'manual': {}, 'apply_paths': None}

    def _ask_mode() -> bool:
        """How images pair with tracks."""
        picked = prompt.select("Match cover images to tracks by:", choices=_MODES,
                               index=_MODES.index(state['mode']),
                               header=header(f"{n_images} image(s) found"))
        if not picked:
            return False
        state['mode'] = picked
        return True

    def _ask_group():
        """What counts as a group — only when one cover covers a group."""
        if not state['mode'].startswith("One cover per"):
            return _SKIP
        picked = prompt.select("Group tracks by:", choices=list(_GROUPS),
                              index=list(_GROUPS).index(state['group']), header=header())
        if not picked:
            return False
        state['group'] = picked
        return True

    def _ask_pattern():
        """The image-name pattern — only for pattern matching."""
        if state['mode'] != "Name pattern (%token%)":
            return _SKIP
        picked = _cover_pattern_prompt(header)
        if picked is None:
            return False
        state['pattern'] = picked
        return True

    def _ask_policy() -> bool:
        """Whether tracks that already have art start ticked."""
        picked = prompt.select("For tracks that already have art:", choices=_POLICIES,
                               index=_POLICIES.index(state['policy']), header=header())
        if not picked:
            return False
        state['policy'] = picked
        return True

    def _ask_preview() -> bool:
        """Pair everything up, then show it — `d` re-chooses one track's cover."""
        mode, pattern = state['mode'], state['pattern']
        grouped = mode.startswith("One cover per")
        group_by = _GROUPS[state['group']]
        overwrite_default = (state['policy'] == "Overwrite existing")

        # Build the plan per directory, so images only pair with tracks beside them.
        by_dir: dict[str, list] = {}
        for p in writable:
            by_dir.setdefault(os.path.dirname(os.path.abspath(p)), []).append(p)
        plan: dict = {}
        for d, group in by_dir.items():
            imgs = dir_images[d]
            if not imgs:
                plan.update({p: None for p in group})
            elif grouped:
                plan.update(cm.plan_grouped(group, imgs, group_by, tokens))
            elif mode.startswith("Auto"):
                plan.update(cm.plan_best(group, imgs, tokens))
            elif mode == "Matching file name":
                plan.update(cm.plan_basename(group, imgs, tokens))
            elif mode == "Track number / order":
                plan.update(cm.plan_positional(group, imgs, tokens))
            else:
                plan.update(cm.plan_template(group, imgs, pattern or '', tokens))
        # Covers chosen by hand in a previous pass through this screen win over
        # whatever the matcher would pair now.
        plan.update(state['manual'])

        existing_art = {p: tw.has_cover(p) for p in writable}

        # Only tracks whose folder has images are actionable (the rest have nothing
        # to pair with, auto or by hand).
        shown = [p for p in writable if dir_images[os.path.dirname(os.path.abspath(p))]]

        # A cover shared by 2+ tracks is group art — badge every one of its rows with
        # the group (disc/season/work) so the column stays consistent, instead of one
        # row flipping to "high" just because its track number happens to match the
        # cover's number. Per-track (unique) covers keep the match-confidence label.
        shared = {img for img, n in Counter(v for v in plan.values() if v).items() if n >= 2}

        def _conf(p: str) -> str:
            """Confidence/group label for a track's planned cover, or '' if unmatched."""
            img = plan.get(p)
            if not img:
                return ''
            if grouped or img in shared:
                return cm.group_label(p, tokens[p], group_by)
            return cm.confidence(cm.score_match(p, tokens[p], img))

        def _cover_cell(p: str):
            """Cell text for a track's planned cover: label, plus a "has art" badge if replacing."""
            img = plan.get(p)
            if not img:
                return "— none (d to choose) —"
            label = _img_label(img, os.path.dirname(os.path.abspath(p)))
            if existing_art[p]:
                return [(label, 'normal'), ('  · has art', 'static-dim')]
            return label

        def _checked(p: str) -> bool:
            """Default tick state: matched, and either overwriting or no existing art."""
            return bool(plan.get(p)) and (overwrite_default or not existing_art[p])

        choice_by_path: dict = {}
        preview_choices: list = []
        for p in shown:
            ch = prompt.Choice(title=os.path.basename(p), value=p, checked=_checked(p),
                               cells=[os.path.basename(p), _cover_cell(p), _conf(p)])
            choice_by_path[p] = ch
            preview_choices.append(ch)

        def _set_cover(p: str, img) -> None:
            """Point track ``p`` at ``img`` (or None) and refresh its preview row."""
            ch = choice_by_path[p]
            state['manual'][p] = img        # remembered if this screen is revisited
            if img is None:
                plan[p] = None
                ch.cells = [os.path.basename(p), "— none (d to choose) —", '']
                ch.checked = False
            else:
                plan[p] = img
                ch.cells = [os.path.basename(p), _cover_cell(p), _conf(p)]
                ch.checked = True               # an explicit pick means write it

        def _copy_scope(picked: str, path: str) -> list | None:
            """Ask how far a hand-picked cover should carry, and return the tracks it
            applies to (None if the user backed out).

            A cover chosen for an unmatched track is usually right for its
            neighbours too, but not always for the whole selection — a box set
            changes art per disc.  So the choice is: every track, everything from
            here down, a counted run from here, or this track alone.
            """
            below = shown[shown.index(path):]
            choices = [prompt.Choice(title=f"All {len(shown)} tracks", value="all")]
            if len(below) > 1 and len(below) != len(shown):
                choices.append(prompt.Choice(
                    title=f"This track and the {len(below) - 1} below it", value="down"))
            if len(below) > 1:
                choices.append(prompt.Choice(title="This track and the next N…", value="count"))
            choices.append(prompt.Choice(title="Just this track", value="one"))

            scope = prompt.select(f"Apply {os.path.basename(picked)} to:", choices=choices,
                                  header=header("apply cover"))
            if scope is None:
                return None                     # backed out — leave the row unchanged
            if scope == "all":
                return shown
            if scope == "down":
                return below
            if scope == "count":
                raw = prompt.text(f"How many tracks from here (1-{len(below)}):",
                                  default=str(len(below)))
                if raw is None:
                    return None
                try:
                    n = int(raw.strip())
                except ValueError:
                    ui_utils.show_status("Not a number — applied to this track only.")
                    return [path]
                n = max(1, min(n, len(below)))
                return below[:n]
            return [path]

        def _reassign(path: str) -> None:
            """Let the user pick/clear the cover for one track, then choose how far
            that cover carries down the selection."""
            d = os.path.dirname(os.path.abspath(path))
            picked = pick_nearby_cover(path, tokens=tokens[path], images=dir_images[d],
                                       current=plan.get(path), allow_none=True, header=header)
            if picked is None:
                return                          # backed out, no change
            if picked is CLEAR_COVER or not isinstance(picked, str):
                _set_cover(path, None)
                return
            targets = [path]
            if len(shown) > 1:
                targets = _copy_scope(picked, path)
                if targets is None:
                    return
            for tp in targets:
                _set_cover(tp, picked)

        n_match = sum(1 for p in shown if plan.get(p))
        n_nodir = len(writable) - len(shown)

        # Rebuilt on every render (select calls the header each frame), so the ticked
        # count tracks live as you Space/'a'/'d' through the list.
        def _preview_header():
            """Build the live status line (matched/ticked/skipped counts) for the cover preview."""
            nk = sum(1 for ch in preview_choices if ch.checked)
            bits = [ui_utils.plural(len(shown), "track"), f"{n_match} matched"]
            if n_nodir:
                bits.append(f"{n_nodir} without images")
            if skipped_fmt:
                bits.append(f"{skipped_fmt} unsupported skipped")
            if nk:
                bits.append(f"{nk} ticked")
            elif n_match:
                bits.append("0 ticked — existing art; Overwrite or Space/a to tick")
            return header(" · ".join(bits))()

        sel = prompt.select(
            "Preview — ↵ applies:",
            choices=preview_choices, columns=_COVER_PREVIEW_COLUMNS,
            header=_preview_header, multi=True,
            extra_hints={'d': 'choose cover'}, on_inspect=_reassign)
        if sel is None:
            return False
        state.update(plan=plan, shown=shown, apply_paths=set(sel))
        return True

    if not _walk([_ask_mode, _ask_group, _ask_pattern, _ask_policy, _ask_preview]):
        return
    plan, shown = state['plan'], state['shown']
    apply_paths = state['apply_paths']
    if not apply_paths:
        ui_utils.show_status("No tracks selected.")
        return

    # 4) Apply. The checkbox is explicit consent, so writes replace any existing
    #    art (fill-blanks was already honoured by the default check state).
    count = errors = mp4_skipped = 0
    img_cache: dict[str, tuple | None] = {}
    for p in shown:
        if p not in apply_paths:
            continue
        img = plan.get(p)
        if not img:
            continue
        if img not in img_cache:
            img_cache[img] = cm.read_image(img)
        read = img_cache[img]
        if not read:
            errors += 1
            continue
        data, mime = read
        r = tw.write_cover(p, data, mime, pic_type=3, desc='', overwrite=True)
        if r.skipped_format:
            mp4_skipped += 1
            continue
        if r.error:
            errors += 1
            continue
        if r.written:
            count += 1
            try:
                refresh_library_entry(library, p)
            except Exception:
                pass

    msg = f"Set album art on {count} track(s)."
    if mp4_skipped:
        msg += f" {mp4_skipped} MP4 skipped (cover needs JPEG/PNG)."
    if skipped_fmt:
        msg += f" {skipped_fmt} unsupported skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


_SORT_VALUE_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.4),                      # the name itself
    prompt.Column(style='dynamic-dim', flex=True),                                # sort order
    prompt.Column(style='dynamic-dim', max_width=22, priority=2),                 # where it came from
    prompt.Column(style='dynamic-dim', align='right', max_width=9, priority=1),   # how many files
]

_SORT_PERSON_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.45),
    prompt.Column(style='dynamic-dim', flex=True),
]

_SPLIT_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.4),                      # the value as tagged
    prompt.Column(style='dynamic-dim', flex=True),                                # the names in it
    prompt.Column(style='dynamic-dim', max_width=8, priority=2),                  # how many
    prompt.Column(style='dynamic-dim', align='right', max_width=9, priority=1),   # how many files
]

_SORT_TAG_LABEL = {'TSOP': 'artist', 'TSO2': 'album artist',
                   'TSOC': 'composer', 'TSOA': 'album'}


class _SortPlan:
    """What a bulk sort-order run will write, keyed by source value, not by file.

    One row per distinct artist/album value however many tracks carry it, so a
    decision made once covers all of them. Decisions are held per *person*
    rather than per value: settling "Somebody Else" as "Else, Somebody" in one
    collaboration settles it in every other value that person appears in, and on
    their solo tracks — which is the copying-down that makes reviewing a library
    of repeats bearable.
    """

    def __init__(self, entries: dict, delim: str) -> None:
        from src.id3 import id3_browser
        self._nb = id3_browser                 # lazy: id3_browser is the caller
        self.entries = entries                 # (sort_tag, raw) → [paths]
        self.delim = delim
        self.chosen: dict[str, str] = {}       # person → the sort text they were given
        self.splits: dict[str, list] = {}      # raw → the names it was verified to hold
        self.custom: dict[tuple, str] = {}     # entry → a whole value typed by hand

    # ── Splitting a value into people ──────────────────────────────────────
    def split_options(self, sort_tag: str, raw: str) -> list:
        """The ways this value could be read as a list of names (album tags: none)."""
        if sort_tag not in self._nb._NAME_SORT_TAGS:
            return []
        return self._nb.split_options(raw)

    def people(self, sort_tag: str, raw: str) -> list:
        """The individuals in one value — as verified, else as the engine reads it."""
        options = self.split_options(sort_tag, raw)
        if not options:
            return []
        return self.splits.get(raw) or options[0]

    def set_split(self, raw: str, people: list) -> None:
        """Record how a value really divides (the verification step's whole job)."""
        self.splits[raw] = list(people)

    # ── Sort values ────────────────────────────────────────────────────────
    def candidates(self, person: str) -> list:
        """Ranked sort orders offered for one person, their current pick first."""
        cur = self.person_sort(person)
        out = [cur]
        for c in self._nb._sort_single_name(person):
            if c not in out:
                out.append(c)
        if person not in out:
            out.append(person)                 # "leave it alone" is always an option
        return out

    def person_sort(self, person: str) -> str:
        """How one person sorts: their decision if made, else the engine's pick."""
        if person in self.chosen:
            return self.chosen[person]
        if self._nb._looks_inverted(person):
            return person                      # already in sort order
        return (self._nb._sort_single_name(person) or [person])[0]

    def value(self, sort_tag: str, raw: str) -> str:
        """The sort string this entry would write."""
        typed = self.custom.get((sort_tag, raw))
        if typed:
            return typed
        people = self.people(sort_tag, raw)
        if not people:
            return _sort_value(sort_tag, raw) or raw
        return self.delim.join(self.person_sort(p) for p in people)

    def writes(self, sort_tag: str, raw: str) -> bool:
        """Whether this entry still has something worth writing."""
        v = self.value(sort_tag, raw)
        return bool(v) and v != raw


def _split_keys(plan: _SortPlan) -> list:
    """Values that could be read more than one way — the only ones worth checking."""
    keys = [k for k in plan.entries if len(plan.split_options(*k)) > 1]
    keys.sort(key=lambda k: k[1].lower())
    return keys


def _verify_splits(plan: _SortPlan, header, note: str = "") -> bool:
    """Confirm how each value divides into names, before any of them is sorted.

    Splitting is guesswork — an ampersand joins two artists in one credit and is
    part of one act's name in the next, and a comma does three different jobs —
    and every sort order downstream is built on the answer. So the guesses come
    first, one row per value that could be read more than one way, and `e` cycles
    the readings: the engine's, the value whole as a single name, the maximal
    split, or one typed by hand with " / " between the names.

    Values with only one reading never appear; nor do albums, which hold nobody.
    Returns False if the step was abandoned.
    """
    keys = _split_keys(plan)
    if not keys:
        return True

    def _shown(people: list) -> str:
        return " / ".join(people)

    def _cells(key: tuple) -> list:
        people = plan.people(*key)
        return [key[1], _shown(people),
                ui_utils.plural(len(people), 'name'),
                ui_utils.plural(len(plan.entries[key]), 'file')]

    rows = {k: prompt.Choice(title=k[1], value=k, cells=_cells(k)) for k in keys}

    def _options(key: tuple) -> list:
        return [_shown(o) for o in plan.split_options(*key)]

    def _commit(key: tuple, text: str) -> None:
        """Take the chosen (or typed) splitting for this value."""
        people = [p.strip() for p in re.split(r'\s*[/·]\s*', text) if p.strip()]
        if people:
            plan.set_split(key[1], people)
            rows[key].cells = _cells(key)

    sub = f"{ui_utils.plural(len(keys), 'value')} to check{note}"
    sel = prompt.select("Do these divide correctly? — ↵ continues:",
                        choices=[rows[k] for k in keys], columns=_SPLIT_COLUMNS,
                        header=header(sub), row_edit=_options,
                        row_edit_commit=_commit, row_edit_col=1)
    return sel is not None


def _review_sort_people(plan: _SortPlan, header, note: str = "") -> set | None:
    """One flat list of everything that needs a sort order, each thing once.

    Not one row per tag value but one per *individual*: an artist, an album
    artist and a composer with the same name are one person and one decision,
    however many tracks and however many collaborations they turn up in. Albums,
    having nobody in them, are rows of their own. Who the people are was settled
    in the split-verification step before this one.

    `e` cycles a row's sort order through its candidates in place, one press per
    option, with one step past the last being a text field to type your own — no
    second screen for any of it. ↵ writes the checked rows; unchecking a person
    leaves every value they appear in alone.
    """
    def _index() -> tuple:
        """Group the entries into people and albums."""
        people: dict = {}
        albums = []
        for key, paths in plan.entries.items():
            sort_tag, raw = key
            if not plan.split_options(sort_tag, raw):
                albums.append(key)
                continue
            for person in plan.people(sort_tag, raw):
                rec = people.setdefault(person, {'paths': set(), 'tags': set()})
                rec['paths'].update(paths)
                rec['tags'].add(_SORT_TAG_LABEL.get(sort_tag, sort_tag))
        return people, albums

    def _rows() -> list:
        """The flat list: people, then albums."""
        people, albums = _index()
        out = []
        for name in sorted(people, key=str.lower):
            rec = people[name]
            out.append((('person', name),
                        [name, plan.person_sort(name),
                         " · ".join(sorted(rec['tags'])),
                         ui_utils.plural(len(rec['paths']), 'file')]))
        for key in sorted(albums, key=lambda k: k[1].lower()):
            out.append((('album', key),
                        [key[1], plan.value(*key), 'album',
                         ui_utils.plural(len(plan.entries[key]), 'file')]))
        return out

    # Rows are rebuilt on every edit, but Choice objects are reused where the row
    # survives, so checkbox state and the cursor stay put.
    made: dict = {}

    def _choices() -> list:
        out = []
        for rid, cells in _rows():
            choice = made.get(rid)
            if choice is None:
                choice = made[rid] = prompt.Choice(title=cells[0], value=rid, checked=True)
            choice.cells = cells
            out.append(choice)
        return out

    def _options(rid: tuple) -> list:
        """What `e` cycles this row through, its current value first."""
        kind, payload = rid
        if kind == 'person':
            return plan.candidates(payload)
        return [plan.value(*payload)] + list(plan._nb._sort_candidates(*payload))

    def _commit(rid: tuple, text: str) -> None:
        """Record one row's choice, then refresh them all — a person reaches many."""
        kind, payload = rid
        if kind == 'person':
            plan.chosen[payload] = text
        else:
            plan.custom[payload] = text
        for rid_, cells in _rows():
            if rid_ in made:
                made[rid_].cells = cells

    choices = _choices()
    files = len({p for ps in plan.entries.values() for p in ps})
    sub = f"{ui_utils.plural(len(choices), 'name')} · {ui_utils.plural(files, 'file')}{note}"
    sel = prompt.select("Preview — ↵ applies:", choices=choices,
                        columns=_SORT_VALUE_COLUMNS, header=header(sub), multi=True,
                        row_edit=_options, row_edit_commit=_commit, row_edit_col=1)
    return None if sel is None else set(sel)


def apply_sort_orders(paths: list, library: list, header) -> None:
    """Generate smart sort-order tags (TSOP/TSO2/TSOC/TSOA) from each file's
    existing artist/album-artist/composer/album, via the #42 engine. MP3/ID3 only.

    Runs as a sequence of screens you can walk backwards through: back on the
    first one leaves, and back on any other returns to the one before it with
    everything you had already decided still there. Splits are confirmed before
    sort orders (see _verify_splits), then reviewed one individual at a time
    (_review_sort_people). No title sort: a title sorts on itself, so TSOT is
    never generated. Nor is a sort tag whose value would equal its source.
    """
    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    skipped_fmt = len(paths) - len(mp3s)
    note = f" · {skipped_fmt} non-MP3 skipped" if skipped_fmt else ""

    from src.config import load_config
    from src.id3 import id3_browser as nb
    delim = load_config().get('sort_list_delimiter', '/')

    _FIELDS = [('artist', "Artist sort (TSOP)"), ('album_artist', "Album-artist sort (TSO2)"),
               ('composer', "Composer sort (TSOC)"), ('album', "Album sort (TSOA)")]
    _MODES = ["Fill blanks only", "Overwrite existing"]

    chosen: set = {f for f, _ in _FIELDS}
    mode = _MODES[0]
    plan: _SortPlan | None = None
    scanned_for: tuple | None = None       # what the current plan was scanned for
    checked: set = set()

    def _scan(overwrite: bool) -> dict:
        """Read the selection into (sort_tag, raw) → paths."""
        entries: dict = {}
        for p in mp3s:
            try:
                audio = ID3(p)
            except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
                continue
            for field, src, sort_tag in _SORT_SRC:
                if field not in chosen:
                    continue
                fr = audio.get(src)
                # A source frame can hold several values — TCOM takes one
                # composer each — while the sort frame is single, so they join on
                # the delimiter and the engine reads them straight back as a list.
                vals = [str(t).strip() for t in fr.text if str(t).strip()] if (
                    fr and getattr(fr, 'text', None)) else []
                raw = delim.join(vals)
                if not raw:
                    continue
                # A value that sorts as itself is still worth carrying when it
                # could be split more than one way: that judgement is the
                # verification step's to confirm, and "Blank & Jones" needing no
                # tag is exactly the sort of guess someone may want to overrule.
                if not _sort_value(sort_tag, raw) and not (
                        sort_tag in nb._NAME_SORT_TAGS and len(nb.split_options(raw)) > 1):
                    continue
                ex = audio.get(sort_tag)
                if not overwrite and ex is not None and getattr(ex, 'text', None) \
                        and str(ex.text[0]).strip():
                    continue
                entries.setdefault((sort_tag, raw), []).append(p)
        return entries

    def _ask_fields() -> bool:
        """Which sort frames to generate."""
        nonlocal chosen
        picked = prompt.select(
            "Sort tags to generate:", multi=True, header=header(),
            choices=[prompt.Choice(title=label, value=f, checked=f in chosen)
                     for f, label in _FIELDS])
        if not picked:
            return False                   # first screen: back leaves the op
        chosen = set(picked)
        return True

    def _ask_mode() -> bool:
        """Fill blanks, or overwrite what is already there."""
        nonlocal mode
        picked = prompt.select("When a sort tag already has a value:", choices=_MODES,
                               index=_MODES.index(mode), header=header())
        if not picked:
            return False
        mode = picked
        return True

    def _read_files():
        """Not a screen: re-read the files when an answer above has changed.

        Transparent to the walk in both directions — it steps aside once the plan
        matches the answers — but reports a back when the selection turns up
        nothing, so the walk lands on the question worth changing.
        """
        nonlocal plan, scanned_for
        overwrite = (mode == "Overwrite existing")
        if scanned_for == (frozenset(chosen), overwrite):
            return _SKIP
        entries = _scan(overwrite)
        if not entries:
            ui_utils.show_status("No sort orders to write — already set, or none needed.")
            return False
        # Carry every decision across the rescan: they are keyed by person and by
        # value, so they outlive the entries they were made against.
        fresh = _SortPlan(entries, delim)
        if plan is not None:
            fresh.chosen.update(plan.chosen)
            fresh.splits.update(plan.splits)
            fresh.custom.update(plan.custom)
        plan = fresh
        scanned_for = (frozenset(chosen), overwrite)
        return _SKIP

    def _ask_verify():
        """Confirm who the people are — skipped when nothing reads two ways."""
        assert plan is not None
        if not _split_keys(plan):
            return _SKIP
        return _verify_splits(plan, header, note)

    def _ask_review() -> bool:
        """The flat list of individuals, and what gets written."""
        nonlocal checked
        assert plan is not None
        got = _review_sort_people(plan, header, note)
        if got is None:
            return False
        checked = got
        return True

    if not _walk([_ask_fields, _ask_mode, _read_files, _ask_verify, _ask_review]):
        return

    if not checked:
        ui_utils.show_status("Nothing selected.")
        return
    assert plan is not None

    # Rows are people; values are what gets written. A value goes out when
    # everyone in it is checked — unchecking one person leaves every value they
    # appear in untouched rather than half-sorted.
    per_path: dict = {}
    for key in plan.entries:
        sort_tag, raw = key
        people = plan.people(sort_tag, raw)
        if people:
            if any(('person', person) not in checked for person in people):
                continue
        elif ('album', key) not in checked:
            continue
        if not plan.writes(sort_tag, raw):
            continue
        for p in plan.entries[key]:
            per_path.setdefault(p, []).append((sort_tag, plan.value(sort_tag, raw)))

    count = errors = 0
    for p, writes in per_path.items():
        try:
            audio = ID3(p)
            changed = False
            for sort_tag, sv in writes:
                audio.delall(sort_tag)
                frame = create_frame(sort_tag, sv)
                if frame is not None:
                    audio.add(frame)
                    changed = True
            if changed:
                save_id3(audio, p)
                count += 1
                try:
                    refresh_library_entry(library, p)
                except Exception:
                    pass
        except Exception:
            errors += 1

    msg = f"Wrote sort orders for {count} file(s)."
    if skipped_fmt:
        msg += f" {skipped_fmt} non-MP3 skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def assign_by_pattern(paths: list, library: list, header) -> None:
    """Assign one tag across an ordered selection by ranges, an every-N grouping,
    or a date schedule (#IDEA: pattern-based bulk editing). MP3/ID3 only."""
    by_path = {s['path']: s for s in library}
    # Overlay the on-disk disc/track numbers over the cached ones: the ranges and
    # the per-disc seeding are position-based, so ordering from a stale cache
    # would point every range at the wrong tracks.
    merged = []
    for p in paths:
        s = dict(by_path.get(p, {'path': p}))
        s['path'] = p
        pairs = tw.read_number_pairs(p)
        if pairs['disc']:
            s['disc'] = pairs['disc']
        if pairs['track']:
            s['track'] = pairs['track']
        merged.append(s)
    ordered = bp.order_tracks(merged)
    if not ordered:
        ui_utils.show_status("No tracks.")
        return
    n = len(ordered)

    # Every answer is kept here, so a screen reopened by walking back holds what
    # it was left holding — typed rows and schedules included.
    state: dict = {'tag': '', 'mode': None, 'rows': [], 'sched': None, 'gs': '',
                   'tmpl': '', 'mode2': "Fill blanks only", 'apply_set': None}

    def _ask_tag() -> bool:
        """Which frame to assign. A name that isn't a frame is asked again here
        rather than reported as a back, which on the first screen would end the
        operation over a typo."""
        while True:
            raw = prompt.text("Tag to assign (e.g. TSST, TIT1, TDRC):",
                              default=state['tag'])
            if not raw:
                return False
            tag_id = raw.strip().upper()
            if get_tag_info(tag_id):
                state['tag'] = tag_id
                return True
            ui_utils.show_status(f"Unknown tag: {tag_id}")
            state['tag'] = tag_id            # so the typo is there to correct

    def _modes() -> list:
        """The assignment modes this tag supports (schedules need a date frame)."""
        info = get_tag_info(state['tag'])
        modes = ["Ranges (from–to → value)", "Every N tracks → value"]
        if info and info.format_spec == 'ISO8601':
            modes += ["Date schedule", "Schedule per range (own start + interval)"]
        return modes

    def _ask_mode() -> bool:
        """Ranges, every N, or a date schedule."""
        modes = _modes()
        idx = modes.index(state['mode']) if state['mode'] in modes else 0
        picked = prompt.select(
            "Assignment mode:", choices=modes, index=idx,
            header=header(f"{state['tag']} · {n} tracks in disc/track order"))
        if not picked:
            return False
        state['mode'] = picked
        return True

    def _int(s, what):
        """Parse s as an int, or show a status error naming `what` and return None."""
        try:
            return int(str(s).strip())
        except (TypeError, ValueError):
            ui_utils.show_status(f"{what} must be a number.")
            return None

    def _ask_spec() -> bool:
        """Ask whatever the chosen mode needs, and build the assignments.

        One step rather than one per question: these sub-flows loop and branch
        (a time per range, a group size only for "per group"), and threading a
        walk through them would tangle more than it helps. A back inside returns
        to the mode question, with typed rows kept for the next attempt.
        """
        tag_id = state['tag']
        mode = state['mode']
        assignments: dict = {}
        if mode.startswith("Ranges"):
            rows = prompt.list_edit(f"Ranges for {tag_id} (positions 1–{n}; range no. as "
                                    f"{{n}} 3 / {{r}} III / {{en}} Three):",
                                    state['rows'], ("FROM", "TO", "VALUE"))
            if not rows:
                return False
            state['rows'] = rows              # kept, in case this is walked back to
            ranges = []
            for r in rows:
                cells = list(r) if isinstance(r, (list, tuple)) else [r]
                if len(cells) < 3:
                    continue
                lo, hi = _int(cells[0], "FROM"), _int(cells[1], "TO")
                if lo is None or hi is None:
                    return False
                ranges.append((lo, hi, str(cells[2]).strip()))
            assignments = bp.assign_ranges(ordered, ranges)
        elif mode.startswith("Every"):
            gs = _int(prompt.text("Group size (N tracks per group):",
                                  default=state['gs']), "Group size")
            if gs is None:
                return False
            state['gs'] = str(gs)
            tmpl = prompt.text("Value — group number as {n} 3, {r} III or {en} Three "
                               "(e.g. Series {n}, Act {r}, Series {en}):",
                               default=state['tmpl'])
            if tmpl is None:
                return False
            state['tmpl'] = tmpl
            assignments = bp.assign_periodic(ordered, gs, tmpl)
        elif mode.startswith("Schedule per range"):
            # Seeded with one row per disc — the common case is "each disc/series has
            # its own start date and cadence", so the positions are filled in from
            # the real disc boundaries and only START/EVERY need typing.
            runs = bp.disc_ranges(ordered)
            seeded = [[str(lo), str(hi), '', '7', 'track'] for lo, hi, _ in runs]

            labels = [lab for _, _, lab in runs]
            shown = ', '.join(labels[:6]) + ('…' if len(labels) > 6 else '')

            def _sched_hints(col: int, row: list) -> list:
                """Barrel-pickable values for the STEP column."""
                return ['track', 'disc'] if col == 4 else []

            rows = prompt.list_edit(
                f"Per-range {tag_id} schedule — a row per disc ({shown}); "
                f"type digits into START, EVERY = days, STEP = track/disc:",
                state['sched'] or seeded, ("FROM", "TO", "START", "EVERY", "STEP"),
                col_hints=_sched_hints,
                # START edits as a split date/time field and is given the width to
                # show a whole stamp; the short numeric columns give way to it.
                col_types={2: 'timestamp'},
                col_mins=_SCHEDULE_COL_MINS,
                col_ratios=_SCHEDULE_COL_RATIOS)
            if not rows:
                return False
            state['sched'] = rows             # kept, in case this is walked back to

            # Every row is checked before anything is written, and a bad one is named
            # rather than quietly dropping out of the result.
            specs, row_errors = bp.validate_schedule_rows(rows, n)
            if row_errors:
                ui_utils.show_status("  ·  ".join(row_errors[:3])
                                     + (f"  (+{len(row_errors) - 3} more)"
                                        if len(row_errors) > 3 else ""), duration=5.0)
                if not specs:
                    return False
                if not prompt.confirm(f"{len(row_errors)} row(s) unusable — "
                                      f"apply the other {len(specs)}?"):
                    return False
            if not specs:
                ui_utils.show_status("No usable schedule rows.")
                return False

            # A time typed into START is kept per range. Only when no row carried one
            # is a time worth asking about — and "No time" sits first, so Enter
            # accepts the plain dates most archives want.
            if not any(spec[3] for spec in specs):
                tchoices = ["No time", "Same time for all"]
                if 1 < len(specs) <= 12:
                    tchoices.append("Per range")
                tsel = prompt.select("Time of day:", choices=tchoices, header=header())
                if not tsel:
                    return False
                if tsel == "Same time for all":
                    tval = bp.norm_time(prompt.text("Time (HH:MM, 24-hour):"))
                    if not tval:
                        ui_utils.show_status("Not a valid 24-hour time.")
                        return False
                    specs = [(lo, hi, st, tval, ev, sp) for lo, hi, st, _, ev, sp in specs]
                elif tsel == "Per range":
                    filled = []
                    for lo, hi, st, _, ev, sp in specs:
                        raw = prompt.text(f"Time for positions {lo}-{hi} "
                                          f"(HH:MM, blank = none):")
                        if raw is None:
                            return False
                        filled.append((lo, hi, st, bp.norm_time(raw), ev, sp))
                    specs = filled

            assignments = bp.assign_range_schedules(ordered, specs)
        else:                                            # Date schedule (ISO8601 tags)
            start = prompt.calendar_select("Start date:")
            if not start:
                return False
            iv = _int(prompt.text("Interval in days (7 = weekly):", default="7"), "Interval")
            if iv is None:
                return False
            gsel = prompt.select("Step the date:",
                                 choices=["Per track", "Per disc", "Per group of N"])
            if not gsel:
                return False
            gran, gsize = 'track', 1
            if gsel.startswith("Per disc"):
                gran = 'disc'
            elif gsel.startswith("Per group"):
                gsize = _int(prompt.text("Group size (N):"), "Group size")
                if gsize is None:
                    return False
                gran = 'group'

            # Optional time of day → full ISO timestamps. Per-group times (e.g. each
            # series at a different time) are offered when the groups are few.
            times = None
            tmode_choices = ["No time", "Same time for all"]
            groups = bp.date_groups(ordered, gran, gsize)
            if gran != 'track' and 1 < len(groups) <= 12:
                tmode_choices.append("Per group")
            tmode = prompt.select("Time of day:", choices=tmode_choices, header=header())
            if not tmode:
                return False
            if tmode == "Same time for all":
                times = prompt.text("Time (HH:MM, 24-hour):")
                if not times:
                    return False
            elif tmode == "Per group":
                times = {}
                for g in groups:
                    t = prompt.text(f"Time for group {g} (HH:MM, blank = none):")
                    if t:
                        times[g] = t
            assignments = bp.assign_dates(ordered, start, iv, gran, gsize, times=times)

        assignments = {p: v for p, v in assignments.items() if v}
        if not assignments:
            ui_utils.show_status("Nothing to assign — check the ranges/positions.")
            return False
        state['assignments'] = assignments
        return True

    _MODE2 = ["Fill blanks only", "Overwrite existing"]

    def _ask_mode2() -> bool:
        """Fill blanks, or overwrite what is already there."""
        picked = prompt.select("When the tag already has a value:", choices=_MODE2,
                               index=_MODE2.index(state['mode2']), header=header())
        if not picked:
            return False
        state['mode2'] = picked
        return True

    def _ask_preview() -> bool:
        """Show the value each track would get, and take the selection."""
        tag_id, assignments = state['tag'], state['assignments']
        pos = {s['path']: i + 1 for i, s in enumerate(ordered)}
        targets = [s for s in ordered if s['path'] in assignments]
        n_mp4 = sum(1 for s in targets if not s['path'].lower().endswith('.mp3'))
        keep = state['apply_set']
        choices = [
            prompt.Choice(title=os.path.basename(s['path']), value=s['path'],
                          checked=keep is None or s['path'] in keep,
                          cells=[str(pos[s['path']]), os.path.basename(s['path']),
                                 assignments[s['path']]])
            for s in targets if s['path'].lower().endswith('.mp3')
        ]
        if not choices:
            ui_utils.show_status("No MP3s to assign (this operation is MP3-only).")
            return False
        sub = f"{tag_id} · {ui_utils.plural(len(choices), 'file')}" + (
            f" · {n_mp4} non-MP3 skipped" if n_mp4 else "")
        sel = prompt.select("Preview — ↵ applies:", choices=choices,
                            columns=_PATTERN_COLUMNS, header=header(sub), multi=True)
        if sel is None:
            return False
        state['targets'] = targets
        state['apply_set'] = set(sel)
        return True

    if not _walk([_ask_tag, _ask_mode, _ask_spec, _ask_mode2, _ask_preview]):
        return

    tag_id = state['tag']
    assignments = state['assignments']
    targets = state['targets']
    apply_set = state['apply_set']
    overwrite = (state['mode2'] == "Overwrite existing")
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    count = errors = 0
    for s in targets:
        p = s['path']
        if p not in apply_set:
            continue
        val = assignments[p]
        try:
            try:
                audio = ID3(p)
            except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
                audio = ID3()
            if not overwrite:
                fr = audio.get(tag_id)
                if fr is not None and getattr(fr, 'text', None) and str(fr.text[0]).strip():
                    continue
            audio.delall(tag_id)
            frame = create_frame(tag_id, val)
            if frame is None:
                continue
            audio.add(frame)
            save_id3(audio, p)
            count += 1
            try:
                refresh_library_entry(library, p)
            except Exception:
                pass
        except Exception:
            errors += 1

    msg = f"Assigned {tag_id} to {count} file(s)."
    if n_mp4:
        msg += f" {n_mp4} non-MP3 skipped."
    if errors:
        msg += f" {errors} error(s)."
    ui_utils.show_status(msg)


def _operation_verb(operation: str) -> str:
    """The verb of a bulk operation name, without the object it acts on:
    ``"Delete Tags"`` -> ``"delete"``, ``"Set Common Value"`` -> ``"set common value"``.
    """
    return re.sub(r'\s+tags?$', '', operation, flags=re.I).lower()


def bulk_id3_manager(library: list, album_name: str | None = None, paths: list | None = None) -> None:
    """
    Bulk tag operations across a set of tracks.

    Pass either album_name (looks up from library) or paths directly.
    Library is updated in-place and cache saved after changes.
    """
    if paths is not None:
        album_tracks = paths
    elif album_name is not None:
        album_tracks = [s['path'] for s in library if s['album'] == album_name]
    else:
        return

    if not album_tracks:
        ui_utils.show_status("No tracks found.")
        return

    cols = get_terminal_width()

    ui_utils.show_status(f"Scanning {len(album_tracks)} tracks…")
    all_tag_counts: Counter = Counter()
    tag_values: dict = {}
    # The first real frame seen for each tag.  `tag_values` holds *display*
    # summaries (newlines flattened to '\\', multi-values joined) which are fine
    # on screen but lossy — editing must start from the frame itself.
    tag_first_frame: dict = {}

    for path in album_tracks:
        # The ID3-based operations below are MP3-only; non-MP3s are skipped
        # quietly rather than raising (use "Derive from filename" for
        # cross-format writes).
        if not path.lower().endswith('.mp3'):
            continue
        try:
            audio = ID3(path)
        except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
            continue                          # untagged MP3 — simply has no tags yet
        except (OSError, IOError):
            continue
        try:
            all_tag_counts.update(audio.keys())
            for k in audio.keys():
                raw = audio[k]
                if k.startswith(('APIC', 'SYLT')):
                    val = k
                elif k.startswith(('TMCL', 'TIPL')):
                    val = f"{len(raw.people)} people"
                elif hasattr(raw, 'adjustments'):
                    n = len(raw.adjustments)
                    val = f"{n} band{'s' if n != 1 else ''}"
                elif hasattr(raw, 'gain') and hasattr(raw, 'channel'):
                    val = f"{raw.gain:+g} dB"
                elif hasattr(raw, 'text'):
                    # Rendered for the screen (values are stored with ';').
                    # This used to concatenate with no separator at all, so a
                    # two-genre frame summarised as "PopRock".
                    full_text = format_value_list(list(raw.text))
                    lines = [line for line in full_text.replace("\r\n", "\n").split("\n")]
                    val = "\\".join(lines)
                else:
                    val = str(raw)
                tag_values.setdefault(k, []).append(val)
                tag_first_frame.setdefault(k, raw)
        except Exception as e:
            ui_utils.show_status(f"Error scanning {os.path.basename(path)}: {e}")
            continue

    def _bulk_header(subtitle: str | None = None):
        """Rounded, full-width box header: bold title left, track count right,
        optional dim subtitle line. Returns a builder for select()/checkbox()."""
        def _build():
            """Render the boxed header lines for the current terminal width."""
            cols_now = get_terminal_width()
            mh = ui_utils.MARGIN_H
            # Reserve mh on BOTH sides (box border = 1+inner+2+1); the ' '*mh
            # prefix is the left margin, so subtract 2*mh for an even right one.
            inner = max(12, cols_now - 2 * mh - 4)
            title = "Bulk Edit"
            count = f"{len(album_tracks)} track" + ("" if len(album_tracks) == 1 else "s")
            gap = max(2, inner - len(title) - len(count))
            title_line = f"{C.BOLD}{title}{C.RESET}{' ' * gap}{C.DIM}{count}{C.RESET}"

            lines = [
                f"{' ' * mh}{C.DIM}╭{'─' * (inner + 2)}╮{C.RESET}",
                f"{' ' * mh}{C.DIM}│{C.RESET} {title_line} {C.DIM}│{C.RESET}",
            ]
            if subtitle:
                sub = subtitle if len(subtitle) <= inner else subtitle[:inner - 1] + "…"
                lines.append(f"{' ' * mh}{C.DIM}│{C.RESET} {C.DIM}{sub:<{inner}}{C.RESET} {C.DIM}│{C.RESET}")
            lines.append(f"{' ' * mh}{C.DIM}╰{'─' * (inner + 2)}╯{C.RESET}")
            lines.append("")
            return lines
        return _build

    # Main screen: the basic per-tag ops, plus one entry into the automation
    # submenu (derive/pattern/propagate ops that compute or copy values rather
    # than setting them directly).
    while True:
        operation = prompt.select(
            "Operation:",
            choices=[
                "Add new tag",
                "Set value",
                "Rename tags",
                "Delete tags",
                prompt.separator(),
                "Automation…",
            ],
            header=_bulk_header()
        )
        if not operation:
            return
        if operation != "Automation…":
            break
        operation = prompt.select(
            "Automation:",
            choices=[
                "Derive from filename",
                "Rename files from tags",
                "Set album art from files",
                "Assign by range / schedule",
                "Apply sort orders",
                "Renumber tracks (disc ↔ continuous)",
                "Reflow disc numbering",
                "Remove single-disc numbering",
                "Set picture type",
                "Copy from first track",
            ],
            header=_bulk_header()
        )
        if operation:
            break
        # Backed out of the submenu — fall through to re-show the main menu.

    op_display = operation.lower()
    op_map = {
        "Derive from filename": "Derive From Filename",
        "Rename files from tags": "Rename Files",
        "Set album art from files": "Set Album Art",
        "Assign by range / schedule": "Assign By Pattern",
        "Apply sort orders": "Apply Sort Orders",
        "Renumber tracks (disc ↔ continuous)": "Renumber Tracks",
        "Reflow disc numbering": "Reflow Discs",
        "Remove single-disc numbering": "Strip Single Disc",
        "Set picture type": "Set Picture Type",
        "Set value": "Set Common Value",
        "Copy from first track": "Copy From First Track",
        "Delete tags": "Delete Tags",
        "Rename tags": "Rename Tags",
        "Add new tag": "Add New Tag",
    }
    operation = str(op_map.get(operation, operation))

    # These manage their own preview/confirm/apply flow.
    if operation == "Derive From Filename":
        derive_from_filename(album_tracks, library, _bulk_header)
        return
    if operation == "Rename Files":
        rename_files_op(album_tracks, library, _bulk_header)
        return
    if operation == "Set Album Art":
        set_album_art_op(album_tracks, library, _bulk_header)
        return
    if operation == "Assign By Pattern":
        assign_by_pattern(album_tracks, library, _bulk_header)
        return
    if operation == "Apply Sort Orders":
        apply_sort_orders(album_tracks, library, _bulk_header)
        return
    if operation == "Renumber Tracks":
        renumber_tracks_op(album_tracks, library, _bulk_header)
        return
    if operation == "Set Picture Type":
        set_picture_type_op(album_tracks, library, _bulk_header)
        return
    if operation == "Strip Single Disc":
        strip_single_disc_op(album_tracks, library, _bulk_header)
        return
    if operation == "Reflow Discs":
        reflow_discs_op(album_tracks, library, _bulk_header)
        return

    if not all_tag_counts and operation not in ("Add New Tag",):
        ui_utils.show_status("No tags found.")
        return

    # Friendly-name column: modest, bounded width (truncates long names with an
    # ellipsis inside the brackets). Value column: bounded so the whole row fits
    # the terminal — otherwise the line overflows and the label's closing bracket
    # gets clipped by the fallback truncation.
    alias_budget = max(20, min(32, cols - 48))
    VAL_MAX = max(10, cols - alias_budget - 33)

    def _b_alias(tag):
        """Friendly name for a tag id, or '' if unknown."""
        info = get_tag_info(tag)
        return info.name[0] if info else ""

    def _value_summary(tag) -> str:
        """Summarize a tag's values across the selection: single value, "{n values}", or a type label."""
        if tag.startswith('APIC'):
            return "‹image›"
        if tag.startswith(('TMCL', 'TIPL')):
            vals = tag_values.get(tag, [])
            unique = set(vals)
            return f"‹{vals[0]}›" if len(unique) == 1 else "‹varies›"
        if tag.startswith('SYLT'):
            return "‹synced lyrics›"

        vals = tag_values.get(tag, [])
        if not vals:
            return ""
        unique = set(vals)
        if len(unique) == 1:
            v = vals[0]
            return v if len(v) <= VAL_MAX else v[:VAL_MAX - 1] + "…"

        n_vary = len(unique)
        return f"{{{n_vary} values}}"

    def _tag_option_cells(tag, count):
        """Build the [id+name, category, value summary, count] row cells for a tag option."""
        # Column 1 = TAG (bright) + friendly name (dim) as two segments.
        alias = _b_alias(tag)
        friendly = f" ({alias})" if alias else ""
        category = get_tag_category(tag).lower()
        val_disp = _value_summary(tag)
        return [
            [(display_tag_id(tag), 'primary'), (friendly, 'dynamic-dim')],
            category,
            val_disp,
            f"{count}/{len(album_tracks)}",
        ]

    selected_tags = []
    target_tag_id = None
    target_val = None
    set_spec = None   # regex spec for "Set Common Value" (per-file value computation)

    if operation == "Add New Tag":
        raw_id = prompt.text("New Tag ID (e.g. TSO2, COMM[eng], TXXX:Mood):")
        if not raw_id:
            return
        # Upper-case the base, preserving any :desc:lang or [lang] suffix.
        _parts = raw_id.split(':')
        target_tag_id = _parts[0].upper() + ((":" + ":".join(_parts[1:])) if len(_parts) > 1 else "")
        base_id, _, _ = parse_composite_tag_id(target_tag_id)
        # Reuse the same type-aware value prompt as single-track editing so the
        # right widget (date picker, fraction editor, etc.) is used in bulk too.
        if get_tag_info(base_id):
            target_val = prompt_for_value(target_tag_id)
        else:
            target_val = prompt.text(f"Value for {target_tag_id}:")
        if target_val is None:
            return
    else:
        tag_options = [prompt.Choice(title=t, value=t, cells=_tag_option_cells(t, c))
                       for t, c in sorted(all_tag_counts.items())]

        callback = None if operation == "Delete Tags" else get_tag_category

        selected_tags = prompt.select(
            # The operation names already end in their object ("Delete Tags",
            # "Rename Tags"), and this prompt supplies its own — so use the verb
            # alone, or the line reads "Select tags to delete tags:".
            message=f"Select tags to {_operation_verb(operation)}:",
            choices=tag_options,
            interlock_category_callback=callback,
            header=_bulk_header(),
            columns=_BULK_COLUMNS,
            multi=True,
        )

        if not selected_tags:
            return

        if operation == "Rename Tags":
            target_val = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
            if target_val:
                target_val = target_val.upper()
        elif operation == "Set Common Value":
            # People lists (TMCL/TIPL) get the common-entry editor (edit/add/remove
            # across files by coverage) instead of a blind whole-list replace.
            people_sel = [t for t in selected_tags
                          if getattr(get_tag_info(t), 'ui_category', None) == 'people']
            if people_sel:
                for pt in people_sel:
                    bulk_people_editor(album_tracks, pt, library, _bulk_header)
                selected_tags = [t for t in selected_tags if t not in people_sel]
                if not selected_tags:
                    return
            # Fraction pairs (TRCK/TPOS/MVIN) get their own editor too: it greys
            # out whichever half differs between the files instead of writing one
            # file's whole 'n/N' over the selection.
            frac_sel = [t for t in selected_tags
                        if getattr(get_tag_info(t), 'format_spec', None) == 'FRACTIONAL']
            if frac_sel:
                for ft in frac_sel:
                    bulk_fraction_editor(album_tracks, ft, library, _bulk_header)
                selected_tags = [t for t in selected_tags if t not in frac_sel]
                if not selected_tags:
                    return
            # Album art has its own picker and preview further down.  Routing it
            # through the text-value prompt as well asked for the image, its
            # picture type and its description a *second* time — the worst of the
            # screen bloat here.  Art-only selections skip straight to that block.
            value_sel = [t for t in selected_tags if not t.startswith('APIC')]
            if not value_sel:
                target_val = "_album_art_"   # sentinel: the art block does the work
            else:
                first_tag = value_sel[0]
                # Seed the editor from the frame itself.  Seeding it from
                # `tag_values` fed the '\\'-joined display summary back in and saved
                # it verbatim, so every bulk pass over a lyric frame replaced its
                # newlines with literal backslashes.
                existing_vals = tag_values.get(first_tag, [])
                fallback_val = tag_first_frame.get(first_tag)
                if fallback_val is None:
                    fallback_val = existing_vals[0] if existing_vals else ""

                source = prompt.select(
                    "Value source:",
                    choices=["Enter a value", "Find & replace (regex)",
                             "From file name / folder (regex)"],
                    header=_bulk_header())
                if not source:
                    return

                if source == "Enter a value":
                    target_val = prompt_for_value(first_tag, current_value=fallback_val)
                elif source == "Find & replace (regex)":
                    pat = prompt.text("Find (regex) — applied to each existing value:")
                    if not pat:
                        return
                    try:
                        rx = re.compile(pat)
                    except re.error as e:
                        ui_utils.show_status(f"Invalid regex: {e}")
                        return
                    repl = prompt.text(r"Replace with (\1 / \g<name> for capture groups):",
                                       default="")
                    if repl is None:
                        return
                    set_spec = {'mode': 'replace', 'rx': rx, 'repl': repl}
                    target_val = "_regex_"   # sentinel so the None-guard below doesn't bail
                else:  # From file name / folder (regex)
                    against = prompt.select("Match regex against:",
                                            choices=["File name", "Folder path"],
                                            header=_bulk_header())
                    if not against:
                        return
                    base = _derive_regex_base(album_tracks) if against == "Folder path" else None
                    sample = fp._regex_target(album_tracks[0], base) if album_tracks else ''
                    pat = prompt.text(
                        rf"Regex with capture groups (matches e.g. '{sample}'; "
                        "use / between folders):")
                    if not pat:
                        return
                    try:
                        rx = re.compile(pat)
                    except re.error as e:
                        ui_utils.show_status(f"Invalid regex: {e}")
                        return
                    tmpl = prompt.text(r"Value template (\1, \2 or \g<name> for groups):")
                    if not tmpl:
                        return
                    set_spec = {'mode': 'filename', 'rx': rx, 'tmpl': tmpl, 'base': base}
                    target_val = "_regex_"
        elif operation == "Copy From First Track":
            # Values come directly from the first track; no extra prompt needed.
            target_val = "_copy_from_first_"

    if target_val is None and operation not in ["Delete Tags"]:
        return

    apic_tags = [t for t in selected_tags if t.startswith('APIC')]
    non_apic_tags = [t for t in selected_tags if not t.startswith('APIC')]
    new_apic_frame = None
    new_apic_desc = None

    # Album art is its own little pipeline: pick the image from the ranked
    # candidates beside the tracks (never a hand-typed path), settle its type and
    # description on one screen, and preview per file before writing.
    new_apic_type = None
    if apic_tags and operation != "Delete Tags":
        apic_action = prompt.select(
            f"Album art — {len(apic_tags)} frame(s) selected:",
            choices=["Replace image", "Edit description", "Edit picture type",
                     "Skip album art"],
            header=_bulk_header())

        if apic_action == "Replace image":
            picked = pick_nearby_cover(
                album_tracks[0], header=_bulk_header,
                title="Cover to embed on every ticked track — best guess first:")
            read = cm.read_image(picked) if isinstance(picked, str) else None
            if not read:
                if isinstance(picked, str):
                    ui_utils.show_status("Could not read that image.")
                apic_tags = []
            else:
                img_data, mime = read
                meta = _prompt_for_image_metadata(header=_bulk_header)
                if meta is None:
                    apic_tags = []          # cancelled — don't write anything
                else:
                    pic_type, desc = meta
                    new_apic_frame = APIC(encoding=3, mime=mime, type=pic_type,
                                          desc=desc, data=img_data)
        elif apic_action == "Edit description":
            new_apic_desc = prompt.text("New description for all album-art frames:")
            if new_apic_desc is None:
                apic_tags = []
        elif apic_action == "Edit picture type":
            # Type only — the old path asked for a description here too and then
            # threw it away.
            new_apic_type = _prompt_for_picture_type(header=_bulk_header)
            if new_apic_type is None:
                apic_tags = []              # cancelled — was a silent no-op before
        else:
            apic_tags = []

    # Backing out of the art screens with nothing else selected ends the run.
    # Falling through asked "Apply set value to N tracks?" and then reported
    # "processed 0 files" — two screens of noise after an explicit cancel.
    # ("Add new tag" never selects existing tags, so it is exempt.)
    if operation != "Add New Tag" and not apic_tags and not non_apic_tags:
        ui_utils.show_status("Cancelled")
        return

    # Album art gets a preview with per-row ticks, like every other bulk op —
    # the old path went straight from a hand-typed path to a bare yes/no.  Enter
    # on the preview IS the confirmation, so it replaces the confirm entirely.
    apic_apply: set = set(album_tracks)
    if apic_tags:
        mp3s = [p for p in album_tracks if p.lower().endswith('.mp3')]
        n_other = len(album_tracks) - len(mp3s)
        if not mp3s:
            ui_utils.show_status("No MP3s here — album-art frames are ID3-only.")
            return

        had_art = {p: tw.has_cover(p) for p in mp3s}

        def _art_action(p: str):
            """What this track's art will become, as preview cells."""
            if operation == "Delete Tags":
                return ("remove art", 'has art' if had_art[p] else 'none')
            if new_apic_frame is not None:
                return ("embed cover", 'replaces art' if had_art[p] else 'adds art')
            if new_apic_desc is not None:
                return (f"description → {new_apic_desc or '(blank)'}",
                        '' if had_art[p] else 'no art')
            if new_apic_type is not None:
                label = dict(_PICTURE_TYPES).get(new_apic_type, str(new_apic_type))
                return (f"picture type → {label}", '' if had_art[p] else 'no art')
            return ("no change", '')

        art_choices = []
        for p in mp3s:
            what, note = _art_action(p)
            # Only a replace can act on a track with no art yet; the description
            # and type edits need an existing frame to edit.
            actionable = (new_apic_frame is not None or operation == "Delete Tags"
                          or had_art[p])
            art_choices.append(prompt.Choice(
                title=os.path.basename(p), value=p, checked=actionable,
                cells=[os.path.basename(p), what, note]))

        def _art_header():
            """Live counts for the album-art preview."""
            nk = sum(1 for ch in art_choices if ch.checked)
            bits = [ui_utils.plural(len(mp3s), "MP3"), f"{nk} ticked"]
            n_blank = sum(1 for p in mp3s if not had_art[p])
            if n_blank:
                bits.append(f"{n_blank} without art")
            if n_other:
                bits.append(f"{n_other} non-MP3 skipped")
            return _bulk_header(" · ".join(bits))()

        picked_rows = prompt.select(
            "Preview — ↵ applies:",
            choices=art_choices, columns=_COVER_PREVIEW_COLUMNS,
            header=_art_header, multi=True)
        if picked_rows is None:
            return
        apic_apply = set(picked_rows)
        if not apic_apply and not non_apic_tags:
            ui_utils.show_status("No tracks selected.")
            return
    elif not prompt.confirm(f"Apply {op_display} to {len(album_tracks)} tracks?"):
        return

    # For copy-from-first, read source frames once from the first track.
    copy_source_frames: dict = {}
    if operation == "Copy From First Track" and album_tracks:
        try:
            _src = ID3(album_tracks[0])
            for tag in selected_tags:
                if tag in _src:
                    copy_source_frames[tag] = _src[tag]
        except Exception as e:
            ui_utils.show_status(f"Could not read source track: {e}")
            return

    count_modified = 0
    count_other_fmt = 0
    for path in album_tracks:
        # These operations are ID3/MP3-only; skip other formats without erroring.
        if not path.lower().endswith('.mp3'):
            count_other_fmt += 1
            continue
        try:
            try:
                audio = ID3(path)
            except mutagen.id3.ID3NoHeaderError:  # type: ignore[reportPrivateImportUsage]
                # Untagged MP3: only "Add New Tag" can create tags from nothing;
                # for the other operations there is nothing to change.
                if operation != "Add New Tag":
                    continue
                audio = ID3()
            changed = False

            if operation == "Add New Tag":
                if target_tag_id is None:
                    continue
                if target_val is None:
                    continue
                new_frame = create_frame(target_tag_id, target_val)
                if new_frame:
                    audio.add(new_frame)
                    changed = True

            if apic_tags and path in apic_apply:
                if new_apic_frame is not None:
                    # A replace clears every selected art frame and writes the new
                    # one ONCE.  The old loop added the same frame per selected
                    # key — and they share the `APIC:desc` hash key, so two
                    # selected frames silently collapsed into one.  It also only
                    # acted `if tag in audio`, so tracks with no art yet — the
                    # ones most in need of a cover — were skipped in silence.
                    for tag in apic_tags:
                        audio.delall(tag)
                    audio.add(new_apic_frame)
                    changed = True
                else:
                    # Description / type edits need an existing frame to edit.
                    for tag in apic_tags:
                        if tag not in audio:
                            continue
                        if operation == "Delete Tags":
                            audio.pop(tag)
                            changed = True
                        elif new_apic_desc is not None:
                            # `desc` is part of the hash key, so re-key the frame
                            # rather than mutating it in place under a stale key.
                            frame = audio.pop(tag)
                            frame.desc = new_apic_desc
                            audio.add(frame)
                            changed = True
                        elif new_apic_type is not None:
                            audio[tag].type = new_apic_type
                            changed = True

            for tag in non_apic_tags:
                if operation == "Copy From First Track":
                    src_frame = copy_source_frames.get(tag)
                    if src_frame is not None and path != album_tracks[0]:
                        import copy as _copy
                        audio.delall(tag)
                        audio.add(_copy.deepcopy(src_frame))
                        changed = True
                    continue
                if tag in audio:
                    if operation == "Delete Tags":
                        audio.pop(tag)
                        changed = True
                    elif operation == "Rename Tags":
                        old_frame = audio.pop(tag)
                        if target_val is None:
                            continue
                        if rename_frame(audio, old_frame, target_val):
                            changed = True
                        else:
                            audio.add(old_frame)
                    elif operation == "Set Common Value":
                        # Literal value, or a per-file value from the regex spec.
                        if set_spec is None:
                            new_val = target_val
                        else:
                            new_val = _compute_set_value(set_spec, audio.get(tag), path)
                            if new_val is None:
                                continue   # no match / not applicable — leave frame as-is
                        if new_val is None:
                            continue
                        audio.delall(tag)
                        try:
                            new_frame = create_frame(tag, new_val)
                            if new_frame:
                                audio.add(new_frame)
                                changed = True
                        except ValueError:
                            pass

            if changed:
                save_id3(audio, path)   # explicit path: works for a fresh ID3 too
                count_modified += 1
                try:
                    refresh_library_entry(library, path)
                except Exception:
                    pass
        except Exception as e:
            ui_utils.show_status(f"Could not process track {os.path.basename(path)}: {e}")

    msg = f"Successfully processed {count_modified} files."
    if count_other_fmt:
        # Previously skipped in total silence, which read as "nothing happened".
        msg += f" {count_other_fmt} non-MP3 skipped (these ops are ID3-only)."
    ui_utils.show_status(msg)
