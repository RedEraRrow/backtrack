"""
Music library management and metadata extraction.

Handles library building, searching, grouping, and metadata loading.
Integrates with tag_registry for consistent field naming and validation.
"""

from __future__ import annotations
import os
import json
import tempfile
import urllib.parse
import re
import xml.etree.ElementTree as ET
import unicodedata
import threading
from pathlib import Path
from typing import Any

from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from src.history import get_recent_paths


# ============================================================================
# Constants
# ============================================================================

VALID_AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac')
SYNC_INTERVAL_SECONDS = 30

# Field names — single source for library metadata keys
METADATA_FIELDS = {
    'title', 'artist', 'album_artist', 'album', 'track', 'total_tracks' 'disc', 'total_discs'
    'disc_subtitle', 'genre', 'year', 'grouping', 'work',
    'movement_name', 'movement', 'total_movements' 'play_count', 'bpm',
    'performers', 'cached_mtime', 'path'
}

# ID3 frame mapping for common fields (simplified)
ID3_FIELD_MAP = {
    'title': 'TIT2',
    'artist': 'TPE1',
    'album_artist': 'TPE2',
    'album': 'TALB',
    'composer': 'TCOM',
    'genre': 'TCON',
    'year': 'TDRC',
    'bpm': 'TBPM',
}


# ============================================================================
# Setup
# ============================================================================

def _default_cache_dir() -> Path:
    """Get platform-appropriate cache directory."""
    if os.name == "nt":
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / "Backtrack"
        return Path.home() / "AppData" / "Roaming" / "Backtrack"
    
    xdg_cache = os.getenv("XDG_CACHE_HOME")
    if xdg_cache:
        return Path(xdg_cache) / "backtrack"
    return Path.home() / ".cache" / "backtrack"


CACHE_DIR = Path(os.getenv("BACKTRACK_CACHE_DIR") or _default_cache_dir())
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_PATH = CACHE_DIR / "library_cache.json"
XML_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/Library.xml"))

# Background sync state
_sync_thread: threading.Thread | None = None
_sync_trigger = threading.Event()
_cache_mtime = 0
_sync_lock = threading.Lock()
_sync_state = {
    "library": None,
    "xml_db": None,
    "xml_title_keys": set(),
}


# ============================================================================
# Utilities
# ============================================================================

def normalise_string(s: str) -> str:
    """Normalise string for fuzzy matching by removing accents and non-alphanumeric."""
    if not s:
        return ""
    
    s = os.path.splitext(s)[0]
    s = unicodedata.normalize('NFD', s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _parse_plist_value(element: Any) -> Any:
    """Extract value from plist element based on tag type."""
    handlers = {
        'string': lambda e: e.text or '',
        'integer': lambda e: e.text or '0',
        'true': lambda e: True,
        'false': lambda e: False,
        'date': lambda e: e.text or '',
    }
    handler = handlers.get(element.tag, lambda e: e.text or '')
    return handler(element)


# ============================================================================
# XML Database (iTunes Library.xml)
# ============================================================================

def load_xml_database(xml_path: str = "Library.xml") -> tuple:
    """
    Parse Apple Music Library XML format.
    
    Args:
        xml_path: Path to Library.xml
    
    Returns:
        (db_dict, title_keys_set) or (None, set()) if not found
    """
    db = {}
    if not os.path.exists(xml_path):
        return None, set()
    
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        tracks_dict = root.find("./dict/dict")
        if tracks_dict is None:
            return None, set()
        
        for i in range(0, len(tracks_dict), 2):
            track_entry = tracks_dict[i + 1]
            track_data = {}
            
            for j in range(0, len(track_entry), 2):
                k = track_entry[j].text
                v = _parse_plist_value(track_entry[j + 1])
                track_data[k] = v
            
            # Index by filename and metadata-based keys for fuzzy matching
            location = track_data.get('Location', '')
            if location:
                decoded_path = urllib.parse.unquote(location)
                file_name = os.path.basename(decoded_path).replace(':', '_').replace('/', '_')
                db[file_name] = track_data
                db[f"norm_{normalise_string(file_name)}"] = track_data
            
            if track_data.get('Name'):
                norm_name = normalise_string(track_data['Name'])
                db[f"title_{norm_name}"] = track_data
                if track_data.get('Artist'):
                    meta_key = normalise_string(f"{track_data['Artist']}{track_data['Name']}")
                    db[f"meta_{meta_key}"] = track_data
        
        return db, {k for k in db if k.startswith("title_")}
    except Exception:
        return None, set()


def _get_xml_mtime() -> float:
    """Get modification time of XML database."""
    try:
        return os.path.getmtime(XML_PATH)
    except OSError:
        return 0


# ============================================================================
# Background Sync
# ============================================================================

def start_background_sync(library: list, xml_db: dict, xml_title_keys: set) -> None:
    """Start background library synchronization thread."""
    global _sync_thread
    
    with _sync_lock:
        _sync_state["library"] = library
        _sync_state["xml_db"] = xml_db
        _sync_state["xml_title_keys"] = xml_title_keys
        
        if _sync_thread and _sync_thread.is_alive():
            _sync_trigger.set()
            return
        
        _sync_thread = threading.Thread(
            target=_sync_worker,
            args=(library, xml_db, xml_title_keys),
            daemon=True,
        )
        _sync_thread.start()


def _signal_background_sync() -> None:
    """Wake background sync when library or cache changes."""
    _sync_trigger.set()


def _sync_worker(library: list, xml_db: dict, xml_title_keys: set) -> None:
    """Worker thread for background library synchronization."""
    global _cache_mtime
    from src.utils import ui_utils
    
    last_xml_mtime = _get_xml_mtime()
    
    while True:
        library = _sync_state.get("library") or library
        xml_db = _sync_state.get("xml_db") or xml_db
        xml_title_keys = _sync_state.get("xml_title_keys") or xml_title_keys
        
        ui_utils.set_status("sync", "Checking library for updates...")
        changed = False
        
        # Reload XML if modified
        current_xml_mtime = _get_xml_mtime()
        if current_xml_mtime and current_xml_mtime != last_xml_mtime:
            refreshed_xml_db, refreshed_xml_title_keys = load_xml_database(XML_PATH)
            if refreshed_xml_db is not None:
                xml_db = refreshed_xml_db
                xml_title_keys = refreshed_xml_title_keys
            last_xml_mtime = current_xml_mtime
        
        # Check for modified files
        path_map = {track['path']: track for track in library}
        for i, (path, track) in enumerate(path_map.items()):
            if i % 20 == 0:
                ui_utils.set_status("sync", f"Syncing library ({i}/{len(library)})")
            
            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                continue
            
            if current_mtime != track.get('cached_mtime', 0):
                fresh = get_metadata(path, xml_db, xml_title_keys)
                track.update(fresh)
                changed = True
        
        if changed:
            save_library_cache(library, _async=False)
        
        # Check for external cache updates
        if os.path.exists(CACHE_PATH):
            try:
                current_cache_mtime = os.path.getmtime(CACHE_PATH)
                if current_cache_mtime != _cache_mtime:
                    _cache_mtime = current_cache_mtime
                    external_library = load_library_cache()
                    if external_library and external_library != library:
                        library[:] = external_library
            except OSError:
                pass
        
        ui_utils.set_status("sync", None)
        _sync_trigger.wait(SYNC_INTERVAL_SECONDS)
        _sync_trigger.clear()


# ============================================================================
# Metadata Extraction
# ============================================================================

def _get_default_metadata(file_path: str) -> dict:
    """Get default metadata with all required fields."""
    return {
        "title": os.path.splitext(os.path.basename(file_path))[0],
        "artist": "Unknown Artist",
        "album_artist": "",
        "album": "Unknown Album",
        "track": "0",
        "total_tracks": "0",
        "disc": "0",
        "total_discs": "0",
        "disc_subtitle": "",
        "path": file_path,
        "genre": "Unknown Genre",
        "year": "Unknown Year",
        "grouping": "",
        "work": "",
        "movement_name": "",
        "movement": "0",
        "total_movements": "0",
        "play_count": 0,
        "bpm": "0",
        "performers": "",
        "cached_mtime": 0,
    }


def get_song_duration(file_path: str) -> float:
    """
    Get audio duration in seconds.
    
    Args:
        file_path: Path to audio file
    
    Returns:
        Duration in seconds, or 0 if unavailable
    """
    try:
        return MP3(file_path).info.length
    except Exception:
        return 0


def get_metadata(file_path: str, xml_db: dict | None = None,
                 xml_title_keys: set | None = None) -> dict:
    """
    Extract metadata from audio file.
    
    All required metadata fields are initialized to prevent KeyErrors.
    XML database is used for enrichment if available.
    
    Args:
        file_path: Path to audio file
        xml_db: Parsed iTunes Library.xml database
        xml_title_keys: Set of title index keys from xml_db
    
    Returns:
        Dictionary with all standard metadata fields
    """
    metadata = _get_default_metadata(file_path)
    
    try:
        metadata["cached_mtime"] = os.path.getmtime(file_path)
    except OSError:
        pass
    
    # Extract from ID3 tags (MP3)
    if file_path.lower().endswith('.mp3'):
        try:
            tags = ID3(file_path)
            metadata.update(_extract_id3_metadata(tags))
        except Exception:
            pass
    
    # Extract from MP4 tags (M4A, MP4, M4P)
    elif file_path.lower().endswith(('.m4a', '.mp4', '.m4p')):
        try:
            tags = MP4(file_path)
            if tags:  # Only process if tags exist
                extracted = _extract_mp4_metadata(tags)
                metadata.update(extracted)
            else:
                # If MP4 read succeeded but no tags, try XML enrichment
                if xml_db and xml_title_keys:
                    _enrich_from_xml(metadata, xml_db, xml_title_keys)
        except Exception as e:
            # If MP4 parsing fails (e.g., DRM-protected), try XML fallback
            if xml_db and xml_title_keys:
                _enrich_from_xml(metadata, xml_db, xml_title_keys)
    
    # Enrich from XML database if available
    if xml_db and xml_title_keys:
        _enrich_from_xml(metadata, xml_db, xml_title_keys)
    
    return metadata


def _extract_id3_metadata(tags: ID3) -> dict:
    """Extract metadata from ID3 tags."""
    result = {}
    
    # Standard text frames
    frame_map = {
        'TIT2': 'title',
        'TPE1': 'artist',
        'TPE2': 'album_artist',
        'TALB': 'album',
        'TCON': 'genre',
        'TDRC': 'year',
        'TIT1': 'work',
        'TBPM': 'bpm',
    }
    
    for frame_id, field in frame_map.items():
        if frame_id in tags:
            result[field] = str(tags[frame_id].text[0]) if tags[frame_id].text else ""
    
    # Track and disc numbers
    if 'TRCK' in tags:
        track_number_data = tags['TRCK'].text[0].replace('⁄', '/').split("/") if tags['TRCK'].text else ""
        result['track'] = str(track_number_data[0])
        result['total_tracks'] = str(track_number_data[1])
    if 'TPOS' in tags:
        disc_number_data = tags['TPOS'].text[0].replace('⁄', '/').split("/") if tags['TPOS'].text else ""
        result['disc'] = str(disc_number_data[0])
        result['total_discs'] = str(disc_number_data[1])
    
    # Classical music extensions
    if 'MVIN' in tags:
        movement_data = tags['MVIN'].text[0].replace('⁄', '/').split("/") if tags['MVIN'].text else ""
        result['movement'] = str(movement_data[0])
        result['total_movements'] = str(movement_data[1])
    if 'MVNM' in tags:
        result['movement_name'] = str(tags['MVNM'].text[0]) if tags['MVNM'].text else ""
    
    # Grouping
    if 'TIT1' in tags:
        result['grouping'] = str(tags['TIT1'].text[0]) if tags['TIT1'].text else ""
    
    return result


def _extract_mp4_metadata(tags: MP4) -> dict:
    """Extract metadata from MP4/M4A/M4P tags with robust error handling."""
    result = {}
    
    if not tags:
        return result
    
    # MP4 atom mappings for standard metadata
    field_map = {
        '\xa9nam': 'title',
        '\xa9ART': 'artist',
        'aART': 'album_artist',
        '\xa9alb': 'album',
        '\xa9gen': 'genre',
        '\xa9day': 'year',
        '\xa9wrk': 'work',
        'tmpo': 'bpm',
    }
    
    for mp4_atom, field in field_map.items():
        try:
            if mp4_atom in tags:
                val = tags[mp4_atom]
                if val:
                    # Handle both list and direct values
                    text = val[0] if isinstance(val, list) else val
                    if text:
                        result[field] = str(text).strip()
        except (KeyError, IndexError, TypeError):
            continue
    
    # Track number with total (preserve X/Total format)
    try:
        if 'trkn' in tags and tags['trkn']:
            track_tuple = tags['trkn'][0]
            track_num = track_tuple[0]
            total = track_tuple[1] if len(track_tuple) > 1 else None
            result['track'] = f"{track_num}/{total}" if total else str(track_num)
    except (KeyError, IndexError, TypeError):
        pass
    
    # Disc number with total (preserve X/Total format)
    try:
        if 'disk' in tags and tags['disk']:
            disc_tuple = tags['disk'][0]
            disc_num = disc_tuple[0]
            total = disc_tuple[1] if len(disc_tuple) > 1 else None
            result['disc'] = f"{disc_num}/{total}" if total else str(disc_num)
    except (KeyError, IndexError, TypeError):
        pass
    
    # Classical music extensions (©mvi = movement number, ©mvn = movement name)
    try:
        if '©mvi' in tags and tags['©mvi']:
            val = tags['©mvi']
            result['movement_number'] = str(val[0]).strip() if val else ""
    except (KeyError, IndexError, TypeError):
        pass
    
    try:
        if '©mvn' in tags and tags['©mvn']:
            val = tags['©mvn']
            result['movement_name'] = str(val[0]).strip() if val else ""
    except (KeyError, IndexError, TypeError):
        pass
    
    return result


def _enrich_from_xml(metadata: dict, xml_db: dict, xml_title_keys: set) -> None:
    """
    Enrich metadata using iTunes Library.xml database with multiple lookup strategies.
    
    Tries:
    1. Title-based lookup (best when metadata exists)
    2. Normalized filename lookup (handles special chars)
    3. Exact filename lookup (fallback)
    4. Metadata-based (artist + title)
    """
    if not xml_db:
        return
    
    file_path = metadata.get('path', '')
    filename = os.path.basename(file_path)
    
    # Strategy 1: Title-based lookup (if we have a real title, not just filename)
    title = metadata.get('title', '').strip()
    if title and not title.startswith('Unknown'):
        norm_title = normalise_string(title)
        xml_track = xml_db.get(f"title_{norm_title}")
        if xml_track:
            _update_metadata_from_xml(metadata, xml_track)
            return
    
    # Strategy 2: Normalized filename lookup (robust to special chars)
    norm_filename = normalise_string(filename)
    xml_track = xml_db.get(f"norm_{norm_filename}")
    if xml_track:
        _update_metadata_from_xml(metadata, xml_track)
        return
    
    # Strategy 3: Exact filename lookup
    xml_track = xml_db.get(filename)
    if xml_track:
        _update_metadata_from_xml(metadata, xml_track)
        return
    
    # Strategy 4: Try without extension
    filename_no_ext = os.path.splitext(filename)[0]
    norm_no_ext = normalise_string(filename_no_ext)
    xml_track = xml_db.get(f"norm_{norm_no_ext}")
    if xml_track:
        _update_metadata_from_xml(metadata, xml_track)
        return


def _update_metadata_from_xml(metadata: dict, xml_track: dict) -> None:
    """Update metadata dictionary from XML track entry."""
    if not xml_track:
        return
    
    updates = {}
    
    # Map XML keys to metadata keys
    xml_field_map = {
        'Name': 'title',
        'Artist': 'artist',
        'Album Artist': 'album_artist',
        'Album': 'album',
        'Genre': 'genre',
        'Year': 'year',
        'BeatsPerMinute': 'bpm',
    }
    
    for xml_key, meta_key in xml_field_map.items():
        if xml_key in xml_track and xml_track[xml_key]:
            val = xml_track[xml_key]
            updates[meta_key] = str(val).strip() if val else ""
    
    metadata.update(updates)


# ============================================================================
# Library Operations
# ============================================================================

def build_library(directory: str, xml_db: dict | None = None,
                  xml_title_keys: set | None = None,
                  ignore_hidden: bool = False) -> list:
    """
    Build music library from directory.
    
    Args:
        directory: Root directory to scan
        xml_db: Optional iTunes Library.xml database
        xml_title_keys: Optional title index keys
        ignore_hidden: Skip tracks marked HIDDEN
    
    Returns:
        List of track metadata dictionaries
    """
    library = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(VALID_AUDIO_EXTENSIONS):
                full_path = os.path.join(root, file)
                meta = get_metadata(full_path, xml_db, xml_title_keys)
                if not (ignore_hidden and meta.get('grouping') == 'HIDDEN'):
                    library.append(meta)
    return library


def save_library_cache(library: list, _async: bool = False) -> None:
    """
    Save library to disk atomically using temp file.
    
    Args:
        library: Library to save
        _async: If True, save in background thread
    """
    def _write():
        dir_name = CACHE_PATH.parent
        fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(library, f, indent=4)
            os.replace(temp_path, CACHE_PATH)
            global _cache_mtime
            _cache_mtime = os.path.getmtime(CACHE_PATH)
            if threading.current_thread() is not _sync_thread:
                _signal_background_sync()
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            print(f"Error saving cache: {e}")
    
    if _async:
        threading.Thread(target=_write, daemon=True).start()
    else:
        _write()


def load_library_cache() -> list:
    """Load library from disk cache."""
    global _cache_mtime
    if not os.path.exists(CACHE_PATH):
        _cache_mtime = 0
        return []
    
    try:
        with open(CACHE_PATH, 'r') as f:
            library = json.load(f)
        _cache_mtime = os.path.getmtime(CACHE_PATH)
        return library
    except (json.JSONDecodeError, IOError):
        _cache_mtime = 0
        return []


def refresh_library_entry(library: list, file_path: str,
                          xml_db: dict | None = None) -> dict:
    """
    Re-read and update metadata for a single file.
    
    Args:
        library: Library to update
        file_path: Path to file to refresh
        xml_db: Optional XML database for enrichment
    
    Returns:
        Updated track metadata
    """
    fresh = get_metadata(file_path, xml_db)
    for i, track in enumerate(library):
        if track['path'] == file_path:
            library[i] = fresh
            break
    else:
        library.append(fresh)
    
    save_library_cache(library)
    return fresh


# ============================================================================
# Library Grouping and Searching
# ============================================================================

def get_grouped_data(library: list, category: str) -> dict:
    """
    Group library by category.
    
    Args:
        library: Music library
        category: Field to group by (artist, album, genre, grouping)
    
    Returns:
        Dict mapping category value to list of tracks
    """
    grouped = {}
    
    for song in library:
        val = song.get(category) or "Unknown"
        
        # Prefer album_artist for artist grouping
        if category == "artist":
            val = song.get("album_artist") or song.get("artist") or "Unknown"
        
        # Skip Unknown grouping
        if category == "grouping" and val == "Unknown":
            continue
        
        # Classical artist normalization
        if category == "artist" and song.get("genre", "").lower() == "classical":
            val = val.split(',')[0].split('&')[0].split(';')[0].strip()
        
        grouped.setdefault(val, []).append(song)
    
    return grouped


def get_group_sort_key(display_name: str, songs: list, category: str) -> str:
    """
    Sort key for group name.
    
    Priority:
        1. Explicit sort-order tag (TSOP/TSO2/TSOA)
        2. Display name with leading "The " dropped
        3. Raw lowercase display name
    
    Args:
        display_name: Display name of group
        songs: Tracks in group
        category: Category being sorted
    
    Returns:
        Sort key string
    """
    sort_tags = {
        'artist': ('Album Artist Sort Order', 'Performer Sort Order'),
        'album_artist': ('Album Artist Sort Order', 'Performer Sort Order'),
        'album': ('Album Sort Order',),
    }.get(category, ())
    
    for tag in sort_tags:
        for song in songs:
            val = song.get(tag, '').strip()
            if val:
                key = val.lower()
                if key.endswith(', the'):
                    key = key[:-5].strip()
                return key
    
    # No sort tag — use display name, strip "The "
    name = display_name.lower()
    if name.startswith("the "):
        return name[4:].strip()
    return name


def sort_library_logic(tracks: list) -> list:
    """
    Sort tracks by artist, year, album, disc, and track number.
    
    Args:
        tracks: List of tracks to sort
    
    Returns:
        Sorted list
    """
    def get_sortable_name(display_name: str, sort_order: str | None) -> str:
        if sort_order and str(sort_order).strip():
            return str(sort_order).lower()
        if not display_name:
            return ""
        name = str(display_name).lower()
        if name.startswith("the "):
            return name[4:].strip()
        return name
    
    def to_num(v: Any) -> float:
        """Convert value to number, handling fractions like '1/12'."""
        try:
            s = str(v).strip()
            if '/' in s:
                return float(s.split('/')[0])
            return float(s) if s else 0.0
        except (ValueError, TypeError, IndexError):
            return 0.0
    
    def sort_key(track: dict):
        artist = track.get('album_artist') or track.get('artist', 'Unknown Artist')
        artist_sort = track.get('Album Artist Sort Order') or track.get('Performer Sort Order')
        album = track.get('album', 'Unknown Album')
        album_sort = track.get('Album Sort Order')
        year_val = track.get('year')
        
        try:
            clean_year = int(str(year_val))
        except (ValueError, TypeError):
            clean_year = 0
        
        return (
            get_sortable_name(artist, artist_sort),
            -clean_year,
            get_sortable_name(album, album_sort),
            to_num(track.get('disc', 1)),
            to_num(track.get('track', 0)),
        )
    
    return sorted(tracks, key=sort_key)


def search_library(library: list, query: str,
                   active_tags: list[str] | None = None) -> list:
    """
    Search library with AND logic and relevance scoring.
    
    Scoring:
        - Exact match: 4x weight
        - Start-of-field: 2x weight
        - Substring: 1x weight
        - Recent play: +2 bonus
    
    Args:
        library: Music library
        query: Search query
        active_tags: Tags to search (default: title, artist, album, genre, performers)
    
    Returns:
        Sorted list of matching tracks
    """
    recent = get_recent_paths()
    active_tags = active_tags or ['title', 'artist', 'album', 'genre', 'performers']
    weights = {'title': 10, 'artist': 8, 'album': 5, 'genre': 3, 'performers': 2}
    
    words = query.lower().split()
    results = []
    
    for song in library:
        score = 0
        field_vals = {tag: str(song.get(tag, "")).lower() for tag in active_tags}
        
        # Every word must match at least one field (AND logic)
        for word in words:
            word_matched = False
            for tag, val in field_vals.items():
                if word in val:
                    w = weights.get(tag, 1)
                    if val == word:
                        score += w * 4
                    elif val.startswith(word):
                        score += w * 2
                    else:
                        score += w
                    word_matched = True
            if not word_matched:
                score = 0
                break
        
        if score <= 0:
            continue
        
        # Recency boost
        if song['path'] in recent:
            score += 2
        
        results.append((score, song))
    
    results.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in results]