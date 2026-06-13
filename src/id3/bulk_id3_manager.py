"""
Bulk ID3 editor - multi-file tag operations with strict type validation.

All operations routed through id3_tag_handler for correctness.
"""
from __future__ import annotations
import os
import time
from mutagen.id3 import ID3
from src.utils import prompt
from src.id3.id3_tag_handler import (
    collect_tag_data,
    prompt_for_value,
    apply_bulk_edit,
    get_tag_info,
    get_tag_category,
    summarize_tag_value,
    create_apic_frame,
    create_frame,
    rename_frame,
    apply_bulk_operation_to_files,
)
from src.utils.ui_utils import get_terminal_width, Colors as C
from src.music_library import refresh_library_entry

from collections import Counter
import textwrap
import re

from mutagen.id3._frames import APIC


def prompt_for_picture_type() -> int:
    """Displays a standardized prompt to pick an ID3 picture type integer."""
    choice = prompt.select(
        "Picture type:",
        choices=[
            prompt.Choice("Cover (front) [3]", 3),
            prompt.Choice("Cover (back)  [4]", 4),
            prompt.Choice("Artist        [8]", 8),
            prompt.Choice("Other         [0]", 0),
        ]
    )
    if isinstance(choice, int):
        return choice
    if choice and isinstance(choice, str):
        return int(choice.split()[0])
    return 3


def prompt_for_image_payload() -> tuple[bytes, str, int, str] | None:
    """
    Prompts the user for artwork details.
    Returns (img_data, mime_type, pic_type, description) or None if cancelled.
    """
    img_path = prompt.text("Path to image:")
    if not img_path or not os.path.isfile(img_path):
        print("File not found.")
        return None
        
    ext = os.path.splitext(img_path)[1].lower()
    mime = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png', '.gif': 'image/gif',
        '.bmp': 'image/bmp', '.webp': 'image/webp'
    }.get(ext, 'image/jpeg')
    
    pic_type = prompt_for_picture_type()
    desc = prompt.text("Description (leave blank for none):") or ''
    
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
        print("No tracks found.")
        return
 
    cols = get_terminal_width()
 
    print(f"Scanning {len(album_tracks)} tracks…")
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
                elif hasattr(raw, 'text'):
                    # 1. Unify mutagen's text list elements into a single string
                    full_text = "".join(str(t) for t in raw.text)
                    
                    # 2. Normalize and split the string cleanly by any newline type (\r\n or \n)
                    # This guarantees it separates actual lines, not individual characters
                    lines = [line for line in full_text.replace("\r\n", "\n").split("\n")]
                    
                    # 3. Join the extracted lines back together with backslashes
                    val = "\\".join(lines)
                else:
                    val = str(raw)
                tag_values.setdefault(k, []).append(val)
        except Exception as e:
            print(f"Error scanning {os.path.basename(path)}: {e}")
            continue
 
    def _bulk_header():
        available_width = max(10, cols - 6)
        content = f"Bulk edit  {len(album_tracks)} tracks"
        
        wrapped = []
        for raw_line in content.split("\n"):
            if raw_line == "":
                wrapped.append("")
            else:
                wrapped.extend(textwrap.wrap(raw_line, width=available_width, drop_whitespace=False) or [""])
        
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
 
    op_display = operation.lower()
    op_map = {
        "Set value": "Set Common Value",
        "Delete tags": "Delete Tags",
        "Rename tags": "Rename Tags",
        "Add new tag": "Add New Tag",
    }
    operation = op_map.get(operation, operation)
 
    if not all_tag_counts and operation not in ("Add New Tag",):
        print("No tags found.")
        return
 
    ALIAS_MAX = 22
    TAG_MAX = 12
    VAL_MAX = 200
 
    def _b_alias(tag):
        info = get_tag_info(tag)
        return info.label if info else ""
 
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
            return v
 
        n_vary = len(unique)
        return f"{{{n_vary} values}}"
 
    bulk_tag_col = min(TAG_MAX, max((len(t) for t in all_tag_counts), default=6))
    bulk_alias_col = min(ALIAS_MAX, max((len(_b_alias(t)) + 3 for t in all_tag_counts), default=0))
    _fixed = bulk_tag_col + bulk_alias_col + 32
    bulk_val_col = min(VAL_MAX, max(cols - _fixed, 10))
 
    selected_tags = []
    target_tag_id = None
    target_val = None
 
    if operation == "Add New Tag":
        target_tag_id = prompt.text("New Tag ID (e.g. TSO2, COMM[eng]):")
        if not target_tag_id:
            return
        target_tag_id = target_tag_id.split(':')[0].upper() + ":" + ":".join(target_tag_id.split(':')[1:])
        target_val = prompt.text(f"Value for {target_tag_id}:")
        if target_val is None:
            return
    else:
        # Multi-select tag list
        sorted_tags = sorted(all_tag_counts.items(), key=lambda x: x[1], reverse=True)
        tag_choices = []
        
        for t, c in sorted_tags:
            tag_choices.append(prompt.Choice(
                title=t,
                value=t,
                category=get_tag_category(t).lower(),
                sub_label=_b_alias(t),
                display_val=_value_summary(t),
                count=f"{c}/{len(album_tracks)}"
            ))
        
        # Disable category interlock if the user is just deleting tags
        active_callback = None if operation == "Delete Tags" else get_tag_category
        
        selected_tags = prompt.checkbox(
            "Tags:",
            choices=tag_choices,
            interlock_category_callback=active_callback,
        )
        
        if not selected_tags:
            return
        
        if operation in ("Set Common Value",):
            target_val = prompt_for_value(selected_tags[0], current_value="")
            if target_val is None:
                return
 
    count_modified = 0
    for path in album_tracks:
        try:
            audio = ID3(path)
            changed = False
 
            if operation == "Add New Tag":
                try:
                    assert target_tag_id is not None
                    assert target_val is not None
                    new_frame = create_frame(target_tag_id, target_val)
                    if new_frame:
                        audio.add(new_frame)
                        changed = True
                except (ValueError, AssertionError):
                    pass
            else:
                for tag in selected_tags:
                    if operation == "Delete Tags":
                        audio.delall(tag)
                        changed = True
                    elif operation == "Rename Tags":
                        try:
                            new_tag = prompt.text(f"Rename {tag} to:")
                            if new_tag and new_tag != tag:
                                old_frame = audio.pop(tag)
                                if rename_frame(audio, old_frame, new_tag):
                                    changed = True
                                else:
                                    audio.add(old_frame)
                        except KeyError:
                            pass
                    elif operation == "Set Common Value":
                        audio.delall(tag)
                        try:
                            assert target_val is not None
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
            print(f"Failed to process track {os.path.basename(path)}: {e}")
 
    print(f"Successfully processed {count_modified} files.")
    time.sleep(1.5)
 

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


def bulk_replace_apic(file_paths: list[str], library: list) -> None:
    """Replace album art in multiple files."""
    if not file_paths:
        print("No files selected.")
        time.sleep(1)
        return
    
    payload = prompt_for_image_payload()
    if not payload:
        time.sleep(1)
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
        except Exception:
            fail_count += 1
    
    print(f"Done: {success_count}/{len(file_paths)} files updated.")
    if fail_count > 0:
        print(f"Failed: {fail_count} files.")
    time.sleep(2)


def main_menu(library: list) -> None:
    """Main menu for bulk operations."""
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
                bulk_id3_manager(file_paths)
        
        elif choice == "Replace Album Art":
            file_paths = select_files()
            if file_paths:
                bulk_replace_apic(file_paths, library)
        
        elif not choice or choice == "Exit":
            break


if __name__ == '__main__':
    main_menu([])
