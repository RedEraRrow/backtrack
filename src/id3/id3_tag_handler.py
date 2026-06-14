"""
Unified ID3 frame factory and value prompting - simplified using tag_registry.

Single source of truth: tag_registry.py
Functions here implement frame creation and UI widget selection.
"""
from __future__ import annotations
from typing import Any, Optional
from mutagen.id3 import ID3

from src.id3.tag_registry import *

import time
from src.utils import prompt


# ============================================================================
# COMPOSITE TAG ID PARSING
# ============================================================================

def parse_composite_tag_id(tag_id: str) -> tuple[str, str, str]:
    """
    Parses complex tag strings into (base_id, description, language).
    
    Supports:
      - TXXX:Transcription -> ('TXXX', 'Transcription', '')
      - TXXX:Transcription:eng -> ('TXXX', 'Transcription', 'eng')
      - COMM[eng] -> ('COMM', '', 'eng')
    """
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    desc_val = ''
    lang_val = ''

    # Handle legacy bracket notation: COMM[eng]
    if '[' in tag_id and ']' in tag_id:
        lang_val = tag_id.split('[')[1].split(']')[0]
        return base_id, desc_val, lang_val

    # Handle colon notation: TXXX:Transcription:eng
    if ':' in tag_id:
        parts = tag_id.split(':')
        if len(parts) == 3:
            desc_val = parts[1]
            lang_val = parts[2]
        elif len(parts) == 2:
            desc_val = parts[1]
    
    return base_id, desc_val, lang_val


# ============================================================================
# FRAME CREATION
# ============================================================================

def create_frame(tag_id: str, value: Any) -> TextFrame | APIC | SYLT | USLT | TMCL | TIPL | None:
    """
    Universal frame factory - creates correct frame type for any tag.
    
    Validates value format before frame creation.
    Returns None if value is None, frame type not supported, or format invalid.
    """
    if value is None:
        return None
    
    info = get_tag_info(tag_id)
    if not info:
        return None
    
    parsed_base, parsed_desc, parsed_lang = parse_composite_tag_id(tag_id)
    
    try:
        # TEXT FRAMES
        if info.frame_type == 'TEXT':
            text_val = str(value).strip()
            if not text_val:
                return None
            
            frame_class = info.mutagen_class
            
            # User-defined frames need description
            if frame_class in [TXXX, WXXX]:
                return frame_class(encoding=3, desc=parsed_desc, text=text_val)
            
            # Language-aware frames
            if frame_class in [COMM, USLT]:
                clean_lang = str(parsed_lang).strip() if parsed_lang else 'eng'
                if len(clean_lang) != 3:
                    clean_lang = 'eng'
                return frame_class(encoding=3, lang=clean_lang, desc=parsed_desc, text=text_val)
            
            # Standard text frames
            return frame_class(encoding=3, text=[text_val])
        
        # TIMESTAMP FRAMES (ISO8601)
        elif info.format_spec == 'ISO8601':
            date_val = str(value).strip()
            if not date_val or not any(c.isdigit() for c in date_val):
                return None
            return info.mutagen_class(encoding=3, text=[date_val])
        
        # FRACTIONAL FRAMES (TRCK, TPOS, MVIN)
        elif info.frame_type == 'FRACTIONAL':
            frac_val = str(value).strip()
            if not frac_val or not any(c.isdigit() for c in frac_val):
                return None
            return info.mutagen_class(encoding=3, text=[frac_val])
        
        # NUMERIC FRAMES (TBPM, TLEN, TDLY)
        elif info.frame_type == 'NUMERIC':
            num_val = str(value).strip()
            if not num_val or not any(c.isdigit() for c in num_val):
                return None
            return info.mutagen_class(encoding=3, text=[num_val])
        
        # LIST FRAMES (TIPL, TMCL)
        elif info.frame_type == 'LIST':
            # Value should be list of (role, person) tuples
            if isinstance(value, list) and value:
                people_list = []
                for item in value:
                    if isinstance(item, (tuple, list)) and len(item) == 2:
                        people_list.append((str(item[0]).strip(), str(item[1]).strip()))
                
                if not people_list:
                    return None
                
                return info.mutagen_class(encoding=3, people=people_list)
            return None
        
        # SYLT (not supported in simple create_frame - use dedicated import)
        elif tag_id.startswith('SYLT'):
            return None
        
        # Unknown type
        return None
    
    except Exception:
        return None


def create_apic_frame(
    data: bytes,
    mime: str,
    pic_type: int,
    desc: str = ''
) -> APIC:
    """Create APIC (album art) frame."""
    if not isinstance(pic_type, int):
        pic_type = 3  # Default to "Cover (front)"
    return APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=data)


def rename_frame(audio_obj: ID3, old_frame, new_id: str) -> bool:
    """Rename a frame while preserving type and value."""
    old_id = None
    for key in audio_obj.keys():
        if audio_obj[key] is old_frame:
            old_id = key
            break
    
    if not old_id:
        return False
    
    # Verify category match (not changing frame type)
    old_info = get_tag_info(old_id)
    new_info = get_tag_info(new_id)
    if not old_info or not new_info or old_info.ui_category != new_info.ui_category:
        return False
    
    # Extract value from old frame
    if hasattr(old_frame, 'people'):
        value = old_frame.people
    elif hasattr(old_frame, 'text'):
        value = old_frame.text[0] if old_frame.text else None
    else:
        value = str(old_frame)
    
    if value is None:
        return False
    
    # Create new frame
    new_frame = create_frame(new_id, value)
    if not new_frame:
        return False
    
    try:
        audio_obj.pop(old_id)
        audio_obj.add(new_frame)
        return True
    except Exception:
        return False


# ============================================================================
# VALUE PROMPTING (widget selection per UI category)
# ============================================================================

def prompt_for_value(tag_id: str, current_value: Any = None, initial_people: list | None = None) -> Any | None:
    """
    Auto-select correct widget based on tag's UI category.
    Returns value ready for create_frame(), or None if cancelled.
    """
    info = get_tag_info(tag_id)
    if not info:
        return None
    
    ui_category = info.ui_category
    label = get_preferred_tag_name(tag_id)
    
    # TEXT
    if ui_category == 'text':
        default_val = str(current_value) if current_value else ""
        return prompt.text(f"{label}:", default=default_val)
    
    # MULTILINE TEXT (COMM, USLT)
    if ui_category == 'multiline text':
        default_val = str(current_value) if current_value else ""
        return prompt.system_editor_edit(initial_text=default_val)
    
    # DATE (TDRC, TDEN, TDOR, etc.)
    if ui_category == 'date':
        default_val = str(current_value) if current_value else ""
        return prompt.calendar_select(
            f"{label} (YYYY, YYYY-MM-DD, etc.):",
            initial=default_val
        )
    
    # FRACTION (TRCK, TPOS, MVIN)
    if ui_category == 'fraction':
        default_val = str(current_value) if current_value else ""
        return prompt.text(
            f"{label} (format: X or X/Total):",
            default=default_val
        )
    
    # DURATION (TLEN, TDLY)
    if ui_category == 'duration':
        default_val = str(current_value) if current_value else ""
        return prompt.text(
            f"{label} (milliseconds):",
            default=default_val
        )
    
    # PEOPLE (TMCL, TIPL)
    if ui_category == 'people':
        people: list[tuple[str, str]] | None = initial_people if initial_people else []
        
        if isinstance(current_value, list):
            people = [(str(r), str(n)) for r, n in current_value]
        
        while True:
            # We must treat 'people' as potentially None for the Pylance checker
            # Use 'or []' to treat None as an empty list for the UI selection
            action = prompt.select(
                f"{label}:",
                choices=["Edit in List", "Import from Text", "Done" if (people and len(people) > 0) else "Done (empty)"]
            )
            
            if action == "Edit in List":
                # Ensure it's a list before passing to editor
                people = prompt.list_edit(f"Edit {tag_id}", people or [], ("ROLE", "NAME"))
            elif action == "Import from Text":
                text_input = prompt.system_editor_edit(initial_text="")
                if text_input:
                    imported = parse_people_from_text(text_input)
                    # Merge logic: if people is None, initialize it with the import
                    if people is None:
                        people = imported
                    else:
                        people.extend(imported)
            else:
                return people
    
    # IMAGE (APIC)
    if ui_category == 'image':
        print("Use 'Manage' action to edit album art.")
        time.sleep(1)
        return None
    
    # LYRICS (SYLT)
    if ui_category == 'lyrics':
        print("Use dedicated lyric import for SYLT.")
        time.sleep(1)
        return None
    
    # Fallback
    return prompt.text(f"{label}:")


def parse_people_from_text(text: str) -> list[tuple[str, str]]:
    """
    Parse people list from multiline text.
    Expected format (per line): "Role/Instrument - Person Name"
    """
    people = []
    for line in text.strip().split('\n'):
        line = line.strip()
        if not line or ' - ' not in line:
            continue
        
        role, name = line.split(' - ', 1)
        people.append((role.strip(), name.strip()))
    
    return people


# ============================================================================
# TAG SUMMARY (for display)
# ============================================================================

def summarize_tag_value(tag_id: str, raw_frame) -> str:
    """
    Get short display summary of tag value (max 100 chars).
    """
    info = get_tag_info(tag_id)
    if not info:
        return str(raw_frame)
    
    # PEOPLE
    if info.ui_category == 'people':
        people = getattr(raw_frame, 'people', [])
        return f"{len(people)} people"
    
    # IMAGE
    if info.ui_category == 'image':
        img_data = getattr(raw_frame, 'data', b'')
        mime = getattr(raw_frame, 'mime', '').split("/")[-1].upper()
        b = len(img_data)
        return f"image [{mime}] ({b:.0f} bytes)"
    
    # LYRICS (SYLT)
    if info.ui_category == 'lyrics':
        sylt_data = getattr(raw_frame, 'text', [])
        return f"{len(sylt_data)} lines"
    
    # Generic text
    if hasattr(raw_frame, 'text'):
        text = "".join(str(t).replace("\n", "\\") for t in raw_frame.text)
        return text
    
    return str(raw_frame)


# ============================================================================
# BULK OPERATIONS
# ============================================================================

def collect_tag_data(paths: list[str]) -> tuple[dict, dict, dict]:
    """
    Scan multiple files and collect tag statistics.
    
    Returns:
    - tag_counts: {tag_id: count}
    - tag_values: {tag_id: [unique_values]}
    - people_tags: {tag_id: set of (role, person) tuples}
    """
    tag_counts = {}
    tag_values = {}
    people_tags = {}
    
    for path in paths:
        try:
            audio = ID3(path)
            for tag_id in audio.keys():
                tag_counts[tag_id] = tag_counts.get(tag_id, 0) + 1
                
                frame = audio[tag_id]
                info = get_tag_info(tag_id)
                
                if info and info.ui_category == 'people':
                    people = getattr(frame, 'people', [])
                    if people:
                        if tag_id not in people_tags:
                            people_tags[tag_id] = set()
                        people_tags[tag_id].update(people)
                else:
                    if tag_id not in tag_values:
                        tag_values[tag_id] = []
                    
                    val = summarize_tag_value(tag_id, frame)
                    if val not in tag_values[tag_id]:
                        tag_values[tag_id].append(val)
        except Exception:
            pass
    
    # Convert sets to lists for JSON-safe output
    people_tags_list = {k: list(v) for k, v in people_tags.items()}
    
    return tag_counts, tag_values, people_tags_list


def apply_bulk_edit(
    audio: ID3,
    tag_id: str,
    operation: str,
    new_value: Any = None,
    new_tag_id: str | None = None
) -> bool:
    """
    Apply bulk operation to a single file.
    
    Operations:
    - 'set': set tag to new_value
    - 'rename': rename tag_id to new_tag_id (preserves type)
    - 'delete': remove tag
    """
    try:
        if operation == 'set':
            if new_value is None:
                return False
            audio.delall(tag_id)
            new_frame = create_frame(tag_id, new_value)
            if not new_frame:
                return False
            audio.add(new_frame)
            return True
        
        elif operation == 'rename':
            if not new_tag_id or new_tag_id == tag_id:
                return False
            
            # Validate UI category match (not changing widget type)
            old_info = get_tag_info(tag_id)
            new_info = get_tag_info(new_tag_id)
            if not old_info or not new_info or old_info.ui_category != new_info.ui_category:
                return False
            
            if tag_id not in audio:
                return False
            
            old_frame = audio.pop(tag_id)
            if not rename_frame(audio, old_frame, new_tag_id):
                audio.add(old_frame)  # Restore on failure
                return False
            return True
        
        elif operation == 'delete':
            audio.delall(tag_id)
            return True
        
        return False
    
    except Exception:
        return False


def apply_bulk_operation_to_files(
    file_paths: list[str],
    operation: str,
    tag_ids: list[str],
    target_value: Any = None,
    library: list | None = None
) -> tuple[int, int]:
    """
    Applies a verified tag operation across multiple files.
    Returns (success_count, fail_count).
    """
    success_count = 0
    fail_count = 0
    
    for path in file_paths:
        try:
            audio = ID3(path)
            changed = False
            
            for tag_id in tag_ids:
                if operation == 'set':
                    if apply_bulk_edit(audio, tag_id, 'set', target_value):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
                elif operation == 'rename':
                    if apply_bulk_edit(audio, tag_id, 'rename', new_tag_id=target_value):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
                elif operation == 'delete':
                    if apply_bulk_edit(audio, tag_id, 'delete'):
                        changed = True
                        success_count += 1
                    else:
                        fail_count += 1
            
            if changed:
                audio.save(v2_version=3)
                if library is not None:
                    from src.music_library import refresh_library_entry
                    try:
                        refresh_library_entry(library, path)
                    except Exception:
                        pass
        except Exception:
            fail_count += len(tag_ids)
    
    return success_count, fail_count