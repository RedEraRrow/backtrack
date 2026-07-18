"""Bulk ID3 tag operations across multiple files."""
from __future__ import annotations
import os
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
    apply_bulk_operation_to_files,
    _prompt_for_image_metadata,
    _EXT_TO_MIME,
)
from src.id3.tag_registry import parse_composite_tag_id
from src.id3 import filename_parser as fp
from src.id3 import tag_writer as tw
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
            audio.save(p, v2_version=3)
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
            inner = max(12, cols_now - mh - 4)
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

    operation = prompt.select(
        "Operation:",
        choices=["Derive from filename", "Set value", "Copy from first track",
                 "Delete tags", "Rename tags", "Add new tag"],
        header=_bulk_header()
    )

    if not operation:
        return

    op_display = operation.lower()
    op_map = {
        "Derive from filename": "Derive From Filename",
        "Set value": "Set Common Value",
        "Copy from first track": "Copy From First Track",
        "Delete tags": "Delete Tags",
        "Rename tags": "Rename Tags",
        "Add new tag": "Add New Tag",
    }
    operation = str(op_map.get(operation, operation))

    # Derivation works on files with no tags at all, so it runs before the
    # "no tags found" guard and manages its own preview/confirm/apply flow.
    if operation == "Derive From Filename":
        derive_from_filename(album_tracks, library, _bulk_header)
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
            target_val = prompt_for_value(first_tag, current_value=fallback_val)
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
                        audio.delall(tag)
                        try:
                            if target_val is None:
                                continue
                            new_frame = create_frame(tag, target_val)
                            if new_frame:
                                audio.add(new_frame)
                                changed = True
                        except ValueError:
                            pass

            if changed:
                audio.save(path, v2_version=3)   # explicit path: works for a fresh ID3 too
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
                audio.save(v2_version=3)
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
