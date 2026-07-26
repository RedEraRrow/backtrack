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
    _EXT_TO_MIME,
)
from src.id3.tag_registry import parse_composite_tag_id
from src.id3 import filename_parser as fp
from src.id3 import tag_writer as tw
from src.id3 import file_namer as fnm
from src import bulk_pattern as bp
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
    prompt.Column(style='dynamic-dim'),                                 # type / category
    prompt.Column(style='normal', flex=True),                           # value summary
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=3),  # count / total
]


def prompt_for_image_payload() -> tuple[bytes, str, int, str] | None:
    """
    Prompts the user for artwork details.
    Returns (img_data, mime_type, pic_type, description) or None if cancelled.
    """
    img_path = prompt.text("Path to image:")
    if not img_path or not os.path.isfile(img_path):
        ui_utils.show_status("File not found.")
        return None

    ext = os.path.splitext(img_path)[1].lower()
    mime = _EXT_TO_MIME.get(ext, 'image/jpeg')

    meta = _prompt_for_image_metadata()
    if meta is None:
        return None
    pic_type, desc = meta

    with open(img_path, 'rb') as f:
        return f.read(), mime, pic_type, desc


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
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=3),  # write / kept
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


def _num_pair(num, total) -> str:
    return f"{num}/{total}" if total else f"{num}"


# base field → sort frame id, used to compute sort-order strings via the #42 engine.
_SORT_BASE = [('artist', 'TSOP'), ('album_artist', 'TSO2'),
              ('album', 'TSOA'), ('title', 'TSOT')]


def _sort_value(base_id: str, raw: str) -> str | None:
    """Top smart sort-order candidate for a value, or None when none is needed.

    Reuses the #42 engine (name-inversion for artists, article-move for
    album/title). "Various Artists" sorts as itself, so no tag is generated."""
    if not raw or raw.strip().lower() in ('various artists', 'various'):
        return None
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
    prompt.Column(style='primary', flex=True, max_frac=0.45),           # role / character
    prompt.Column(style='normal', flex=True),                           # name / actor
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=3),  # N/total or state
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
        choices.append(prompt.Choice(title="＋ Add person (to all files)", value="__add__"))
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


def _derive_regex_base(paths: list) -> str:
    """Base directory for folder-path regex matching: the configured library
    root when every file is under it, otherwise the files' common ancestor."""
    abspaths = [os.path.abspath(p) for p in paths]
    try:
        from src.config import load_config
        music_dir = os.path.abspath(load_config().get('music_directory', '') or '')
    except Exception:
        music_dir = ''
    if music_dir and all(p.startswith(music_dir + os.sep) for p in abspaths):
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
    field_choices = [
        prompt.Choice(title="Title  — from file name", value="title", checked=True),
        prompt.Choice(title="Track number (+ total)", value="track"),
        prompt.Choice(title="Disc number (+ total)", value="disc"),
        prompt.Choice(title="Disc subtitle — from folder (MP3 only)", value="disc_subtitle"),
        prompt.Choice(title="Album — from folder", value="album"),
        prompt.Choice(title="Album artist — from parent folder", value="album_artist"),
        prompt.Choice(title="Track artist — from file name / folder", value="artist"),
        prompt.Choice(title="Year / date — from folder or file name", value="year"),
        prompt.Choice(title="Sort-order tags — for the fields ticked above", value="sort"),
    ]
    chosen = prompt.select("Fields to derive & write:", choices=field_choices,
                           header=header(), multi=True)
    if not chosen:
        return
    apply_fields = set(chosen)

    # 2) Conflict policy.
    mode = prompt.select("When a tag already has a value:",
                         choices=["Fill blanks only", "Overwrite existing"],
                         header=header())
    if not mode:
        return
    overwrite = (mode == "Overwrite existing")

    # 3) Auto-detect, a naming template, or a raw regex.
    template = None
    regex = None
    regex_base = None
    detect = prompt.select("Detection:",
                           choices=["Auto-detect", "Use a naming template", "Use a regex"],
                           header=header())
    if not detect:
        return
    if detect == "Use a naming template":
        raw = prompt.text("Template (e.g. %disc%-%track% %title%; tokens: %track% "
                          "%disc% %title% %artist% %albumartist% %album% %year% %date% "
                          "%season% %episode% %ignore%):")
        if not raw:
            return
        try:
            fp.compile_template(raw)
        except fp.TemplateError as e:
            ui_utils.show_status(f"Invalid template: {e}")
            return
        template = raw
    elif detect == "Use a regex":
        # A vs B: match the file name, or the path from the library root so the
        # regex can capture folder levels (Artist/Album/…).
        target = prompt.select("Match regex against:",
                               choices=["File name", "Folder path"], header=header())
        if not target:
            return
        if target == "Folder path":
            regex_base = _derive_regex_base(writable)
        sample = fp._regex_target(writable[0], regex_base)
        raw = prompt.text(
            rf"Regex, named groups (matches e.g. '{sample}'; use / between folders); "
            "groups: track disc title artist albumartist album year date season episode:")
        if not raw:
            return
        try:
            compiled = fp.compile_regex(raw)
        except fp.TemplateError as e:
            ui_utils.show_status(f"Invalid regex: {e}")
            return
        unknown = fp.unrecognised_regex_groups(compiled)
        if unknown:
            ui_utils.show_status(f"Ignoring unrecognised group(s): {', '.join(unknown)}")
        regex = raw

    # Derive + plan.
    derived = fp.derive_all(writable, template=template, regex=regex, regex_base=regex_base)
    present_cache = {p: tw.present_fields(p) for p in writable}
    plans = {p: _plan_write(derived[p], apply_fields, overwrite, present_cache[p], p)
             for p in writable}
    to_write = [p for p in writable if plans[p]]

    if not to_write:
        ui_utils.show_status("Nothing to write — selected fields are already set "
                             "(try Overwrite).")
        return

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
    header_bits = [f"{len(to_write)} file(s)"]
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
            header_bits.append(f"disc subtitle N/A on {n_mp4} MP4 file(s)")
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
            title=os.path.basename(p), value=p, checked=True, cells=cells))

    def _show_detail(path) -> None:
        _detail_view(path, derived[path], plans[path], present_cache[path],
                     apply_fields, overwrite, header)

    selected = prompt.select(
        "Preview — Space to deselect, d for details, Enter to apply:",
        choices=preview_choices, columns=prev_cols,
        header=header(sub), multi=True,
        extra_hints={'d': 'details'}, on_inspect=_show_detail)
    if selected is None:
        return
    apply_paths = set(selected)
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
_PATTERN_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4),
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='normal', max_frac=0.42),
]


# source text frame → sort frame, for the standalone "apply sort orders" op.
_SORT_SRC = [
    ('artist',       'TPE1', 'TSOP'),
    ('album_artist', 'TPE2', 'TSO2'),
    ('album',        'TALB', 'TSOA'),
    ('title',        'TIT2', 'TSOT'),
]

_SORT_APPLY_COLUMNS = [
    prompt.Column(style='primary', flex=True, max_frac=0.45),
    prompt.Column(style='dynamic-dim', flex=True),
]


_RENUMBER_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4),   # position
    prompt.Column(style='primary', flex=True),                       # file
    prompt.Column(style='dynamic-dim', align='right', pin=True, gap=3),  # old → new
]


def renumber_tracks_op(paths: list, library: list, header) -> None:
    """Renumber track numbers per-disc (disc-relative) ↔ continuous (album-
    relative / movement systems). Works for MP3 and MP4 via tag_writer."""
    by_path = {s['path']: s for s in library}
    ordered = bp.order_tracks([by_path.get(p, {'path': p}) for p in paths])
    writable = [s for s in ordered if tw.is_writable(s['path'])]
    skipped_fmt = len(ordered) - len(writable)
    if not writable:
        ui_utils.show_status("No MP3/MP4 tracks to renumber.")
        return

    mode_sel = prompt.select(
        "Renumber to:",
        choices=["Continuous (album-relative) — 1…N across all discs",
                 "Per-disc (disc-relative) — restart at 1 each disc"],
        header=header(f"{len(writable)} tracks in disc/track order"))
    if not mode_sel:
        return
    mode = 'continuous' if mode_sel.startswith("Continuous") else 'per_disc'
    plan = bp.renumber_tracks(writable, mode)        # {path: (track, total)}

    pos = {s['path']: i + 1 for i, s in enumerate(writable)}
    choices = []
    for s in writable:
        trk, total = plan[s['path']]
        old = str(s.get('track', '') or '?')
        choices.append(prompt.Choice(
            title=os.path.basename(s['path']), value=s['path'], checked=True,
            cells=[str(pos[s['path']]), os.path.basename(s['path']), f"{old} → {trk}/{total}"]))
    sub = f"{len(choices)} file(s)" + (f" · {skipped_fmt} unsupported skipped" if skipped_fmt else "")
    sel = prompt.select("Preview — Space to deselect, Enter to apply:", choices=choices,
                        columns=_RENUMBER_COLUMNS, header=header(sub), multi=True)
    if sel is None:
        return
    apply_set = set(sel)
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
# Rename preview: position · old name · new name.
_RENAME_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4),
    prompt.Column(style='dynamic-dim', flex=True),
    prompt.Column(style='primary', flex=True),
]


def _show_tokens(header) -> None:
    """Read-only reference of every %token% the pattern accepts."""
    rows: list = [prompt.Choice(title=f"%{t}%", value=t, disabled=True,
                                cells=[f"%{t}%", desc]) for t, desc in fnm.TOKENS.items()]
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

    pattern: str | None = None
    while pattern is None:
        choices: list = []
        for pat, example in fnm.PRESETS:
            tail = f"e.g. {example}" + ("   ★ suggested" if pat == default_pattern else "")
            choices.append(prompt.Choice(title=pat, value=pat, cells=[pat, tail]))
        choices.append(prompt.separator())
        choices.append(prompt.Choice(title="Custom pattern…", value="__custom__",
                                     cells=["Custom pattern…", "type your own %token% pattern"]))
        choices.append(prompt.Choice(title="Show all tokens", value="__tokens__",
                                     cells=["Show all tokens", f"{len(fnm.TOKENS)} available"]))
        default_idx = next((i for i, (pat, _) in enumerate(fnm.PRESETS)
                            if pat == default_pattern), 0)
        sub = f"{len(writable)} file(s)" + (" · artists vary → artist suggested" if vary else "")
        sel = prompt.select("File-name pattern:", choices=choices,
                            columns=_RENAME_PICK_COLUMNS, header=header(sub), index=default_idx)
        if not sel:
            return
        if sel == "__tokens__":
            _show_tokens(header)
            continue
        if sel == "__custom__":
            raw = prompt.text("Pattern (e.g. %disc%-%track% %title% — 'Show all tokens' lists them):",
                              default=default_pattern)
            if not raw:
                continue
            unk = fnm.unknown_tokens(raw)
            if unk:
                ui_utils.show_status(f"Unknown token(s) will render blank: {', '.join(unk)}")
            pattern = raw
        else:
            pattern = sel

    plan = fnm.plan_renames(writable, pattern, tokens)     # [(path, old, new)]
    changed = [(p, o, n) for (p, o, n) in plan if o != n]
    if not changed:
        ui_utils.show_status("File names already match the pattern.")
        return

    choices = [prompt.Choice(title=os.path.basename(p), value=p, checked=True,
                             cells=[str(i + 1), o, n])
               for i, (p, o, n) in enumerate(changed)]
    sub = f"{len(changed)} to rename" + (f" · {skipped_fmt} unsupported skipped" if skipped_fmt else "")
    sel = prompt.select("Preview — Space to deselect, Enter to rename:", choices=choices,
                        columns=_RENAME_COLUMNS, header=header(sub), multi=True)
    if sel is None:
        return
    apply_set = set(sel)
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
            ui_utils.show_status(f"Couldn't rename {os.path.basename(orig)}: {e}")

    # Phase 2: move each temp to its final name and keep the library in sync.
    count = 0
    for orig, tmp, final in staged:
        try:
            os.replace(tmp, final)
        except OSError as e:
            errors += 1
            ui_utils.show_status(f"Couldn't rename to {os.path.basename(final)}: {e}")
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


def apply_sort_orders(paths: list, library: list, header) -> None:
    """Generate smart sort-order tags (TSOP/TSO2/TSOA/TSOT) from each file's
    existing artist/album-artist/album/title, via the #42 engine. MP3/ID3 only."""
    field_choices = [
        prompt.Choice(title="Artist sort (TSOP)", value='artist', checked=True),
        prompt.Choice(title="Album-artist sort (TSO2)", value='album_artist', checked=True),
        prompt.Choice(title="Album sort (TSOA)", value='album', checked=True),
        prompt.Choice(title="Title sort (TSOT)", value='title'),
    ]
    chosen = prompt.select("Sort tags to generate:", choices=field_choices,
                           header=header(), multi=True)
    if not chosen:
        return
    chosen = set(chosen)
    mode = prompt.select("When a sort tag already has a value:",
                         choices=["Fill blanks only", "Overwrite existing"], header=header())
    if not mode:
        return
    overwrite = (mode == "Overwrite existing")

    mp3s = [p for p in paths if p.lower().endswith('.mp3')]
    skipped_fmt = len(paths) - len(mp3s)

    plan: dict = {}                                  # path → [(sort_tag, sort_value)]
    for p in mp3s:
        try:
            audio = ID3(p)
        except (mutagen.id3.ID3NoHeaderError, OSError):  # type: ignore[reportPrivateImportUsage]
            continue
        entries = []
        for field, src, sort_tag in _SORT_SRC:
            if field not in chosen:
                continue
            fr = audio.get(src)
            raw = str(fr.text[0]).strip() if (fr and getattr(fr, 'text', None)) else ""
            if not raw:
                continue
            sv = _sort_value(sort_tag, raw)          # top candidate, or None if none needed
            if not sv:
                continue
            ex = audio.get(sort_tag)
            if not overwrite and ex is not None and getattr(ex, 'text', None) and str(ex.text[0]).strip():
                continue
            entries.append((sort_tag, sv))
        if entries:
            plan[p] = entries

    if not plan:
        ui_utils.show_status("No sort orders to write — already set, or none needed.")
        return

    choices = []
    for p in mp3s:
        if p not in plan:
            continue
        summary = " · ".join(f"{t}={v}" for t, v in plan[p])
        choices.append(prompt.Choice(title=os.path.basename(p), value=p, checked=True,
                                     cells=[os.path.basename(p), summary]))
    sub = f"{len(choices)} file(s)" + (f" · {skipped_fmt} non-MP3 skipped" if skipped_fmt else "")
    sel = prompt.select("Preview — Space to deselect, Enter to apply:", choices=choices,
                        columns=_SORT_APPLY_COLUMNS, header=header(sub), multi=True)
    if sel is None:
        return
    apply_set = set(sel)
    if not apply_set:
        ui_utils.show_status("No files selected.")
        return

    count = errors = 0
    for p in mp3s:
        if p not in plan or p not in apply_set:
            continue
        try:
            audio = ID3(p)
            changed = False
            for sort_tag, sv in plan[p]:
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
    ordered = bp.order_tracks([by_path.get(p, {'path': p}) for p in paths])
    if not ordered:
        ui_utils.show_status("No tracks.")
        return
    n = len(ordered)

    raw = prompt.text("Tag to assign (e.g. TSST, TIT1, TDRC):")
    if not raw:
        return
    tag_id = raw.strip().upper()
    info = get_tag_info(tag_id)
    if not info:
        ui_utils.show_status(f"Unknown tag: {tag_id}")
        return
    is_date = info.format_spec == 'ISO8601'

    modes = ["Ranges (from–to → value)", "Every N tracks → value"]
    if is_date:
        modes.append("Date schedule")
    mode = prompt.select("Assignment mode:", choices=modes,
                         header=header(f"{tag_id} · {n} tracks in disc/track order"))
    if not mode:
        return

    def _int(s, what):
        try:
            return int(str(s).strip())
        except (TypeError, ValueError):
            ui_utils.show_status(f"{what} must be a number.")
            return None

    assignments: dict = {}
    if mode.startswith("Ranges"):
        rows = prompt.list_edit(f"Ranges for {tag_id} (positions 1–{n}; {{n}} = range no.):",
                                [], ("FROM", "TO", "VALUE"))
        if not rows:
            return
        ranges = []
        for r in rows:
            cells = list(r) if isinstance(r, (list, tuple)) else [r]
            if len(cells) < 3:
                continue
            lo, hi = _int(cells[0], "FROM"), _int(cells[1], "TO")
            if lo is None or hi is None:
                return
            ranges.append((lo, hi, str(cells[2]).strip()))
        assignments = bp.assign_ranges(ordered, ranges)
    elif mode.startswith("Every"):
        gs = _int(prompt.text("Group size (N tracks per group):"), "Group size")
        if gs is None:
            return
        tmpl = prompt.text("Value (use {n} for the group number, e.g. Series {n}):")
        if tmpl is None:
            return
        assignments = bp.assign_periodic(ordered, gs, tmpl)
    else:                                            # Date schedule (ISO8601 tags)
        start = prompt.calendar_select("Start date:")
        if not start:
            return
        iv = _int(prompt.text("Interval in days (7 = weekly):", default="7"), "Interval")
        if iv is None:
            return
        gsel = prompt.select("Step the date:",
                             choices=["Per track", "Per disc", "Per group of N"])
        if not gsel:
            return
        gran, gsize = 'track', 1
        if gsel.startswith("Per disc"):
            gran = 'disc'
        elif gsel.startswith("Per group"):
            gsize = _int(prompt.text("Group size (N):"), "Group size")
            if gsize is None:
                return
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
            return
        if tmode == "Same time for all":
            times = prompt.text("Time (HH:MM, 24-hour):")
            if not times:
                return
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
        return

    mode2 = prompt.select("When the tag already has a value:",
                          choices=["Fill blanks only", "Overwrite existing"], header=header())
    if not mode2:
        return
    overwrite = (mode2 == "Overwrite existing")

    pos = {s['path']: i + 1 for i, s in enumerate(ordered)}
    targets = [s for s in ordered if s['path'] in assignments]
    n_mp4 = sum(1 for s in targets if not s['path'].lower().endswith('.mp3'))
    choices = [
        prompt.Choice(title=os.path.basename(s['path']), value=s['path'], checked=True,
                      cells=[str(pos[s['path']]), os.path.basename(s['path']), assignments[s['path']]])
        for s in targets if s['path'].lower().endswith('.mp3')
    ]
    if not choices:
        ui_utils.show_status("No MP3s to assign (this operation is MP3-only).")
        return
    sub = f"{tag_id} · {len(choices)} file(s)" + (f" · {n_mp4} non-MP3 skipped" if n_mp4 else "")
    sel = prompt.select("Preview — Space to deselect, Enter to apply:", choices=choices,
                        columns=_PATTERN_COLUMNS, header=header(sub), multi=True)
    if sel is None:
        return
    apply_set = set(sel)
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
                    full_text = "".join(str(t) for t in raw.text)
                    lines = [line for line in full_text.replace("\r\n", "\n").split("\n")]
                    val = "\\".join(lines)
                else:
                    val = str(raw)
                tag_values.setdefault(k, []).append(val)
        except Exception as e:
            ui_utils.show_status(f"Error scanning {os.path.basename(path)}: {e}")
            continue

    def _bulk_header(subtitle: str | None = None):
        """Rounded, full-width box header: bold title left, track count right,
        optional dim subtitle line. Returns a builder for select()/checkbox()."""
        def _build():
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
                "Assign by range / schedule",
                "Apply sort orders",
                "Renumber tracks (disc ↔ continuous)",
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
        "Assign by range / schedule": "Assign By Pattern",
        "Apply sort orders": "Apply Sort Orders",
        "Renumber tracks (disc ↔ continuous)": "Renumber Tracks",
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
    if operation == "Assign By Pattern":
        assign_by_pattern(album_tracks, library, _bulk_header)
        return
    if operation == "Apply Sort Orders":
        apply_sort_orders(album_tracks, library, _bulk_header)
        return
    if operation == "Renumber Tracks":
        renumber_tracks_op(album_tracks, library, _bulk_header)
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
        info = get_tag_info(tag)
        return info.name[0] if info else ""

    def _value_summary(tag) -> str:
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
            message=f"Select tags to {operation.lower()}:",
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
            first_tag = selected_tags[0]
            existing_vals = tag_values.get(first_tag, [])
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

    if apic_tags and operation != "Delete Tags":
        apic_action = prompt.select(
            f"Bulk APIC action ({len(apic_tags)} tags):",
            choices=["Replace Image", "Edit Description", "Edit Picture Type", "Skip APIC"]
        )

        if apic_action == "Replace Image":
            payload = prompt_for_image_payload()
            if payload:
                img_data, mime, pic_type, desc = payload
                new_apic_frame = APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=img_data)
            else:
                apic_tags = []
        elif apic_action == "Edit Description":
            new_apic_desc = prompt.text("New description for all APIC tags:")
            if new_apic_desc is None:
                apic_tags = []
        elif apic_action == "Edit Picture Type":
            meta = _prompt_for_image_metadata()
            new_apic_frame = meta[0] if meta is not None else None  # int pic_type, checked with isinstance below
        else:
            apic_tags = []

    if not prompt.confirm(f"Apply {op_display} to {len(album_tracks)} tracks?"):
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
    for path in album_tracks:
        # These operations are ID3/MP3-only; skip other formats without erroring.
        if not path.lower().endswith('.mp3'):
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

            for tag in apic_tags:
                if tag in audio:
                    if operation == "Delete Tags":
                        audio.pop(tag)
                        changed = True
                    elif new_apic_desc is not None:
                        audio[tag].desc = new_apic_desc
                        changed = True
                    elif isinstance(new_apic_frame, int):
                        audio[tag].type = new_apic_frame
                        changed = True
                    elif isinstance(new_apic_frame, APIC):
                        audio.delall(tag)
                        audio.add(new_apic_frame)
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
            ui_utils.show_status(f"Failed to process track {os.path.basename(path)}: {e}")

    ui_utils.show_status(f"Successfully processed {count_modified} files.")


def select_files() -> list[str]:
    """Select music files for bulk editing."""
    start_path = prompt.path("Starting directory:")
    if not start_path or not os.path.isdir(start_path):
        return []

    files = []
    for root, dirs, filenames in os.walk(start_path):
        for fname in filenames:
            if fname.lower().endswith('.mp3'):
                files.append(os.path.join(root, fname))

    return sorted(files)


def bulk_edit_tags(file_paths: list[str], library: list) -> None:
    if not file_paths:
        ui_utils.show_status("No files selected.")
        return

    tag_counts, tag_values, people_tags = collect_tag_data(file_paths)

    if not tag_counts:
        ui_utils.show_status("No tags found in selected files.")
        return

    sorted_tags = sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)

    while True:
        options = []
        for tag_id, count in sorted_tags[:15]:
            info = get_tag_info(tag_id)
            label = info.name[0] if info else "Unknown"
            category = get_tag_category(tag_id).upper()
            options.append(f"{tag_id:<8} [{category:<6}] {label:<25} ({count}/{len(file_paths)} files)")

        options.append("Custom Tag")

        choice = prompt.select("Select tag to bulk edit:", choices=options)

        if not choice:
            break

        if choice == "Custom Tag":
            tag_id = prompt.text("Tag ID (e.g., TIT2, TMCL):")
            if not tag_id:
                continue
            tag_id = tag_id.upper()
        else:
            tag_id = choice.split()[0]

        ops = ["Set Value", "Rename Tag", "Delete Tag"]
        operation = prompt.select(f"Operation for {tag_id}:", choices=ops)

        if not operation:
            continue

        if operation == "Set Value":
            primary_category = get_tag_category(tag_id)
            tag_id_list = [tag_id]

            if prompt.confirm("Apply to other tags too?"):
                multi_tags = prompt.select(
                    "Select additional tags:",
                    choices=[t for t, _ in sorted_tags if t != tag_id],
                    multi=True,
                )
                if multi_tags is not None:
                    for t in multi_tags:
                        if get_tag_category(t) == primary_category:
                            tag_id_list.append(t)
                        else:
                            ui_utils.show_status(f"  {C.BOLD}- {t:<8} [OMITTED] -> Incompatible with {tag_id} ({primary_category}){C.RESET}")
                else:
                    continue

            existing_vals = tag_values.get(tag_id_list[0], [])
            fallback_val = existing_vals[0] if existing_vals else ""
            new_value = prompt_for_value(tag_id_list[0], current_value=fallback_val)
            if new_value is None:
                continue

            if not prompt.confirm(f"Set value on {len(file_paths)} tracks?"):
                continue

            success, fail = apply_bulk_operation_to_files(
                file_paths=file_paths,
                operation='set',
                tag_ids=tag_id_list,
                target_value=new_value,
                library=library
            )

            ui_utils.show_status(f"Done: {success} operations succeeded. Failed: {fail} operations.")

        elif operation == "Rename Tag":
            new_tag_id = prompt.text(f"Rename {tag_id} to:")
            if not new_tag_id or new_tag_id.upper() == tag_id:
                continue

            new_tag_id = new_tag_id.upper()
            if get_tag_category(tag_id) != get_tag_category(new_tag_id):
                ui_utils.show_status(f"Error: Type mismatch between {tag_id} and {new_tag_id}.")
                continue

            if not prompt.confirm(f"Rename tag on {len(file_paths)} tracks?"):
                continue

            success, fail = apply_bulk_operation_to_files(
                file_paths=file_paths,
                operation='rename',
                tag_ids=[tag_id],
                target_value=new_tag_id,
                library=library
            )
            ui_utils.show_status(f"Done: {success} files updated. Failed: {fail} files.")

        elif operation == "Delete Tag":
            if not prompt.confirm(f"Delete {tag_id} from all {len(file_paths)} files?"):
                continue

            success, fail = apply_bulk_operation_to_files(
                file_paths=file_paths,
                operation='delete',
                tag_ids=[tag_id],
                library=library
            )
            ui_utils.show_status(f"Done: {success} files updated. Failed: {fail} files.")


def bulk_replace_apic(file_paths: list[str], library: list) -> None:
    if not file_paths:
        ui_utils.show_status("No files selected.")
        return

    payload = prompt_for_image_payload()
    if not payload:
        return

    img_data, mime, pic_type_int, _ = payload

    success_count = 0
    fail_count = 0

    for file_path in file_paths:
        try:
            audio = ID3(file_path)
            audio.delall('APIC')
            new_frame = create_apic_frame(img_data, mime, pic_type_int, '')
            if new_frame:
                audio.add(new_frame)
                save_id3(audio)
                refresh_library_entry(library, file_path)
                success_count += 1
            else:
                fail_count += 1
        except (mutagen.id3.ID3NoHeaderError, OSError, IOError):  # type: ignore[reportPrivateImportUsage]
            fail_count += 1

    ui_utils.show_status(f"Done: {success_count}/{len(file_paths)} files updated.")
    if fail_count > 0:
        ui_utils.show_status(f"Failed: {fail_count} files.")


def main_menu(library: list) -> None:
    while True:
        choice = prompt.select(
            "Bulk ID3 Editor",
            choices=[
                "Edit Tags",
                "Replace Album Art",
                "Exit"
            ]
        )

        if choice == "Edit Tags":
            file_paths = select_files()
            if file_paths:
                bulk_edit_tags(file_paths, library)

        elif choice == "Replace Album Art":
            file_paths = select_files()
            if file_paths:
                bulk_replace_apic(file_paths, library)

        elif not choice or choice == "Exit":
            break


if __name__ == '__main__':
    main_menu([])
