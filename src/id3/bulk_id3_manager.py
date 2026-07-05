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
        try:
            audio = ID3(path)
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
        choices=["Set value", "Copy from first track", "Delete tags", "Rename tags", "Add new tag"],
        header=_bulk_header()
    )

    if not operation:
        return

    op_display = operation.lower()
    op_map = {
        "Set value": "Set Common Value",
        "Copy from first track": "Copy From First Track",
        "Delete tags": "Delete Tags",
        "Rename tags": "Rename Tags",
        "Add new tag": "Add New Tag",
    }
    operation = str(op_map.get(operation, operation))

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
        try:
            audio = ID3(path)
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
                audio.save(v2_version=3)
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
