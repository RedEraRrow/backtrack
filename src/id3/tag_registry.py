"""
================================================================================
ID3v2.4 COMPLETE TAG REGISTRY - WITH MUTAGEN INTEGRATION
================================================================================

Generated from official ID3v2.4.0 specification + mutagen library.
Used across backtrack: id3_tag_handler, id3_browser, bulk_id3_manager

SINGLE SOURCE OF TRUTH for:
  - Complete tag metadata (name, frame type, format, single_only)
  - Mutagen frame classes (direct instantiation)
  - Category mapping (for UI widget selection)
  - Frame type classification

================================================================================
"""

from dataclasses import dataclass
from typing import Dict, Type, Literal, Any, Optional


from mutagen.id3 import *
from mutagen.id3._frames import *

# ============================================================================
# CATEGORY TYPES - used for widget selection in backtrack UI
# ============================================================================

UICategory = Literal[
    'text',              # Simple text input (TIT2, TPE1, TALB, etc.)
    'multiline text',    # Multi-line text (USLT, COMM)
    'timestamp',         # Date picker (TDRC, TDEN, TDOR, etc.)
    'date',
    'date/time',
    'year',
    'time',
    'fraction',          # X or X/Total (TRCK, TPOS, MVIN)
    'duration',          # Milliseconds (TLEN, TDLY)
    'people',            # List of (role/instrument, person) tuples (TMCL, TIPL)
    'image',             # APIC - album art
    'lyrics',            # SYLT - synchronized lyrics
]


# ============================================================================
# DATA CLASS
# ============================================================================

@dataclass
class TagInfo:
    """
    Complete information for a single ID3v2.4 tag with mutagen integration.
    
    Attributes:
        tag_id (str):
            Frame identifier code (e.g., 'TIT2', 'APIC')
        
        name (str):
            Human-readable full name of the frame
        
        frame_type (str):
            Type of frame: TEXT, BINARY, URL, NUMERIC, FRACTIONAL, TIMESTAMP, LIST
        
        format_spec (str):
            Format specification (TEXT_UTF8, BINARY, ISO8601, etc.)
        
        official_category (str):
            Logical category from ID3 spec (IDENTIFICATION, RIGHTS_LICENSE, etc.)
        
        ui_category (UICategory):
            Widget type for backtrack UI (text, date, fraction, people, image, etc.)
        
        single_only (bool):
            True if only one instance of this frame is allowed per tag.
            False if multiple frames with different descriptors are allowed.
        
        mutagen_class (Type):
            The corresponding mutagen frame class for this tag
            (e.g., mutagen.id3.TIT2 for 'TIT2')
        
        description (str):
            Optional detailed description of frame purpose
    
    Methods:
        create_frame():
            Factory method to instantiate the mutagen frame
    """
    tag_id: str
    name: list[str]
    frame_type: str
    format_spec: str
    official_category: str
    ui_category: UICategory
    single_only: bool
    mutagen_class: Type
    description: str = ""
    
    def create_frame(self, *args, **kwargs) -> Any:
        """
        Create an instance of the mutagen frame class.
        """
        return self.mutagen_class(*args, **kwargs)


# ============================================================================
# COMPLETE TAG REGISTRY (94 tags)
# ============================================================================

TAG_REGISTRY: Dict[str, TagInfo] = {
    
    # ========================================================================
    # TEXT FRAMES (T***)
    # ========================================================================
    
    'TIT1': TagInfo('TIT1', ['Content group description', 'Work name (classical)'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TIT1),
    
    'TIT2': TagInfo('TIT2', ['Title', 'Songname', 'Content description'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', True, TIT2),
    
    'TIT3': TagInfo('TIT3', ['Subtitle', 'Description refinement'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TIT3),
    
    'TALB': TagInfo('TALB', ['Album', 'Movie', 'Show title'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', True, TALB),
    
    'TOAL': TagInfo('TOAL', ['Original album', 'Original Movie', 'Original show title'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TOAL),
    
    'TPE1': TagInfo('TPE1', ['Lead performer(s)', 'Soloist(s)'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TPE1),
    
    'TPE2': TagInfo('TPE2', ['Band', 'Orchestra', 'Accompaniment'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TPE2),
    
    'TPE3': TagInfo('TPE3', ['Conductor', 'Performer refinement'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TPE3),
    
    'TPE4': TagInfo('TPE4', ['Interpreted by', 'Remixed by', 'Modified by'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TPE4),
    
    'TOPE': TagInfo('TOPE', ['Original artist(s)', 'Original performer(s)'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TOPE),
    
    'TEXT': TagInfo('TEXT', ['Lyricist', 'Text writer'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TEXT),
    
    'TOLY': TagInfo('TOLY', ['Original lyricist(s)', 'Original text writer(s)'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TOLY),
    
    'TCOM': TagInfo('TCOM', ['Composer'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TCOM),
    
    'TENC': TagInfo('TENC', ['Encoded by'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TENC),
    
    'TBPM': TagInfo('TBPM', ['Beats per minute'],
                    'NUMERIC', 'INT_BIG', 'DERIVED_SUBJECTIVE', 'text', True, TBPM),
    
    'TLEN': TagInfo('TLEN', ['Length (milliseconds)'],
                    'NUMERIC', 'INT_BIG', 'DERIVED_SUBJECTIVE', 'duration', True, TLEN),
    
    'TKEY': TagInfo('TKEY', ['Initial key'],
                    'TEXT', 'TEXT_UTF8', 'DERIVED_SUBJECTIVE', 'text', False, TKEY),
    
    'TLAN': TagInfo('TLAN', ['Language(s)'],
                    'LIST', 'LIST_STRING', 'DERIVED_SUBJECTIVE', 'text', False, TLAN),
    
    'TCON': TagInfo('TCON', ['Content type', 'Genre'],
                    'LIST', 'LIST_STRING', 'DERIVED_SUBJECTIVE', 'text', True, TCON),
    
    'TFLT': TagInfo('TFLT', ['File type'],
                    'TEXT', 'TEXT_UTF8', 'DERIVED_SUBJECTIVE', 'text', False, TFLT),
    
    'TMED': TagInfo('TMED', ['Media type'],
                    'TEXT', 'TEXT_UTF8', 'DERIVED_SUBJECTIVE', 'text', False, TMED),
    
    'TMOO': TagInfo('TMOO', ['Mood'],
                    'TEXT', 'TEXT_UTF8', 'DERIVED_SUBJECTIVE', 'text', False, TMOO),
    
    'TCOP': TagInfo('TCOP', ['Copyright message'],
                    'TEXT', 'TEXT_UTF8', 'RIGHTS_LICENSE', 'text', True, TCOP),
    
    'TPRO': TagInfo('TPRO', ['Produced notice'],
                    'TEXT', 'TEXT_UTF8', 'RIGHTS_LICENSE', 'text', True, TPRO),
    
    'TPUB': TagInfo('TPUB', ['Publisher'],
                    'TEXT', 'TEXT_UTF8', 'RIGHTS_LICENSE', 'text', True, TPUB),
    
    'TOWN': TagInfo('TOWN', ['File owner', 'Licensee'],
                    'TEXT', 'TEXT_UTF8', 'RIGHTS_LICENSE', 'text', True, TOWN),
    
    'TRSN': TagInfo('TRSN', ['Internet radio station name'],
                    'TEXT', 'TEXT_UTF8', 'RIGHTS_LICENSE', 'text', False, TRSN),
    
    'TRSO': TagInfo('TRSO', ['Internet radio station owner'],
                    'TEXT', 'TEXT_UTF8', 'RIGHTS_LICENSE', 'text', False, TRSO),
    
    'TOFN': TagInfo('TOFN', ['Original filename'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', False, TOFN),
    
    'TDLY': TagInfo('TDLY', ['Playlist delay (milliseconds)'],
                    'NUMERIC', 'INT_BIG', 'OTHER_TEXT', 'duration', False, TDLY),
    
    'TSSE': TagInfo('TSSE', ['Software and settings used for encoding', 'Hardware and settings used for encoding'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', True, TSSE),
    
    'TSOA': TagInfo('TSOA', ['Album sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOA),
    
    'TSOP': TagInfo('TSOP', ['Performer sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOP),
    
    'TSOT': TagInfo('TSOT', ['Title sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOT),
    
    'TSO2': TagInfo('TSO2', ['Album artist sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSO2),
    
    'TSRC': TagInfo('TSRC', ['ISRC (International Standard Recording Code)'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', True, TSRC),
    
    'TXXX': TagInfo('TXXX', ['User defined text information'],
                    'TEXT', 'TEXT_UTF8', 'USER_DEFINED', 'text', False, TXXX),
    
    'COMM': TagInfo('COMM', ['Comments'],
                    'TEXT', 'TEXT_UTF8_LANG', 'SPECIAL_TEXT', 'multiline text', False, COMM),
    
    'USLT': TagInfo('USLT', ['Unsynchronised lyric', 'Unsynchronised text transcription'],
                    'TEXT', 'TEXT_UTF8_LANG', 'SPECIAL_TEXT', 'multiline text', False, USLT),
    
    'USER': TagInfo('USER', ['Terms of use'],
                    'TEXT', 'TEXT_UTF8_LANG', 'SPECIAL_TEXT', 'text', False, USER),
    
    'OWNE': TagInfo('OWNE', ['Ownership frame'],
                    'TEXT', 'TEXT_UTF8', 'SPECIAL_TEXT', 'text', True, OWNE),
    
    'MVNM': TagInfo('MVNM', ['Movement name (classical)'],
                    'TEXT', 'TEXT_UTF8', 'CLASSICAL', 'text', False, MVNM),
    
    'GRP1': TagInfo('GRP1', ['Grouping (iTunes)'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', False, GRP1),
    
    'TSST': TagInfo('TSST', ['Set subtitle'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', False, TSST),
    
    # ========================================================================
    # TIMESTAMP FRAMES (TD**)
    # ========================================================================
    
    'TDEN': TagInfo('TDEN', ['Encoding time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDEN),
    
    'TDOR': TagInfo('TDOR', ['Original release time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDOR),
    
    'TDRC': TagInfo('TDRC', ['Recording time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDRC),
    
    'TDRL': TagInfo('TDRL', ['Release time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDRL),
    
    'TDTG': TagInfo('TDTG', ['Tagging time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDTG),
    
    # ========================================================================
    # LEGACY FRAMES (ID3v2.3)
    # ========================================================================
    
    'TORY': TagInfo('TORY', ['Original release year (legacy, ID3v2.3)'],
                    'YEAR', 'YYYY', 'LEGACY', 'year', False, TORY),
    
    'TDAT': TagInfo('TDAT', ['Date (legacy, ID3v2.3)'],
                    'DATE', 'DDMM', 'LEGACY', 'date', False, TDAT),
    
    'TIME': TagInfo('TIME', ['Time (legacy, ID3v2.3)'],
                    'TIME', 'HHMM', 'LEGACY', 'time', False, TIME),
    
    'TRDA': TagInfo('TRDA', ['Recording dates (legacy, ID3v2.3)'],
                    'DATE', 'DDMM', 'LEGACY', 'date', False, TRDA),
    
    # ========================================================================
    # FRACTIONAL FRAMES
    # ========================================================================
    
    'TRCK': TagInfo('TRCK', ['Track number', 'Position in set'],
                    'FRACTIONAL', 'FRACTIONAL', 'FRACTIONAL', 'fraction', True, TRCK),
    
    'TPOS': TagInfo('TPOS', ['Part of a set'],
                    'FRACTIONAL', 'FRACTIONAL', 'FRACTIONAL', 'fraction', True, TPOS),
    
    'MVIN': TagInfo('MVIN', ['Movement number (classical)'],
                    'FRACTIONAL', 'FRACTIONAL', 'FRACTIONAL', 'fraction', False, MVIN),
    
    # ========================================================================
    # LIST FRAMES
    # ========================================================================
    
    'TIPL': TagInfo('TIPL', ['Involved people list'],
                    'LIST', 'LIST_KV', 'LIST', 'people', False, TIPL),
    
    'TMCL': TagInfo('TMCL', ['Musician credits list'],
                    'LIST', 'LIST_KV', 'LIST', 'people', False, TMCL),
    
    # ========================================================================
    # URL FRAMES (W***)
    # ========================================================================
    
    'WCOM': TagInfo('WCOM', ['Commercial information'],
                    'URL', 'URL', 'URL', 'text', False, WCOM),
    
    'WCOP': TagInfo('WCOP', ['Copyright', 'Legal information'],
                    'URL', 'URL', 'URL', 'text', True, WCOP),
    
    'WOAF': TagInfo('WOAF', ['Official audio file webpage'],
                    'URL', 'URL', 'URL', 'text', True, WOAF),
    
    'WOAR': TagInfo('WOAR', ['Official artist', 'Official performer webpage'],
                    'URL', 'URL', 'URL', 'text', False, WOAR),
    
    'WOAS': TagInfo('WOAS', ['Official audio source webpage'],
                    'URL', 'URL', 'URL', 'text', True, WOAS),
    
    'WORS': TagInfo('WORS', ['Official Internet radio station homepage'],
                    'URL', 'URL', 'URL', 'text', True, WORS),
    
    'WPAY': TagInfo('WPAY', ['Payment'],
                    'URL', 'URL', 'URL', 'text', True, WPAY),
    
    'WPUB': TagInfo('WPUB', ['Publishers official webpage'],
                    'URL', 'URL', 'URL', 'text', True, WPUB),
    
    'WXXX': TagInfo('WXXX', ['User defined URL'],
                    'URL', 'TEXT_UTF8+URL', 'USER_DEFINED', 'text', False, WXXX),
    
    # ========================================================================
    # BINARY FRAMES
    # ========================================================================
    
    'UFID': TagInfo('UFID', ['Unique file identifier'],
                    'BINARY', 'BINARY', 'IDENTIFICATION', 'text', False, UFID),
    
    'MCDI': TagInfo('MCDI', ['Music CD identifier'],
                    'BINARY', 'BINARY', 'IDENTIFICATION', 'text', True, MCDI),
    
    'APIC': TagInfo('APIC', ['Attached picture'],
                    'BINARY', 'BINARY', 'PICTURE', 'image', False, APIC),
    
    'GEOB': TagInfo('GEOB', ['General encapsulated object'],
                    'BINARY', 'BINARY', 'PICTURE', 'text', False, GEOB),
    
    'PRIV': TagInfo('PRIV', ['Private frame'],
                    'BINARY', 'BINARY', 'ENCRYPTION', 'text', False, PRIV),
    
    'AENC': TagInfo('AENC', ['Audio encryption'],
                    'BINARY', 'BINARY', 'ENCRYPTION', 'text', False, AENC),
    
    'ENCR': TagInfo('ENCR', ['Encryption method registration'],
                    'BINARY', 'BINARY', 'ENCRYPTION', 'text', False, ENCR),
    
    'GRID': TagInfo('GRID', ['Group identification registration'],
                    'BINARY', 'BINARY', 'ENCRYPTION', 'text', False, GRID),
    
    'SIGN': TagInfo('SIGN', ['Signature frame'],
                    'BINARY', 'BINARY', 'ENCRYPTION', 'text', False, SIGN),
    
    'SYLT': TagInfo('SYLT', ['Synchronized lyrics', 'Synchronized text'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'lyrics', False, SYLT),
    
    'SYTC': TagInfo('SYTC', ['Synchronized tempo codes'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, SYTC),
    
    'ETCO': TagInfo('ETCO', ['Event timing codes'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, ETCO),
    
    'MLLT': TagInfo('MLLT', ['MPEG location lookup table'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, MLLT),
    
    'ASPI': TagInfo('ASPI', ['Audio seek point index'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, ASPI),
    
    'POSS': TagInfo('POSS', ['Position synchronisation frame'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, POSS),
    
    'RVA2': TagInfo('RVA2', ['Relative volume adjustment (2)'],
                    'BINARY', 'BINARY', 'AUDIO_ADJUSTMENT', 'text', False, RVA2),
    
    'EQU2': TagInfo('EQU2', ['Equalisation (2)'],
                    'BINARY', 'BINARY', 'AUDIO_ADJUSTMENT', 'text', False, EQU2),
    
    'RVRB': TagInfo('RVRB', ['Reverb'],
                    'BINARY', 'BINARY', 'AUDIO_ADJUSTMENT', 'text', True, RVRB),
    
    'PCNT': TagInfo('PCNT', ['Play counter'],
                    'BINARY', 'BINARY', 'PLAYBACK', 'text', True, PCNT),
    
    'POPM': TagInfo('POPM', ['Popularimeter'],
                    'BINARY', 'BINARY', 'PLAYBACK', 'text', False, POPM),
    
    'RBUF': TagInfo('RBUF', ['Recommended buffer size'],
                    'BINARY', 'BINARY', 'PLAYBACK', 'text', True, RBUF),
    
    'SEEK': TagInfo('SEEK', ['Seek frame'],
                    'BINARY', 'BINARY', 'PLAYBACK', 'text', True, SEEK),
    
    'COMR': TagInfo('COMR', ['Commercial frame'],
                    'BINARY', 'BINARY', 'LINKING', 'text', False, COMR),
    
    'LINK': TagInfo('LINK', ['Linked information'],
                    'BINARY', 'BINARY', 'LINKING', 'text', False, LINK),
}


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_tag_info(tag_id: str) -> Optional[TagInfo]:
    """
    Retrieve complete tag information by frame ID.
    Also handles composite IDs like 'COMM[eng]' or 'TXXX:Description'.
    """
    # Extract base ID from composite format
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    return TAG_REGISTRY.get(base_id)


def get_ui_category(tag_id: str) -> UICategory:
    """
    Get the UI widget category for this tag.
    Used to select appropriate input widget in backtrack.
    """
    info = get_tag_info(tag_id)
    return info.ui_category if info else 'text'


def tags_by_type(frame_type: str) -> list[str]:
    """Get all tag IDs for a specific frame type."""
    return [
        tag_id
        for tag_id, info in TAG_REGISTRY.items()
        if info.frame_type == frame_type
    ]


def tags_by_ui_category(category: UICategory) -> list[str]:
    """Get all tag IDs that use a specific UI widget."""
    return [
        tag_id
        for tag_id, info in TAG_REGISTRY.items()
        if info.ui_category == category
    ]


def is_text_tag(tag_id: str) -> bool:
    """Check if tag is text-based (T*** prefix)."""
    return tag_id.startswith('T') and tag_id != 'TXXX'


def is_url_tag(tag_id: str) -> bool:
    """Check if tag is URL-based (W*** prefix)."""
    return tag_id.startswith('W') and tag_id != 'WXXX'


def is_binary_tag(tag_id: str) -> bool:
    """Check if tag is binary."""
    tag = get_tag_info(tag_id)
    return tag.frame_type == 'BINARY' if tag else False


def is_single_only(tag_id: str) -> bool:
    """Check if tag allows only single instance per ID3 tag."""
    tag = get_tag_info(tag_id)
    return tag.single_only if tag else False


def get_mutagen_class(tag_id: str) -> Optional[Type]:
    """Get the mutagen frame class for a tag ID."""
    tag = get_tag_info(tag_id)
    return tag.mutagen_class if tag else None

def get_preferred_tag_name(tag_id: str) -> str:
    """Get the user-defined preferred tag name from config."""
    from src.config import load_config
    config = load_config()
    return dict(config['tag_name_preferences'])[tag_id]