"""
Bulk ID3 editor - multi-file tag operations with strict type validation.

All operations routed through id3_tag_handler for correctness.
Uses tag_registry as single source of truth.
"""
from __future__ import annotations
import os
import time
import textwrap
from collections import Counter
from mutagen.id3 import ID3

from src.utils import prompt
from src.id3.id3_tag_handler import (
    create_frame,
    create_apic_frame,
    prompt_for_value,
    apply_bulk_edit,
    collect_tag_data,
)
from src.id3.tag_registry import *
from src.utils.ui_utils import get_terminal_width, Colors as C
from src.music_library import refresh_library_entry
from src.config import load_config


def prompt_for_picture_type() -> int:
    """Display standardized prompt to pick an ID3 picture type."""
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
    Prompt user for artwork details.
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
    
    try:
        with open(img_path, 'rb') as f:
            return f.read(), mime, pic_type, desc
    except Exception:
        print("Error reading image file.")
        return None


def bulk_id3_manager(library: list, album_name: str | None = None, paths: list | None = None) -> None:
    """
    Bulk tag operations across a set of tracks.
    
    Args:
        library: Music library list
        album_name: Album name to select tracks from library
        paths: Explicit list of file paths
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
    
    # Helper: Display header
    def _bulk_header():
        available_width = max(10, cols - 6)
        content = f"Bulk edit  {len(album_tracks)} tracks"
        wrapped = []
        for raw_line in content.split("\n"):
            wrapped.extend(textwrap.wrap(raw_line, width=available_width, drop_whitespace=False) or [""])
        return [
            f"╭{'─' * (available_width + 2)}╮",
            *[f"│ {line:<{available_width}} │" for line in wrapped],
            f"╰{'─' * (available_width + 2)}╯"
        ]
    
    # Helper: Get tag name
    def _tag_name(tag: str) -> str:
        info = get_tag_info(tag)
        if info:
            config = load_config()
            TAG_NAME_PREFERENCES = dict(config['tag_name_preferences'])
            return [i if i == TAG_NAME_PREFERENCES[info.tag_id] else '' for i in info.name][0]
        return "Unknown Tag"
    
    # Helper: Get value summary
    def _value_summary(tag: str) -> str:
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
            return vals[0]
        
        return f"{{{len(unique)} values}}"
    
    # --- SCANNING PHASE ---
    print(f"Scanning {len(album_tracks)} tracks…")
    tag_counts, tag_values, _ = collect_tag_data(album_tracks)
    
    if not tag_counts:
        print("No tags found.")
        return
    
    # --- OPERATION SELECTION ---
    operation = prompt.select(
        "Operation:",
        choices=["Set value", "Delete tags", "Rename tags", "Add new tag", "Back"],
        header=_bulk_header
    )
    
    if not operation or operation == "Back":
        return
    
    target_tag_id: str | None = None
    target_val: str | None = None
    selected_tags: list[str] | None = None
    rename_map: dict[str, str] = {}
    
    # --- TAG/VALUE SELECTION ---
    if operation == "Add new tag":
        target_tag_id = prompt.text("New Tag ID (e.g. TSO2, COMM[eng]):")
        if not target_tag_id:
            return
        target_val = prompt_for_value(target_tag_id)
    
    else:
        # Build tag choices
        tag_choices = [
            prompt.Choice(
                title=t,
                value=t,
                category=get_ui_category(t),
                sub_label=_tag_name(t),
                display_val=_value_summary(t),
                count=f"{c}/{len(album_tracks)}"
            )
            for t, c in sorted(tag_counts.items(), key=lambda x: x[0])
        ]
        
        # Show category filter only for non-delete operations
        active_callback = None if operation == "Delete tags" else get_ui_category
        result = prompt.checkbox(
            "Tags:",
            choices=tag_choices,
            interlock_category_callback=active_callback
        )
        
        if not result:
            return
        
        selected_tags = result  # Type narrowed to list[str] after None check
        
        if operation == "Set value":
            target_val = prompt_for_value(selected_tags[0], current_value="")
        
        elif operation == "Rename tags":
            for tag in selected_tags:
                new_tag = prompt.text(f"Rename {tag} to:")
                if new_tag and new_tag.upper() != tag:
                    rename_map[tag] = new_tag.upper()
    
    # --- PROCESSING PHASE ---
    count_modified = 0
    RESTRICTED = {'APIC', 'SYLT', 'MCDI', 'PRIV'}
    
    for path in album_tracks:
        try:
            audio = ID3(path)
            changed = False
            
            if operation == "Add new tag" and target_tag_id and target_val is not None:
                new_frame = create_frame(target_tag_id, target_val)
                if new_frame:
                    audio.add(new_frame)
                    changed = True
            
            elif selected_tags:  # Type narrowed: selected_tags is list[str] here
                # Filter restricted tags
                valid_tags = [t for t in selected_tags if t not in RESTRICTED]
                
                for tag in valid_tags:
                    if operation == "Delete tags":
                        audio.delall(tag)
                        changed = True
                    
                    elif operation == "Set value" and target_val is not None:
                        if apply_bulk_edit(audio, tag, 'set', target_val):
                            changed = True
                    
                    elif operation == "Rename tags" and tag in rename_map:
                        if apply_bulk_edit(audio, tag, 'rename', new_tag_id=rename_map[tag]):
                            changed = True
            
            if changed:
                audio.save(v2_version=3)
                count_modified += 1
                refresh_library_entry(library, path)
        
        except Exception:
            continue
    
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
    
    img_data, mime, pic_type_int, desc = payload
    
    success_count = 0
    fail_count = 0
    
    for file_path in file_paths:
        try:
            audio = ID3(file_path)
            audio.delall('APIC')
            new_frame = create_apic_frame(img_data, mime, pic_type_int, desc)
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
                bulk_id3_manager(library, paths=file_paths)
        
        elif choice == "Replace Album Art":
            file_paths = select_files()
            if file_paths:
                bulk_replace_apic(file_paths, library)
        
        elif not choice or choice == "Exit":
            break


if __name__ == '__main__':
    main_menu([])