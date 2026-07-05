"""Single source of truth for ID3v2.4 frames: the tag registry and lookups."""
from dataclasses import dataclass
from typing import Dict, Type, Literal, Any, Optional

from mutagen.id3 import *  # type: ignore[reportWildcardImportFromLibrary]
from mutagen.id3._frames import *  # type: ignore[reportWildcardImportFromLibrary]
from src.config import load_config

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
    'audio adjustment',  # EQU2, RVA2, RVRB
]


@dataclass
class TagInfo:
    """Metadata record for a single ID3v2.4 frame."""
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
        return self.mutagen_class(*args, **kwargs)


TAG_REGISTRY: Dict[str, TagInfo] = {

    # TEXT FRAMES (T***)

    'TIT1': TagInfo('TIT1', ['Work name', 'Work', 'Work name (classical)', 'Content group description', 'Content group'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TIT1),
    
    'TIT2': TagInfo('TIT2', ['Title', 'Songname', 'Song name', 'Song', 'Trackname', 'Track name', 'Track', 'Content description'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', True, TIT2),
    
    'TIT3': TagInfo('TIT3', ['Subtitle', 'Description refinement'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TIT3),
    
    'TALB': TagInfo('TALB', ['Album', 'Album name', 'Movie', 'Movie title', 'Show', 'Show title'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', True, TALB),
    
    'TOAL': TagInfo('TOAL', ['Original album', 'Original Movie', 'Original show title'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TOAL),
    
    'TPE1': TagInfo('TPE1', ['Artist', 'Lead performer(s)', 'Lead performer', 'Soloist(s)', 'Soloist'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TPE1),

    'TPE2': TagInfo('TPE2', ['Album artist', 'Band', 'Orchestra', 'Accompaniment'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TPE2),

    'TPE3': TagInfo('TPE3', ['Conductor', 'Performer refinement'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TPE3),
    
    'TPE4': TagInfo('TPE4', ['Interpreted by', 'Remixed by', 'Modified by'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TPE4),
    
    'TOPE': TagInfo('TOPE', ['Original artist', 'Original performer', 'Original artist(s)', 'Original performer(s)'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TOPE),
    
    'TEXT': TagInfo('TEXT', ['Lyricist', 'Text writer', 'Words'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TEXT),
    
    'TOLY': TagInfo('TOLY', ['Original lyricist', 'Original text writer', 'Original lyricist(s)', 'Original text writer(s)'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', False, TOLY),

    'TCMP': TagInfo('TCMP', ['Compilation'],
                    'TEXT', 'TEXT_UTF8', 'IDENTIFICATION', 'text', False, TCMP),
    
    'TCOM': TagInfo('TCOM', ['Composer'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TCOM),
    
    'TENC': TagInfo('TENC', ['Encoded by'],
                    'TEXT', 'TEXT_UTF8', 'INVOLVED_PERSONS', 'text', True, TENC),
    
    'TBPM': TagInfo('TBPM', ['BPM', 'Beats per minute'],
                    'NUMERIC', 'INT_BIG', 'DERIVED_SUBJECTIVE', 'text', True, TBPM),

    'TLEN': TagInfo('TLEN', ['Duration', 'Duration (ms)', 'Duration (milliseconds)', 'Length', 'Length (ms)', 'Length (milliseconds)'],
                    'NUMERIC', 'INT_BIG', 'DERIVED_SUBJECTIVE', 'duration', True, TLEN),
    
    'TKEY': TagInfo('TKEY', ['Key', 'Initial key'],
                    'TEXT', 'TEXT_UTF8', 'DERIVED_SUBJECTIVE', 'text', False, TKEY),
    
    'TLAN': TagInfo('TLAN', ['Language', 'Language(s)'],
                    'LIST', 'LIST_STRING', 'DERIVED_SUBJECTIVE', 'text', False, TLAN),
    
    'TCON': TagInfo('TCON', ['Genre', 'Content type'],
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
    
    'TDLY': TagInfo('TDLY', ['Playlist delay', 'Playlist delay (ms)', 'Playlist delay (milliseconds)'],
                    'NUMERIC', 'INT_BIG', 'OTHER_TEXT', 'duration', False, TDLY),
    
    'TSSE': TagInfo('TSSE', ['Encoder', 'Encoding settings', 'Software and settings used for encoding', 'Hardware and settings used for encoding'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', True, TSSE),
    
    'TSOA': TagInfo('TSOA', ['Album sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOA),
    
    'TSOP': TagInfo('TSOP', ['Artist sort order', 'Performer sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOP),
    
    'TSOT': TagInfo('TSOT', ['Title sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOT),
    
    'TSO2': TagInfo('TSO2', ['Album artist sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSO2),

    'TSOC': TagInfo('TSOC', ['Composer sort order'],
                    'TEXT', 'TEXT_UTF8', 'SORT_ORDER', 'text', True, TSOC),
    
    'TSRC': TagInfo('TSRC', ['ISRC', 'ISRC (International Standard Recording Code)'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', True, TSRC),
    
    'TXXX': TagInfo('TXXX', ['Custom frame', 'User defined text information'],
                    'TEXT', 'TEXT_UTF8', 'USER_DEFINED', 'text', False, TXXX),
    
    'COMM': TagInfo('COMM', ['Comments'],
                    'TEXT', 'TEXT_UTF8_LANG', 'SPECIAL_TEXT', 'multiline text', False, COMM),
    
    'USLT': TagInfo('USLT', ['Lyrics', 'Unsynced lyrics', 'Unsynchronised lyrics', 'Unsynced text transcription', 'Unsynchronised text transcription'],
                    'TEXT', 'TEXT_UTF8_LANG', 'SPECIAL_TEXT', 'multiline text', False, USLT),
    
    'USER': TagInfo('USER', ['Terms of use'],
                    'TEXT', 'TEXT_UTF8_LANG', 'SPECIAL_TEXT', 'text', False, USER),
    
    'OWNE': TagInfo('OWNE', ['Ownership frame'],
                    'TEXT', 'TEXT_UTF8', 'SPECIAL_TEXT', 'text', True, OWNE),
    
    'MVNM': TagInfo('MVNM', ['Movement name', 'Movement', 'Movement name (classical)'],
                    'TEXT', 'TEXT_UTF8', 'CLASSICAL', 'text', False, MVNM),
    
    'GRP1': TagInfo('GRP1', ['Grouping' ,'Grouping (iTunes)'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', False, GRP1),
    
    'TSST': TagInfo('TSST', ['Disc subtitle', 'Set subtitle'],
                    'TEXT', 'TEXT_UTF8', 'OTHER_TEXT', 'text', False, TSST),

    # TIMESTAMP FRAMES (TD**)

    'TDEN': TagInfo('TDEN', ['Encoding date', 'Encoding time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDEN),
    
    'TDOR': TagInfo('TDOR', ['Original release date', 'Original release time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDOR),
    
    'TDRC': TagInfo('TDRC', ['Year', 'Recording date', 'Recording time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDRC),
    
    'TDRL': TagInfo('TDRL', ['Release date', 'Release time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDRL),
    
    'TDTG': TagInfo('TDTG', ['Tagging date', 'Tagging time'],
                    'TIMESTAMP', 'ISO8601', 'TIMESTAMP', 'date', True, TDTG),

    # LEGACY FRAMES (ID3v2.3)

    'TORY': TagInfo('TORY', ['Original release year', 'Original release year (legacy, ID3v2.3)'],
                    'YEAR', 'YYYY', 'LEGACY', 'year', False, TORY),
    
    'TDAT': TagInfo('TDAT', ['Date', 'Date (legacy, ID3v2.3)'],
                    'DATE', 'DDMM', 'LEGACY', 'date', False, TDAT),
    
    'TIME': TagInfo('TIME', ['Time', 'Time (legacy, ID3v2.3)'],
                    'TIME', 'HHMM', 'LEGACY', 'time', False, TIME),
    
    'TRDA': TagInfo('TRDA', ['Recording dates', 'Recording dates (legacy, ID3v2.3)'],
                    'DATE', 'DDMM', 'LEGACY', 'date', False, TRDA),

    # FRACTIONAL FRAMES

    'TRCK': TagInfo('TRCK', ['Track number', 'Position in set'],
                    'FRACTIONAL', 'FRACTIONAL', 'FRACTIONAL', 'fraction', True, TRCK),
    
    'TPOS': TagInfo('TPOS', ['Disc number', 'Part of a set'],
                    'FRACTIONAL', 'FRACTIONAL', 'FRACTIONAL', 'fraction', True, TPOS),
    
    'MVIN': TagInfo('MVIN', ['Movement number', 'Movement #', 'Movement number (classical)'],
                    'FRACTIONAL', 'FRACTIONAL', 'FRACTIONAL', 'fraction', False, MVIN),

    # LIST FRAMES

    'TIPL': TagInfo('TIPL', ['Involved people list'],
                    'LIST', 'LIST_KV', 'LIST', 'people', False, TIPL),
    
    'TMCL': TagInfo('TMCL', ['Musician credits list'],
                    'LIST', 'LIST_KV', 'LIST', 'people', False, TMCL),

    # URL FRAMES (W***)

    'WCOM': TagInfo('WCOM', ['Commercial information'],
                    'URL', 'URL', 'URL', 'text', False, WCOM),
    
    'WCOP': TagInfo('WCOP', ['Copyright', 'Legal information'],
                    'URL', 'URL', 'URL', 'text', True, WCOP),
    
    'WOAF': TagInfo('WOAF', ['Official audio file webpage'],
                    'URL', 'URL', 'URL', 'text', True, WOAF),
    
    'WOAR': TagInfo('WOAR', ['Artist URL', 'Official artist webpage', 'Official performer webpage'],
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

    # BINARY FRAMES

    'UFID': TagInfo('UFID', ['Unique file identifier'],
                    'BINARY', 'BINARY', 'IDENTIFICATION', 'text', False, UFID),
    
    'MCDI': TagInfo('MCDI', ['Music CD identifier'],
                    'BINARY', 'BINARY', 'IDENTIFICATION', 'text', True, MCDI),
    
    'APIC': TagInfo('APIC', ['Album art', 'Cover art', 'Attached picture'],
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
    
    'SYLT': TagInfo('SYLT', ['Synced lyrics', 'Synchronized lyrics', 'Synced text', 'Synchronized text'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'lyrics', False, SYLT),
    
    'SYTC': TagInfo('SYTC', ['Synced tempo codes', 'Synchronized tempo codes'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, SYTC),
    
    'ETCO': TagInfo('ETCO', ['Event timing codes'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, ETCO),
    
    'MLLT': TagInfo('MLLT', ['MPEG location lookup table'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, MLLT),
    
    'ASPI': TagInfo('ASPI', ['Audio seek point index'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, ASPI),
    
    'POSS': TagInfo('POSS', ['Position sync frame', 'Position synchronisation frame'],
                    'BINARY', 'BINARY', 'SYNCHRONIZED', 'text', True, POSS),
    
    'RVA2': TagInfo('RVA2', ['Relative volume adjustment'],
                    'BINARY', 'BINARY', 'AUDIO_ADJUSTMENT', 'audio adjustment', False, RVA2),

    'EQU2': TagInfo('EQU2', ['Equalisation'],
                    'BINARY', 'BINARY', 'AUDIO_ADJUSTMENT', 'audio adjustment', False, EQU2),

    'RVRB': TagInfo('RVRB', ['Reverb'],
                    'BINARY', 'BINARY', 'AUDIO_ADJUSTMENT', 'audio adjustment', True, RVRB),
    
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


# Supports colon notation (TXXX:desc:lang) and bracket notation (COMM[eng])
def parse_composite_tag_id(tag_id: str) -> tuple[str, str, str]:
    base_id = tag_id.split('[')[0].split(':')[0].upper()
    desc_val = ''
    lang_val = ''

    if '[' in tag_id and ']' in tag_id:
        lang_val = tag_id.split('[')[1].split(']')[0]
        return base_id, desc_val, lang_val

    if ':' in tag_id:
        parts = tag_id.split(':')
        if len(parts) == 3:
            desc_val = parts[1]
            lang_val = parts[2]
        elif len(parts) == 2:
            desc_val = parts[1]

    return base_id, desc_val, lang_val


def get_tag_info(tag_id: str) -> Optional[TagInfo]:
    """
    Retrieve complete tag information by frame ID.
    Also handles composite IDs like 'COMM[eng]' or 'TXXX:Description'.
    """
    base_id, desc_val, lang_val = parse_composite_tag_id(tag_id)
    return TAG_REGISTRY.get(base_id)


def get_official_category(tag_id: str) -> str:
    """Return the official_category for tag_id, or 'OTHER' if unknown."""
    info = get_tag_info(tag_id)
    return info.official_category if info else "OTHER"


def get_tag_category(tag_id: str) -> UICategory:
    """Return the ui_category for tag_id, or 'text' if unknown."""
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


def get_preferred_tag_name(tag_id: str) -> str:
    """Get the user-defined preferred tag name from config, falling back to the registry's first friendly name."""
    config = load_config()
    base_id, _desc, _lang = parse_composite_tag_id(tag_id)
    prefs: dict = dict(config.get('tag_name_preferences', {}))
    if base_id in prefs:
        return prefs[base_id]
    info = TAG_REGISTRY.get(base_id)
    return info.name[0] if info and info.name else base_id