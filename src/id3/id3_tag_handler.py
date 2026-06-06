"""
Unified ID3 tag handling and frame factory.

Centralizes tag type detection, frame creation, value prompting, and editing logic.
Eliminates duplication between id3_browser.py and bulk_id3_manager.py.
"""
from __future__ import annotations
from typing import Any
from dataclasses import dataclass
from mutagen.id3 import ID3
from mutagen.id3._frames import (
    USLT, COMM, SYLT, APIC, TMCL, TIPL, TextFrame,
)
from src.utils import prompt


# ============================================================================
# TAG CATEGORIES & METADATA
# ============================================================================

@dataclass
class TagInfo:
    """Metadata about an ID3 tag type."""
    base_id: str
    category: str  # 'people', 'date', 'fraction', 'duration', 'text', 'lyrics', 'image'
    label: str
    headers: tuple[str, ...] | None = None  # For list_edit widgets


TAG_REGISTRY = {
    # People tags
    'TMCL': TagInfo('TMCL', 'people', 'Musicians/Credits', ('ROLE', 'NAME')),
    'TIPL': TagInfo('TIPL', 'people', 'Producers/Credits', ('JOB', 'NAME')),
    
    # Fraction tags (track/disc/movement number)
    'TRCK': TagInfo('TRCK', 'fraction', 'Track Number'),
    'TPOS': TagInfo('TPOS', 'fraction', 'Disc Number'),
    'MVIN': TagInfo('MVIN', 'fraction', 'Movement Number'),
    
    # Date tags
    'TDRC': TagInfo('TDRC', 'date', 'Recording Date'),
    'TYER': TagInfo('TYER', 'date', 'Year'),
    'TDRL': TagInfo('TDRL', 'date', 'Release Date'),
    'TDOR': TagInfo('TDOR', 'date', 'Original Release Date'),
    'TDTG': TagInfo('TDTG', 'date', 'Tagging Date'),
    'TDAT': TagInfo('TDAT', 'date', 'Date'),
    'TIME': TagInfo('TIME', 'date', 'Time'),
    
    # Duration/time tags
    'TLEN': TagInfo('TLEN', 'duration', 'Track Duration'),
    'TDLY': TagInfo('TDLY', 'duration', 'Playback Delay'),
    
    # Lyrics
    'USLT': TagInfo('USLT', 'lyrics', 'Unsynced Lyrics'),
    'SYLT': TagInfo('SYLT', 'lyrics', 'Synced Lyrics'),
    
    # Image
    'APIC': TagInfo('APIC', 'image', 'Album Art'),
    
    # Common text
    'TIT2': TagInfo('TIT2', 'text', 'Title'),
    'TPE1': TagInfo('TPE1', 'text', 'Artist'),
    'TPE2': TagInfo('TPE2', 'text', 'Album Artist'),
    'TALB': TagInfo('TALB', 'text', 'Album'),
    'TCON': TagInfo('TCON', 'text', 'Genre'),
    'COMM': TagInfo('COMM', 'text', 'Comment'),
}


def get_tag_info(tag_id: str) -> TagInfo | None:
    """Get metadata for a tag ID, handling bracket/colon syntax.
    
    Examples:
        'TRCK' -> TagInfo(...)
        'COMM[eng]' -> TagInfo(...)
        'TXXX:Custom' -> None (custom text, treat as generic text)
    """
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    return TAG_REGISTRY.get(base_id)


def get_tag_category(tag_id: str) -> str:
    """Get category string for a tag ('people', 'date', 'text', etc.)"""
    info = get_tag_info(tag_id)
    if info:
        return info.category
    # Default to text for unknown tags
    return 'text'


# ============================================================================
# FRAME CREATION
# ============================================================================

def create_frame(tag_id: str, value: Any) -> Any:
    """
    Universal frame factory. Handles all tag types.
    
    Args:
        tag_id: Full tag ID (e.g. 'TRCK', 'COMM[eng]', 'TXXX:CustomField')
        value: The value(s) to assign.
               - str for text tags
               - list of tuples (role, name) for people tags
               - dict with keys like 'current'/'total' for fractions
               - bytes for images (not typical; see create_apic_frame)
    
    Returns:
        ID3 frame object, or None if creation failed.
    """
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    lang = 'eng'
    desc = ''
    
    # Extract language and description from tag ID syntax
    if '[' in tag_id and ']' in tag_id:
        lang = tag_id.split('[')[1].split(']')[0]
    if ':' in tag_id:
        desc = tag_id.split(':', 1)[1].split('[')[0]
    
    info = get_tag_info(tag_id)
    
    # People tags: expect list of (role, name) tuples
    if base_id == 'TMCL':
        if isinstance(value, list):
            return TMCL(encoding=3, people=value)
        return None
    
    if base_id == 'TIPL':
        if isinstance(value, list):
            return TIPL(encoding=3, people=value)
        return None
    
    # Lyrics
    if base_id == 'USLT':
        if isinstance(value, str):
            return USLT(encoding=3, lang=lang, desc=desc, text=value)
        return None
    
    if base_id == 'SYLT':
        # SYLT expects list of (text, timestamp_ms) tuples; not typical for bulk ops
        return None
    
    # Comments (special text frame)
    if base_id == 'COMM':
        if isinstance(value, str):
            return COMM(encoding=3, lang=lang, desc=desc, text=[value])
        return None
    
    # Generic text frame (covers TRCK, TPOS, TIT2, TPE1, etc.)
    if base_id in TAG_REGISTRY:
        from mutagen.id3 import Frames as _Frames
        frame_cls = _Frames.get(base_id)
        if frame_cls and issubclass(frame_cls, TextFrame):
            return frame_cls(encoding=3, text=[str(value)])
    
    # Unknown tag: try generic text
    from mutagen.id3 import Frames as _Frames
    frame_cls = _Frames.get(base_id)
    if frame_cls:
        try:
            return frame_cls(encoding=3, text=[str(value)])
        except Exception:
            return None
    
    return None


def create_apic_frame(
    image_data: bytes,
    mime: str = 'image/jpeg',
    pic_type: int = 3,
    desc: str = ''
) -> APIC:
    """Create an APIC (album art) frame."""
    return APIC(encoding=3, mime=mime, type=pic_type, desc=desc, data=image_data)


def rename_frame(audio_obj: ID3, old_frame: Any, new_id: str) -> bool:
    """Rename a frame to a new ID."""
    try:
        base_id = new_id.split('[')[0].split(':')[0].upper()
        lang = 'eng'
        if '[' in new_id and ']' in new_id:
            lang = new_id.split('[')[1].split(']')[0]
        
        old_text = getattr(old_frame, 'text', [''])[0] if hasattr(old_frame, 'text') else ''
        
        if base_id == 'COMM':
            new_frame = COMM(encoding=3, lang=lang, desc='', text=[old_text])
        elif base_id == 'USLT':
            new_frame = USLT(encoding=3, lang=lang, desc='', text=old_text)
        else:
            from mutagen.id3 import Frames
            frame_cls = Frames.get(base_id)
            if frame_cls is None:
                return False
            new_frame = frame_cls(encoding=3, text=[old_text])
        
        audio_obj.add(new_frame)
        return True
    except Exception:
        return False


# ============================================================================
# VALUE PROMPTING (widget selection based on tag type)
# ============================================================================

def prompt_for_value(
    tag_id: str,
    current_value: Any | None = None,
    initial_people: list | None = None
) -> Any | None:
    """
    Prompt user for a value based on tag type.
    
    Returns the appropriate value for the tag (str, list of tuples, etc.)
    or None if user cancels.
    """
    info = get_tag_info(tag_id)
    category = info.category if info else 'text'
    
    # People tags
    if category == 'people':
        headers: tuple[str, ...] = info.headers if info and info.headers else ('ROLE', 'NAME')
        label = info.label if info else 'People'
        return prompt.list_edit(f"Edit {label}:", initial_people, headers)
    
    # Date tags
    if category == 'date':
        return prompt.calendar_select(f"Select {info.label.lower()}:")
    
    # Fraction tags (track/disc/movement)
    if category == 'fraction':
        label_map = {
            'TRCK': 'track',
            'TPOS': 'disc',
            'MVIN': 'movement',
        }
        base_id = info.base_id if info else 'TRCK'
        label = label_map.get(base_id, 'value')
        result = prompt.fraction_edit(f"Edit {label}s:")
        if result:
            if label == 'track':
                return f"{result['current']}/{result['total']}" if result['total'] else result['current']
            elif label == 'disc':
                return f"{result['current']}/{result['total']}" if result['total'] else result['current']
            elif label == 'movement':
                return f"{result['current']}/{result['total']}" if result['total'] else result['current']
        return None
    
    # Duration tags
    if category == 'duration':
        label = info.label if info else 'duration'
        time_str = prompt.time_edit(f"Edit {label.lower()}:")
        if time_str:
            # Convert HH:MM:SS.mmm to milliseconds
            parts = time_str.split(':')
            h = int(parts[0])
            m = int(parts[1])
            sec_parts = parts[2].split('.')
            s = int(sec_parts[0])
            ms = int((sec_parts[1] + "000")[:3]) if len(sec_parts) > 1 else 0
            return str(h * 3600000 + m * 60000 + s * 1000 + ms)
        return None
    
    # Lyrics (unsynced only; synced requires interactive sync tool)
    if category == 'lyrics' and info and info.base_id == 'USLT':
        return prompt.text(f"Edit {info.label}:")
    
    # Default: text input
    label = info.label if info else 'value'
    return prompt.text(f"Edit {label}:")


# ============================================================================
# TAG SUMMARY & DISPLAY
# ============================================================================

def summarize_tag_value(tag_id: str, raw_frame: Any) -> str:
    """Return a display-friendly summary of a tag's value."""
    category = get_tag_category(tag_id)
    
    if category == 'people':
        return f"{len(getattr(raw_frame, 'people', []))} people"
    
    if category == 'lyrics' and tag_id.startswith('SYLT'):
        return "synced lyrics"
    
    if category == 'image':
        return "image"
    
    # Generic text summary
    if hasattr(raw_frame, 'text'):
        text = " / ".join(str(t) for t in raw_frame.text)
        return text[:40] + ("…" if len(text) > 40 else "")
    
    return str(raw_frame)[:40]


def collect_tag_data(paths: list) -> tuple[dict, dict, dict]:
    """
    Scan files and collect tag metadata.
    
    Returns: (tag_counts, tag_values, people_tags)
    - tag_counts: Counter of how many files have each tag
    - tag_values: dict of tag_id -> [display_values]
    - people_tags: dict of tag_id -> [people_lists] (for TMCL/TIPL)
    """
    from collections import Counter
    
    tag_counts = Counter()
    tag_values = {}
    people_tags = {}
    
    for path in paths:
        try:
            audio = ID3(path)
            tag_counts.update(audio.keys())
            
            for tag_id in audio.keys():
                raw = audio[tag_id]
                summary = summarize_tag_value(tag_id, raw)
                tag_values.setdefault(tag_id, []).append(summary)
                
                # Preserve people data separately
                if tag_id.startswith(('TMCL', 'TIPL')):
                    people_tags.setdefault(tag_id, []).append(
                        getattr(raw, 'people', [])
                    )
        except Exception:
            continue
    
    return tag_counts, tag_values, people_tags


# ============================================================================
# BULK OPERATIONS
# ============================================================================

def apply_bulk_edit(
    audio: ID3,
    tag_id: str,
    operation: str,  # 'set', 'delete', 'rename'
    new_value: Any | None = None,
    new_tag_id: str | None = None
) -> bool:
    """
    Apply a bulk operation to a single tag in a file.
    
    Args:
        audio: ID3 object
        tag_id: Tag to operate on
        operation: 'set', 'delete', 'rename'
        new_value: Value to set (for 'set' operation)
        new_tag_id: New tag ID (for 'rename' operation)
    
    Returns:
        True if changed, False otherwise.
    """
    if tag_id not in audio:
        return False
    
    if operation == 'delete':
        audio.pop(tag_id)
        return True
    
    if operation == 'rename':
        if not new_tag_id:
            return False
        old_frame = audio.pop(tag_id)
        return rename_frame(audio, old_frame, new_tag_id)
    
    if operation == 'set':
        if new_value is None:
            return False
        audio.delall(tag_id)
        new_frame = create_frame(tag_id, new_value)
        if new_frame:
            audio.add(new_frame)
            return True
        return False
    
    return False