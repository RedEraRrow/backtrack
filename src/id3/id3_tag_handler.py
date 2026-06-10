"""
Unified ID3 tag registry, frame factory, and bulk operation logic.

Single source of truth for:
- Tag metadata (category, label, handling)
- Frame creation (handles all types correctly)
- Value prompting (selects correct widget per category)
- Bulk operations (with category validation)
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable
from mutagen.id3 import ID3
from mutagen.id3._frames import (
    TPE1, TPE2, TPE3, TPE4, TIT1, TIT2, TIT3, TALB, TDRC, TYER, TDRL,
    TDOR, TDTG, TDAT, TIME, TRCK, TPOS, TCON, COMM, USLT, SYLT, APIC,
    TXXX, WXXX, TMCL, TIPL, TLEN, TDLY, TSSE, USER, TOFN, TFLT, TPUB,
    TEXT, TDEN, TCMP, MVIN, MVNM, GRP1, TSRC, TextFrame
)
import time
from src.utils import prompt


@dataclass
class TagInfo:
    """Metadata for a single ID3 tag."""
    tag_id: str
    label: str
    category: str  # 'text', 'people', 'date', 'fraction', 'duration', 'lyrics', 'image'
    base_id: str  # For COMM[eng], returns 'COMM'; for TXXX:foo, returns 'TXXX'


# ============================================================================
# TAG REGISTRY
# ============================================================================

TAG_REGISTRY: dict[str, TagInfo] = {
    # Titles
    'TIT1': TagInfo('TIT1', 'Content Group', 'text', 'TIT1'),
    'TIT2': TagInfo('TIT2', 'Title', 'text', 'TIT2'),
    'TIT3': TagInfo('TIT3', 'Subtitle', 'text', 'TIT3'),
    
    # Artists
    'TPE1': TagInfo('TPE1', 'Artist', 'text', 'TPE1'),
    'TPE2': TagInfo('TPE2', 'Album Artist', 'text', 'TPE2'),
    'TPE3': TagInfo('TPE3', 'Conductor', 'text', 'TPE3'),
    'TPE4': TagInfo('TPE4', 'Remixer', 'text', 'TPE4'),
    
    # Credits
    'TMCL': TagInfo('TMCL', 'Musician Credits', 'people', 'TMCL'),
    'TIPL': TagInfo('TIPL', 'Involved People', 'people', 'TIPL'),
    
    # Album/Other
    'TALB': TagInfo('TALB', 'Album', 'text', 'TALB'),
    'TCON': TagInfo('TCON', 'Genre', 'text', 'TCON'),
    'TPUB': TagInfo('TPUB', 'Publisher', 'text', 'TPUB'),
    'TSRC': TagInfo('TSRC', 'ISRC', 'text', 'TSRC'),
    
    # Numbers
    'TRCK': TagInfo('TRCK', 'Track Number', 'fraction', 'TRCK'),
    'TPOS': TagInfo('TPOS', 'Disc Number', 'fraction', 'TPOS'),
    'MVIN': TagInfo('MVIN', 'Movement Number', 'fraction', 'MVIN'),
    
    # Dates
    'TDRC': TagInfo('TDRC', 'Recording Date', 'date', 'TDRC'),
    'TYER': TagInfo('TYER', 'Year', 'date', 'TYER'),
    'TDRL': TagInfo('TDRL', 'Release Date', 'date', 'TDRL'),
    'TDOR': TagInfo('TDOR', 'Original Release Date', 'date', 'TDOR'),
    'TDTG': TagInfo('TDTG', 'Tagging Date', 'date', 'TDTG'),
    'TDAT': TagInfo('TDAT', 'Date (DDMM)', 'date', 'TDAT'),
    'TIME': TagInfo('TIME', 'Time (HHMM)', 'date', 'TIME'),
    'TDEN': TagInfo('TDEN', 'Encoding Date', 'date', 'TDEN'),
    
    # Duration/Timing
    'TLEN': TagInfo('TLEN', 'Duration (ms)', 'duration', 'TLEN'),
    'TDLY': TagInfo('TDLY', 'Delay (ms)', 'duration', 'TDLY'),
    
    # Text/encoding
    'TSSE': TagInfo('TSSE', 'Encoder', 'text', 'TSSE'),
    'TOFN': TagInfo('TOFN', 'Original Filename', 'text', 'TOFN'),
    'TFLT': TagInfo('TFLT', 'File Type', 'text', 'TFLT'),
    
    # Comments/Lyrics
    'COMM': TagInfo('COMM', 'Comment', 'text', 'COMM'),
    'USLT': TagInfo('USLT', 'Unsynced Lyrics', 'multiline text', 'USLT'),
    'SYLT': TagInfo('SYLT', 'Synced Lyrics', 'lyrics', 'SYLT'),
    
    # Images
    'APIC': TagInfo('APIC', 'Album Art', 'image', 'APIC'),
    
    # Other common
    'MVNM': TagInfo('MVNM', 'Movement Name', 'text', 'MVNM'),
    'GRP1': TagInfo('GRP1', 'Grouping', 'text', 'GRP1'),
    'TCMP': TagInfo('TCMP', 'Compilation', 'text', 'TCMP'),
    'USER': TagInfo('USER', 'User Text', 'text', 'USER'),
    'TXXX': TagInfo('TXXX', 'Custom Field', 'text', 'TXXX'),
    'WXXX': TagInfo('WXXX', 'URL Link', 'text', 'WXXX'),

    # Sort orders
    'TSOA': TagInfo('TSOA', 'Album Sort Order', 'text', 'TSOA'),
    'TSO2': TagInfo('TSO2', 'Album Artist Sort Order', 'text', 'TSO2'),
    'TSOP': TagInfo('TSOP', 'Artist Sort Order', 'text', 'TSOP')
}


def get_tag_info(tag_id: str) -> TagInfo | None:
    """
    Get metadata for a tag.
    
    Handles:
    - Simple tags: 'TIT2' -> TagInfo
    - Framed tags: 'COMM[eng]' -> TagInfo for 'COMM'
    - Custom tags: 'TXXX:customname' -> TagInfo for 'TXXX'
    """
    if tag_id in TAG_REGISTRY:
        return TAG_REGISTRY[tag_id]
    
    # Extract base ID from framed/custom
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    if base_id in TAG_REGISTRY:
        return TAG_REGISTRY[base_id]
    
    return None


def get_tag_category(tag_id: str) -> str:
    """Get category of a tag (text, people, date, fraction, duration, lyrics, image)."""
    info = get_tag_info(tag_id)
    return info.category if info else 'text'

def parse_composite_tag_id(tag_id: str) -> tuple[str, str, str]:
    """
    Parses complex tag strings into (base_id, description, language).
    Supports:
      - TXXX:Transcription -> ('TXXX', 'Transcription', '')
      - TXXX:Transcription:eng -> ('TXXX', 'Transcription', 'eng')
      - TXXX::eng -> ('TXXX', '', 'eng')
      - COMM[eng] -> ('COMM', '', 'eng')
    """
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    desc_val = ''
    lang_val = ''

    # Handle legacy bracket notation first: COMM[eng]
    if '[' in tag_id and ']' in tag_id:
        lang_val = tag_id.split('[')[1].split(']')[0]
        return base_id, desc_val, lang_val

    # Handle colon notation: TXXX:Transcription:eng
    if ':' in tag_id:
        parts = tag_id.split(':')
        # parts[0] is the base_id (e.g., 'TXXX')
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
    
    Returns None if:
    - value is None
    - frame type is not supported
    - value format is invalid for the tag's category
    
    CRITICAL: Validates value format BEFORE frame creation to catch errors early.
    """
    if value is None:
        return None
    
    parsed_base, parsed_desc, parsed_lang = parse_composite_tag_id(tag_id)
    base_id = parsed_base
    category = get_tag_category(tag_id)
    
    # ========== TEXT FRAMES (TPE1, TIT2, TALB, etc.) ==========
    if base_id in ('TIT1', 'TIT2', 'TIT3', 'TPE1', 'TPE2', 'TPE3', 'TPE4',
                   'TALB', 'TCON', 'TPUB', 'TSRC', 'TSSE', 'TOFN', 'TFLT',
                   'MVNM', 'GRP1', 'TCMP', 'USER', 'TLEN', 'TDLY', 
                   'TXXX', 'WXXX', 'COMM', 'USLT'):
        text_val = str(value).strip()
        if not text_val:
            return None
        
        # Map to correct frame class
        frame_class = {
            'TIT1': TIT1, 'TIT2': TIT2, 'TIT3': TIT3,
            'TPE1': TPE1, 'TPE2': TPE2, 'TPE3': TPE3, 'TPE4': TPE4,
            'TALB': TALB, 'TCON': TCON, 'TPUB': TPUB, 'TSRC': TSRC,
            'TSSE': TSSE, 'TOFN': TOFN, 'TFLT': TFLT,
            'MVNM': MVNM, 'GRP1': GRP1, 'TCMP': TCMP,
            'USER': USER, 'TLEN': TLEN, 'TDLY': TDLY,
            'TXXX': TXXX, 'WXXX': WXXX, 'COMM': COMM, 'USLT': USLT
        }.get(base_id, TEXT)
        if frame_class in [TXXX, WXXX]:
            return frame_class(encoding=3, desc=parsed_desc, text=text_val)
        
        if frame_class in [COMM, USLT]:
            clean_lang = str(parsed_lang).strip() if parsed_lang else 'eng'
            if len(clean_lang) != 3:
                clean_lang = 'eng'
                
            return frame_class(encoding=3, lang=clean_lang, desc=parsed_desc, text=text_val)
        return frame_class(encoding=3, text=[text_val])
    
    # ========== DATE FRAMES (TDRC, TYER, TDRL, etc.) ==========
    if base_id in ('TDRC', 'TYER', 'TDRL', 'TDOR', 'TDTG', 'TDAT', 'TIME', 'TDEN'):
        date_val = str(value).strip()
        if not date_val:
            return None
        
        # Validate basic date format (YYYY, YYYY-MM, YYYY-MM-DD, etc.)
        if not any(c.isdigit() for c in date_val):
            return None
        
        frame_class = {
            'TDRC': TDRC, 'TYER': TYER, 'TDRL': TDRL,
            'TDOR': TDOR, 'TDTG': TDTG, 'TDAT': TDAT,
            'TIME': TIME, 'TDEN': TDEN,
        }.get(base_id, TEXT)
        return frame_class(encoding=3, text=[date_val])
    
    # ========== FRACTION FRAMES (TRCK, TPOS, MVIN) ==========
    if base_id in ('TRCK', 'TPOS', 'MVIN'):
        frac_val = str(value).strip()
        if not frac_val:
            return None
        
        # Accept formats: "3", "3/12", etc.
        if not any(c.isdigit() for c in frac_val):
            return None
        
        frame_class = {
            'TRCK': TRCK, 'TPOS': TPOS, 'MVIN': MVIN,
        }.get(base_id, TEXT)
        return frame_class(encoding=3, text=[frac_val])
    
    # ========== SYNCED LYRICS (SYLT) ==========
    # SYLT requires list of (text, timestamp_ms) tuples - NOT supported in simple edit
    # Must use save_sylt_entries() for proper synced import
    if base_id == 'SYLT':
        return None  # SYLT not supported in simple create_frame - use dedicated import
    # ========== PEOPLE FRAMES (TMCL, TIPL) ==========
    if base_id in ('TMCL', 'TIPL'):
        # value should be list of (role, person) tuples
        if isinstance(value, list) and value:
            people_list = []
            for item in value:
                if isinstance(item, (tuple, list)) and len(item) == 2:
                    people_list.append((str(item[0]).strip(), str(item[1]).strip()))
            
            if not people_list:
                return None
            
            frame_class = TMCL if base_id == 'TMCL' else TIPL
            return frame_class(encoding=3, people=people_list)
        
        return None
    
    # Unknown tag type
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
    """Rename a frame while preserving type and value, handling composite IDs."""
    old_id = None
    for key in audio_obj.keys():
        if audio_obj[key] is old_frame:
            old_id = key
            break
    
    if not old_id:
        return False
    
    if get_tag_category(old_id) != get_tag_category(new_id):
        return False
    
    if hasattr(old_frame, 'people'):
        value = old_frame.people
    elif hasattr(old_frame, 'text'):
        value = old_frame.text[0] if old_frame.text else None
    else:
        value = str(old_frame)
    
    if value is None:
        return False
    
    # Let create_frame do the parsing heavy lifting internally now!
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
# VALUE PROMPTING (widget selection per category)
# ============================================================================

def prompt_for_value(tag_id: str, current_value: Any = None, initial_people: list | None = None) -> Any | None:
    """
    Auto-select correct widget based on tag category.
    
    Returns value ready for create_frame(), or None if cancelled.
    """
    info = get_tag_info(tag_id)
    category = info.category if info else 'text'
    label = info.label if info else tag_id
    
    # ========== TEXT ==========
    if category == 'text':
        default_val = str(current_value) if current_value else ""
        return prompt.text(f"{label}:", default=default_val)
    
    # ========== PEOPLE ==========
    if category == 'people':
        people = initial_people if initial_people else []
        if isinstance(current_value, list):
            people = [(str(r), str(n)) for r, n in current_value]
        
        while True:
            action = prompt.select(
                f"{label}:",
                choices=["Edit in List", "Import from Text", "Done" if people else "Done (empty)"]
            )
            
            if action == "Done (empty)" or action == "Done":
                return people if people else None
            
            if action == "Edit in List":
                people = prompt.list_edit(
                    "People (role: person):",
                    people,
                    headers=("Role", "Person")
                )
            
            elif action == "Import from Text":
                text_input = prompt.text("Paste roles and names (one per line, format: role: person):")
                if text_input:
                    for line in text_input.splitlines():
                        if ':' in line:
                            role, person = line.split(':', 1)
                            if people is not None:
                                people.append((role.strip(), person.strip()))
                            else:
                                continue
    
    # ========== DATE ==========
    if category == 'date':
        default_val = str(current_value) if current_value else ""
        return prompt.text(
            f"{label} (YYYY, YYYY-MM-DD, etc.):",
            default=default_val
        )
    
    # ========== FRACTION (TRACK/DISC/MOVEMENT) ==========
    if category == 'fraction':
        default_val = str(current_value) if current_value else ""
        return prompt.text(
            f"{label} (format: X or X/Total):",
            default=default_val
        )
    
    # ========== DURATION ==========
    if category == 'duration':
        default_val = str(current_value) if current_value else ""
        return prompt.text(
            f"{label} (milliseconds):",
            default=default_val
        )
    
    # ========== LYRICS ==========
    if category == 'multiline text':
        # USLT - multiline text
        default_val = str(current_value) if current_value else ""
        return prompt.system_editor_edit(initial_text=default_val)
    
    # ========== IMAGE ========== # TODO: Implement properly
    if category == 'image':
        print("Use 'Manage' action to edit album art.")
        time.sleep(1)
        return None
    
    # Fallback
    return prompt.text(f"{label}:")


# ============================================================================
# TAG SUMMARY (for display)
# ============================================================================

def summarize_tag_value(tag_id: str, raw_frame) -> str:
    """
    Get short display summary of tag value.
    Max 100 chars.
    """
    category = get_tag_category(tag_id)
    
    if category == 'people':
        people = getattr(raw_frame, 'people', [])
        return f"{len(people)} people"
    
    if category == 'image':
        img_data = getattr(raw_frame, 'data', b'')
        mime = getattr(raw_frame, 'mime', '').split("/")[-1].upper()
        b = len(img_data)
        info = f"{b:.0f} bytes"
        return f"image [{mime}] ({info})"
    
    if category == 'lyrics' and tag_id.startswith('SYLT'):
        sylt_data = getattr(raw_frame, 'text', [])
        l = len(sylt_data)
        return f"{l} lines"
    
    # Generic text
    if hasattr(raw_frame, 'text'):
        text = "".join(str(t).replace("\n","\\") for t in raw_frame.text)
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
                category = get_tag_category(tag_id)
                
                if category == 'people':
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
    
    CRITICAL: Validates operation before execution.
    Returns True if successful, False otherwise.
    """
    try:
        # ========== SET ==========
        if operation == 'set':
            if new_value is None:
                return False
            # TODO: Fix for tags with descriptions
            audio.delall(tag_id)
            new_frame = create_frame(tag_id, new_value)
            if not new_frame:
                return False
            audio.add(new_frame)
            return True
        
        # ========== RENAME ==========
        elif operation == 'rename':
            if not new_tag_id or new_tag_id == tag_id:
                return False
            
            # Validate category match
            old_category = get_tag_category(tag_id)
            new_category = get_tag_category(new_tag_id)
            if old_category != new_category:
                return False
            
            if tag_id not in audio:
                return False
            
            old_frame = audio.pop(tag_id)
            if not rename_frame(audio, old_frame, new_tag_id):
                audio.add(old_frame)  # Restore on failure
                return False
            return True
        
        # ========== DELETE ==========
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
    Applies a verified tag operation ('set', 'rename', 'delete') across multiple files.
    Handles ID3 saving, exception isolation, and library state synchronization.
    Returns a tuple of (success_count, fail_count).
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