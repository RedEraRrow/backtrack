"""Bulk ID3 tag operations across multiple files."""
from __future__ import annotations
import os
import re
import mutagen.id3
from mutagen.id3 import ID3
from src.utils import prompt
from src.id3.id3_tag_handler import (
    prompt_for_value, get_tag_info, get_tag_category, display_tag_id, create_frame,
    rename_frame, save_id3, _prompt_for_image_metadata, _prompt_for_picture_type,
    _PICTURE_TYPES, pick_nearby_cover,
)
from src.id3.tag_registry import parse_composite_tag_id
from src.id3 import filename_parser as fp
from src.id3 import tag_writer as tw
from src.id3 import bulk_ops as bo
from src.id3 import cover_matcher as cm
from src import bulk_pattern as bp
from src.music_library import format_value_list
from src.utils import ui_utils
from src.utils.ui_utils import get_terminal_width
from src.music_library import drop_moved, refresh_library_entry
from src.trim import trim as _trim
from src.trim.trim_bulk import trim_conveyor, apply_replaygain_op

from collections import Counter

from mutagen.id3._frames import APIC
from src.id3.bulk_common import _RENUMBER_COLUMNS, _walk
from src.id3.bulk_names import (
    _compute_set_value, _derive_regex_base, derive_from_filename, rename_files_op,
)
from src.id3.bulk_art import (
    _COVER_PREVIEW_COLUMNS, set_album_art_op, set_picture_type_op,
)
from src.id3.bulk_sort import apply_sort_orders
from src.id3.bulk_assign import (
    assign_by_pattern, bulk_fraction_editor, bulk_people_editor,
)
from src.id3.bulk_common import preview_and_apply

# Structured columns for the bulk tag picker. Column 1 holds the tag id AND the
# friendly name as two styled segments (TAG bright + friendly dim) in one column.
_BULK_COLUMNS = [
    prompt.Column(style='primary'),                                     # TAG (friendly)
    prompt.Column(style='dynamic-dim', priority=1),                     # type / category — drops first
    prompt.Column(style='normal', flex=True),                           # value summary (kept)
    prompt.Column(style='dynamic-dim', align='right', pin=True, priority=2),  # count / total — drops next
]




# Length-tag strip preview: file · which stale tags were found.
_STRIP_LENGTH_COLUMNS = [
    prompt.Column(style='primary', flex=True),
    prompt.Column(style='dynamic-dim', align='right', pin=True),
]


def renumber_tracks_op(paths: list, library: list, header) -> None:
    """Renumber track numbers per-disc (disc-relative) ↔ continuous (album-
    relative / movement systems). Works for MP3 and MP4 via tag_writer."""
    ordered, skipped_fmt = bo.read_numbering(paths)
    if not ordered:
        ui_utils.show_status("No MP3/MP4 tracks to renumber.")
        return

    _MODES = ["Continuous (album-relative) — 1…N across all discs",
              "Per-disc (disc-relative) — restart at 1 each disc"]
    state: dict = {'mode_sel': _MODES[0], 'apply_set': set()}

    def _ask_mode() -> bool:
        """Which numbering to lay down."""
        sel = prompt.select("Renumber to:", choices=_MODES,
                            index=_MODES.index(state['mode_sel']),
                            header=header(f"{len(ordered)} tracks in disc/track order"))
        if not sel:
            return False
        state['mode_sel'] = sel
        return True

    def _ask_preview() -> bool:
        """Show what each file becomes and take the selection."""
        mode = 'continuous' if state['mode_sel'].startswith("Continuous") else 'per_disc'
        plan = bo.plan_renumber(ordered, skipped_fmt, mode)
        state['plan'] = plan
        ticked = state['apply_set'] or {c.path for c in plan.changed}
        choices = [
            prompt.Choice(title=name, value=c.path, checked=c.path in ticked,
                          cells=[str(pos), name, why])
            for (pos, name, why), c in zip(bo.position_rows(plan), plan.changes)]
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
    if not state['apply_set']:
        ui_utils.show_status("No files selected.")
        return

    applied = bo.apply_changes(
        state['plan'], library,
        lambda c: tw.write_fields(c.path, c.fields, {'track'}, overwrite=True),
        selected=state['apply_set'])
    ui_utils.show_status(bo.summarise(applied, "Renumbered"))


def reflow_discs_op(paths: list, library: list, header) -> None:
    """Re-flow disc numbering after a disc is inserted, removed or appended.

    Renumbering the distinct disc values onto a dense 1…N handles all three edits
    with one rule: a disc parked at ``1.5`` becomes 2 and everything above shifts
    up; a deleted disc closes its gap; an appended disc keeps its number and only
    the totals move. "Totals only" fixes the "of N" half without renumbering, for
    deliberately sparse discs. MP3 and MP4 both write, via tag_writer.
    """
    ordered, skipped_fmt = bo.read_numbering(paths)
    if not ordered:
        ui_utils.show_status("No MP3/MP4 tracks to reflow.")
        return

    runs = bp.disc_ranges(ordered)
    disc_list = ', '.join(lab for _, _, lab in runs[:8]) + ('…' if len(runs) > 8 else '')
    sub = f"{len(ordered)} tracks · discs {disc_list}"

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

    plan = bo.plan_reflow(ordered, skipped_fmt, renumber=renumber,
                          disc_totals=disc_totals, track_totals=track_totals)
    if plan.message:
        # Already dense with the right totals — say so, rather than showing an
        # all-unticked preview that ends in "No tracks selected".
        ui_utils.show_status(plan.message)
        return

    fields = {'disc'} | ({'track'} if track_totals else set())
    preview_and_apply(plan, library, header,
                      lambda c: tw.write_fields(c.path, c.fields, fields, overwrite=True),
                      "Reflowed disc numbering on", count=ui_utils.plural(len(ordered), "track"),
                      changing="changing", unchanged=lambda c: 'no change', skipped=skipped_fmt)



def strip_single_disc_op(paths: list, library: list, header) -> None:
    """Remove the disc number from tracks that are disc 1 of 1.

    A single-disc release doesn't need a disc tag: "1/1" is noise that shows up as
    a disc header in browse lists and in file names derived from tags. Bare "1"
    (no total) counts too, but only when nothing in the selection sits on another
    disc — on a real multi-disc album an untotalled "1" is meaningful.
    """
    ordered, skipped_fmt = bo.read_numbering(paths)
    if not ordered:
        ui_utils.show_status("No MP3/MP4 tracks to change.")
        return

    plan = bo.plan_strip_single_disc(ordered, skipped_fmt)
    if plan.message:
        ui_utils.show_status(plan.message)
        return

    def _kept(c) -> str:
        """What an untouched row says instead of a change."""
        stored = c.fields.get('keeps', '')
        return f"keeps disc {stored}" if stored else "no disc number"

    preview_and_apply(plan, library, header, lambda c: tw.clear_fields(c.path, {'disc'}),
                      "Removed the disc number from", count=ui_utils.plural(len(ordered), "track"),
                      changing="with 1/1", unchanged=_kept, skipped=skipped_fmt)


def strip_length_tags_op(paths: list, library: list, header) -> None:
    """Remove stale TLEN (track length) and non-zero TDLY (playlist delay).

    Both hold millisecond values nothing recomputes once a file is cut by any
    means, including a trim done outside backtrack — this is the cleanup pass
    for files that predate that feature. MP3 only, since neither frame has an
    MP4 analogue.
    """
    songs, skipped_fmt = bo.read_length_tags(paths)
    if not songs:
        ui_utils.show_status("No MP3 tracks to check.")
        return

    plan = bo.plan_strip_length_tags(songs, skipped_fmt)
    if plan.message:
        ui_utils.show_status(plan.message)
        return

    preview_and_apply(plan, library, header, bo.strip_length_writer,
                      "Stripped stale length tags from", count=ui_utils.plural(len(songs), "track"),
                      changing="with stale tags", unchanged=lambda c: "no stale tags",
                      skipped=skipped_fmt, columns=_STRIP_LENGTH_COLUMNS, positions=False)






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
    album_tracks = drop_moved(album_tracks)
    if not album_tracks:
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
            count = f"{len(album_tracks)} track" + ("" if len(album_tracks) == 1 else "s")
            return prompt.rounded_header("Bulk Edit", "", count, subtitle)
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
        _automation_choices = [
            "Derive from filename",
            "Rename files from tags",
            "Set album art from files",
            "Assign by range / schedule",
            "Apply sort orders",
            "Renumber tracks (disc ↔ continuous)",
            "Reflow disc numbering",
            "Remove single-disc numbering",
            "Strip stale length tags",
        ]
        if _trim.HAS_FFMPEG:
            # Hidden rather than offered and failing at the keypress (section 2.6).
            _automation_choices.append("Trim tracks…")
            _automation_choices.append("Measure loudness / set ReplayGain…")
        _automation_choices += ["Set picture type", "Copy from first track"]
        operation = prompt.select(
            "Automation:",
            choices=_automation_choices,
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
        "Strip stale length tags": "Strip Length Tags",
        "Trim tracks…": "Trim Tracks",
        "Measure loudness / set ReplayGain…": "Apply ReplayGain",
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
    if operation == "Strip Length Tags":
        strip_length_tags_op(album_tracks, library, _bulk_header)
        return
    if operation == "Trim Tracks":
        trim_conveyor(album_tracks, library, _bulk_header)
        return
    if operation == "Apply ReplayGain":
        apply_replaygain_op(album_tracks, library, _bulk_header)
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
                        # Build the new frame first: if the value can't make
                        # one, the old frame must survive (it was deleted
                        # first, and saved away with another tag's change).
                        new_frame = create_frame(tag, new_val)
                        if new_frame:
                            audio.delall(tag)
                            audio.add(new_frame)
                            changed = True
                        else:
                            ui_utils.show_status(
                                f"{os.path.basename(path)}: {new_val!r} isn't a valid {tag}; kept the old value.")

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
