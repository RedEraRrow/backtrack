"""Bulk operations driven by file names: deriving tags from them, and renaming files from tags."""
from __future__ import annotations
import os
import re
from src.utils import prompt
from src.id3 import filename_parser as fp
from src.id3 import tag_writer as tw
from src.id3 import bulk_ops as bo
from src.id3 import file_namer as fnm
from src.utils import ui_utils
from src.id3.bulk_common import _RENAME_PICK_COLUMNS, _SKIP, _SORT_BASE, _num_pair, _plan_write, _show_tokens, _sort_value, _walk


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
    plan = bo.Plan(changes=[bo.Change(path=p, why=' · '.join(
        f"{f}={v}" for f, v in plans[p].items())) for p in to_write],
        skipped=skipped_fmt)
    applied = bo.apply_changes(
        plan, library, bo.derive_writer(derived, apply_fields, overwrite),
        selected=apply_paths)
    ui_utils.show_status(bo.summarise(applied, "Derived tags for",
                                      skipped_note="non-MP3/MP4 skipped"))


# Rename preview: position · old name · new name. Position (index) drops first;
# both names are the pending change and are kept.
_RENAME_COLUMNS = [
    prompt.Column(style='dynamic-dim', align='right', max_width=4, priority=1),
    prompt.Column(style='dynamic-dim', flex=True),
    prompt.Column(style='primary', flex=True),
]


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

    todo = [(p, os.path.join(os.path.dirname(p), n))
            for (p, o, n) in changed if p in apply_set]
    applied = bo.rename_files(todo, library)
    applied.skipped = skipped_fmt
    ui_utils.show_status(bo.summarise(applied, "Renamed"))
