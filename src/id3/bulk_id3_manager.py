"""
Bulk ID3 tag manager - clean, widget-driven, fully modular.

Uses unified id3_tag_handler for tag detection, frame creation, and prompting.
"""
from __future__ import annotations
import os
import time
import textwrap
from collections import Counter
from mutagen.id3 import ID3
from mutagen.id3._frames import APIC

from src.utils import ui_utils, prompt
from src.music_library import refresh_library_entry
from src.id3.id3_tag_handler import (
    get_tag_info,
    get_tag_category,
    summarize_tag_value,
    collect_tag_data,
    prompt_for_value,
    apply_bulk_edit,
    create_apic_frame,
)


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
        print("No tracks found.")
        return

    cols = ui_utils.get_terminal_width()

    # Scan all tags using unified handler
    print(f"Scanning {len(album_tracks)} tracks…")
    tag_counts, tag_values, people_tags = collect_tag_data(album_tracks)

    if not tag_counts:
        print("No tags found.")
        return

    # Operation selection
    def _bulk_header():
        available_width = max(10, cols - 6)
        content = f"Bulk edit  {len(album_tracks)} tracks"
        wrapped = textwrap.wrap(content, width=available_width, drop_whitespace=False) or [""]
        return [
            f"╭{'─' * (available_width + 2)}╮",
            *[f"│ {line:<{available_width}} │" for line in wrapped],
            f"╰{'─' * (available_width + 2)}╯"
        ]

    operation = prompt.select(
        "Operation:",
        choices=["Set value", "Delete tags", "Rename tags", "Add new tag", "Back"],
        header=_bulk_header
    )

    if not operation or operation == "Back":
        return

    op_map = {
        "Set value": "set",
        "Delete tags": "delete",
        "Rename tags": "rename",
        "Add new tag": "add",
    }
    operation = op_map.get(operation, operation)

    # Tag selection helpers
    TAG_MAX = 28
    VAL_MAX = 30

    def _value_summary(tag_id: str) -> str:
        vals = tag_values.get(tag_id, [])
        if not vals:
            return ""
        unique = set(vals)
        if len(unique) == 1:
            v = vals[0]
            return v if len(v) <= VAL_MAX else v[:VAL_MAX - 1] + "…"
        return f"{{{len(unique)} values}}"

    def _tag_option_title(tag_id: str, count: int) -> str:
        info = get_tag_info(tag_id)
        label = info.label if info else tag_id
        tag_disp = tag_id if len(tag_id) <= TAG_MAX else tag_id[:TAG_MAX - 1] + "…"
        val_disp = _value_summary(tag_id)
        val_disp = val_disp if len(val_disp) <= VAL_MAX else val_disp[:VAL_MAX - 1] + "…"
        return f"{tag_disp:<{TAG_MAX}} | {val_disp:<{VAL_MAX}}  {count}/{len(album_tracks)}"

    selected_tags = []
    target_value = None
    target_tag_id = None

    # Collect input
    if operation == "add":
        target_tag_id = prompt.text("New Tag ID (e.g. TSO2, COMM[eng]):")
        if not target_tag_id:
            return
        target_value = prompt.text(f"Value for {target_tag_id}:")
        if target_value is None:
            return
    else:
        tag_options = [
            prompt.Choice(_tag_option_title(t, c), t)
            for t, c in sorted(tag_counts.items())
        ]
        selected_tags = prompt.checkbox(f"Select tags to {operation}:", tag_options)
        if not selected_tags:
            return

        if operation == "rename":
            target_value = prompt.text("New tag ID (e.g. TPE2, COMM[eng]):")
        elif operation == "set":
            # For mixed tag types, check if all are same category
            categories = {get_tag_category(t) for t in selected_tags}
            if len(categories) > 1:
                print("Cannot set value: mixed tag types selected.")
                time.sleep(1.5)
                return
            
            # Get initial people data if setting people tags
            initial_people = None
            if selected_tags[0].startswith(('TMCL', 'TIPL')):
                if selected_tags[0] in people_tags and people_tags[selected_tags[0]]:
                    initial_people = people_tags[selected_tags[0]][0]
            
            target_value = prompt_for_value(selected_tags[0], initial_people=initial_people)

    if not target_value and operation not in ["delete", "add"]:
        return

    # APIC special handling
    apic_tags = [t for t in selected_tags if t.startswith('APIC')] if selected_tags else []
    non_apic_tags = [t for t in selected_tags if not t.startswith('APIC')] if selected_tags else []
    new_apic_frame = None
    new_apic_desc = None

    if apic_tags and operation not in ["delete"]:
        apic_action = prompt.select(
            f"APIC action ({len(apic_tags)} tags):",
            choices=["Replace Image", "Edit Description", "Edit Picture Type", "Skip"]
        )
        
        if apic_action == "Replace Image":
            img_path = prompt.text("Path to new image:")
            if img_path and os.path.isfile(img_path):
                ext = os.path.splitext(img_path)[1].lower()
                mime = {
                    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                    '.png': 'image/png', '.gif': 'image/gif',
                    '.bmp': 'image/bmp', '.webp': 'image/webp'
                }.get(ext, 'image/jpeg')
                
                pic_type_choice = prompt.select(
                    "Picture type:",
                    choices=[
                        prompt.Choice("Cover (front) [3]", 3),
                        prompt.Choice("Cover (back) [4]", 4),
                        prompt.Choice("Artist [8]", 8),
                        prompt.Choice("Other [0]", 0),
                    ]
                )
                pic_type: int = pic_type_choice if isinstance(pic_type_choice, int) else 3
                
                desc = prompt.text("Description (blank for none):") or ''
                with open(img_path, 'rb') as f:
                    new_apic_frame = create_apic_frame(f.read(), mime, pic_type, desc)
            else:
                print("File not found.")
                apic_tags = []
        elif apic_action == "Edit Description":
            new_apic_desc = prompt.text("New description:")
            if new_apic_desc is None:
                apic_tags = []
        elif apic_action == "Edit Picture Type":
            new_pic_type = prompt.select(
                "Picture type:",
                choices=[
                    prompt.Choice("Cover (front) [3]", 3),
                    prompt.Choice("Cover (back) [4]", 4),
                    prompt.Choice("Artist [8]", 8),
                    prompt.Choice("Other [0]", 0),
                ]
            )
            if new_pic_type is not None:
                new_apic_frame = new_pic_type
            else:
                apic_tags = []
        else:
            apic_tags = []

    # Confirm and apply
    if not prompt.confirm(f"Apply {operation} to {len(album_tracks)} tracks?"):
        return

    count_modified = 0
    for path in album_tracks:
        try:
            audio = ID3(path)
            changed = False

            if operation == "add":
                from src.id3.id3_tag_handler import create_frame
                assert target_tag_id is not None
                new_frame = create_frame(target_tag_id, target_value)
                if new_frame:
                    audio.add(new_frame)
                    changed = True

            # APIC special handling
            for tag in apic_tags:
                if tag in audio:
                    if new_apic_desc is not None:
                        audio[tag].desc = new_apic_desc
                        changed = True
                    elif isinstance(new_apic_frame, int):
                        audio[tag].type = new_apic_frame
                        changed = True
                    elif isinstance(new_apic_frame, APIC):
                        audio.delall(tag)
                        audio.add(new_apic_frame)
                        changed = True

            # Standard tag operations
            for tag in non_apic_tags:
                if apply_bulk_edit(audio, tag, operation, target_value, target_value if operation == "rename" else None):
                    changed = True

            if changed:
                audio.save(v2_version=3)
                count_modified += 1
                try:
                    refresh_library_entry(library, path)
                except Exception:
                    pass

        except Exception:
            pass

    print(f"Successfully processed {count_modified} files.")
    time.sleep(1.5)