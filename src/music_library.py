"""
Music library management and metadata extraction.

Handles building library, searching, grouping, and loading metadata from files and XML.
"""
from __future__ import annotations
from importlib import metadata
import os
import json
import tempfile
import urllib.parse
import re
import xml.etree.ElementTree as ET
import unicodedata
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
from typing import Any
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from src.history import get_recent_paths


def _default_cache_dir() -> Path:
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
VALID_AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac')
SYNC_INTERVAL_SECONDS = 30

_sync_thread: threading.Thread | None = None
_sync_trigger = threading.Event()
_cache_mtime = 0
_sync_lock = threading.Lock()
_sync_state = {
    "library": None,
    "xml_db": None,
    "xml_title_keys": set(),
}

# ID3 tag to metadata field mapping — single source of truth for all modules
TAG_MAP = {
    'TIT2': 'Title',
    'TPE1': 'Artist',
    'TPE2': 'Album Artist',
    'TALB': 'Album',
    'TDRC': 'Year',
    'TCON': 'Genre',
    'TRCK': 'Track',
    'TPOS': 'Disc',
    'GRP1': 'Grouping',
    'TCOM': 'Composer',
    'TENC': 'Encoded By',
    'COMM': 'Comment',
    'APIC': 'Album Art',
    'TIPL': 'Producer',
    'TMCL': 'Performers',
    'TPUB': 'Publisher',
    'TDRL': 'Release Date',
    'TSO2': 'Album Artist Sort Order',
    'TSOA': 'Album Sort Order',
    'TSOP': 'Performer Sort Order',
    'USLT': 'Unsynchronised Lyrics',
    'SYLT': 'Synchronised Lyrics',
    'TSST': 'Set Subtitle',
}


# ============================================================================
# XML Database (iTunes Library.xml)
# ============================================================================


def normalise_string(s: str) -> str:
    """Standardises strings for matching by removing accents and non-alphanumeric chars."""
    if not s: return ""

    s = os.path.splitext(s)[0] 

    s = unicodedata.normalize('NFD', s)
    s = "".join([c for c in s if not unicodedata.combining(c)])

    return re.sub(r'[^a-z0-9]', '', s.lower())

def _parse_plist_value(element: Any) -> Any:
    """Extract the value from a plist element based on its tag type."""
    tag_handlers = {
        'string': lambda e: e.text or '',
        'integer': lambda e: e.text or '0',
        'true': lambda e: True,
        'false': lambda e: False,
        'date': lambda e: e.text or '',
    }
    return tag_handlers.get(element.tag, lambda e: e.text or '')(element)

def load_xml_database(xml_path: str = "Library.xml") -> tuple:
    """Parse Apple-style Music Library XML. Returns (db, title_keys_set) or (None, set())."""
    db = {}
    if not os.path.exists(xml_path):
        return None, set()
    
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        tracks_dict = root.find("./dict/dict")
        if tracks_dict is None: return None, set()

        for i in range(0, len(tracks_dict), 2):
            track_entry = tracks_dict[i + 1]
            track_data = {}
            for j in range(0, len(track_entry), 2):
                k = track_entry[j].text
                v = _parse_plist_value(track_entry[j + 1])
                track_data[k] = v

            # Index by filename and a metadata-based key for fuzzy matching
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
    except Exception as e:
        return None, set()
    
def _get_xml_mtime():
    try:
        return os.path.getmtime(XML_PATH)
    except OSError:
        return 0

def start_background_sync(library: list, xml_db: dict, xml_title_keys: set):
    """Kicks off the background synchronization thread."""
    global _sync_thread

    with _sync_lock:
        _sync_state["library"] = library
        _sync_state["xml_db"] = xml_db
        _sync_state["xml_title_keys"] = xml_title_keys

        if _sync_thread and _sync_thread.is_alive():
            _sync_trigger.set()
            return

        _sync_thread = threading.Thread(
            target=sync_worker,
            args=(library, xml_db, xml_title_keys),
            daemon=True,
        )
        _sync_thread.start()


def _signal_background_sync() -> None:
    """Wake the background sync worker when the library or cache changes."""
    _sync_trigger.set()


def sync_worker(library: list, xml_db: dict, xml_title_keys: set):
    global _cache_mtime
    from src import ui_utils

    last_xml_mtime = _get_xml_mtime()

    while True:
        library = _sync_state.get("library") or library
        xml_db = _sync_state.get("xml_db") or xml_db
        xml_title_keys = _sync_state.get("xml_title_keys") or xml_title_keys

        ui_utils.set_status("sync", "Checking library for updates...")
        changed = False

        current_xml_mtime = _get_xml_mtime()
        if current_xml_mtime and current_xml_mtime != last_xml_mtime:
            refreshed_xml_db, refreshed_xml_title_keys = load_xml_database(XML_PATH)
            if refreshed_xml_db is not None:
                xml_db = refreshed_xml_db
                xml_title_keys = refreshed_xml_title_keys
            last_xml_mtime = current_xml_mtime

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


def get_metadata(file_path: str, xml_db: dict | None = None, xml_title_keys: set | None = None) -> dict:
    """Extract metadata from audio file, ensuring all required keys exist."""
    # INITIALISE ALL KEYS to prevent KeyErrors
    metadata = {
        "title": os.path.splitext(os.path.basename(file_path))[0],
        "artist": "Unknown Artist",
        "album_artist": "",
        "album": "Unknown Album",
        "track": "0",
        "disc": "1",
        "disc_subtitle": "",
        "path": file_path,
        "genre": "Unknown Genre",
        "year": "Unknown Year",
        "grouping": "",
        "play_count": 0
    }

    # 1. Try internal tags
    try:
        if file_path.endswith('.mp3'):
            tags = ID3(file_path)
            if 'TIT2' in tags: metadata["title"]  = str(tags['TIT2'])
            if 'TPE1' in tags: metadata["artist"]       = str(tags['TPE1'])
            if 'TPE2' in tags: metadata["album_artist"] = str(tags['TPE2'])
            if 'TALB' in tags: metadata["album"]  = str(tags['TALB'])
            if 'TRCK' in tags: metadata["track"]  = str(tags['TRCK']).split('/')[0]
            if 'TCON' in tags: metadata["genre"]  = str(tags['TCON'])
            if 'TDRC' in tags: metadata["year"]   = str(tags['TDRC'])
            if 'GRP1' in tags: metadata["grouping"] = str(tags['GRP1'])
            if 'TPOS' in tags: metadata["disc"]   = str(tags['TPOS']).split('/')[0]
            if 'TSST' in tags: metadata["disc_subtitle"] = str(tags['TSST'])
        elif file_path.endswith(('.m4a', '.m4p', '.mp4')):
            tags = MP4(file_path)
            metadata["title"] = str(tags.get('\xa9nam', [metadata["title"]])[0])
            metadata["artist"]       = str(tags.get('\xa9ART', [metadata["artist"]])[0])
            metadata["album_artist"] = str(tags.get('aART',   [metadata["album_artist"]])[0])
            metadata["album"] = str(tags.get('\xa9alb', [metadata["album"]])[0])
            trkn = tags.get('trkn', [(0, 0)])[0]
            metadata["track"] = str(trkn[0]) if isinstance(trkn, tuple) else str(trkn)
            disk = tags.get('disk', [(1, 0)])[0]
            metadata["disc"] = str(disk[0]) if isinstance(disk, tuple) else str(disk)
    except Exception as e:
        pass

    # 2. XML matching with normalisation
    if xml_db:
        norm_title = ""
        file_key = os.path.basename(file_path)
        xml_info = xml_db.get(file_key) or xml_db.get(f"norm_{normalise_string(file_key)}")
        
        if not xml_info:
            # Try artist+title meta key (works when ID3 tags are present)
            meta_key = f"meta_{normalise_string(metadata['artist'] + metadata['title'])}"
            xml_info = xml_db.get(meta_key)
        
        if not xml_info:
            # Title-only fallback for tracks with no embedded tags
            norm_title = normalise_string(metadata['title'])
            if norm_title:
                xml_info = xml_db.get(f"title_{norm_title}")
        
        if not xml_info:
            norm_file = normalise_string(os.path.splitext(file_key)[0])
            
            # Strip macOS duplicate suffixes (e.g., "Apple Juice 1" -> "Apple Juice")
            clean_title = normalise_string(re.sub(r'\s+\d+$', '', metadata['title']))
            clean_file  = normalise_string(re.sub(r'\s+\d+$', '', os.path.splitext(file_key)[0]))

            # Strip prefixed track numbers (e.g., "01 Apple Juice" -> "Apple Juice")
            no_track_title = normalise_string(re.sub(r'^\d+[\s\-_]+', '', metadata['title']))

            # Helper: scan title_keys set for substring match (avoids full dict iteration)
            title_keys = xml_title_keys or {k for k in xml_db if k.startswith("title_")}

            def _title_contains(fragment: str) -> Any | None:
                if not fragment:
                    return None
                for k in title_keys:
                    if fragment in k[6:]:   # k[6:] strips "title_" prefix
                        return xml_db.get(k)
                return None

            def _title_startswith(fragment: str) -> Any | None:
                if not fragment:
                    return None
                for k in title_keys:
                    if k[6:].startswith(fragment):
                        return xml_db.get(k)
                return None

            # A) Direct key lookups (O(1))
            xml_info = (
                xml_db.get(f"title_{clean_title}")
                or xml_db.get(f"norm_{clean_file}")
                or xml_db.get(f"title_{no_track_title}")
            )

            # B) Prefix match for truncated titles (e.g. "Fantasia… […")
            if not xml_info and len(norm_title) > 15:
                xml_info = _title_startswith(norm_title)
            if not xml_info and len(norm_file) > 15:
                for k in title_keys:
                    if k[6:].startswith(norm_file):
                        xml_info = xml_db.get(k); break

            # C) Dot-substitution variants
            if not xml_info:
                title_spaced = metadata['title'].replace('.', ' ')
                norm_spaced  = normalise_string(title_spaced)
                xml_info = xml_db.get(f"title_{norm_spaced}")

                # D) Substring: file title inside XML title ("Che soave…" in "Duettino: Che soave…")
                if not xml_info and len(norm_spaced) > 10:
                    xml_info = _title_contains(norm_spaced)

                # E) Clean leading numbers then dot-substitute
                if not xml_info:
                    clean_classic = re.sub(r'^\d+[\s\-_]+', '', title_spaced)
                    xml_info = xml_db.get(f"title_{normalise_string(clean_classic)}")

            # F) Aria name between underscores ("Act 3_ _Che soave_ -" -> "Che soave")
            if not xml_info:
                parts = metadata['title'].split('_')
                if len(parts) >= 3:
                    for raw in (parts[-2].strip(), parts[-2].strip().replace('.', ' ')):
                        norm_aria = normalise_string(raw)
                        if len(norm_aria) > 8:
                            xml_info = _title_contains(norm_aria)
                            if xml_info:
                                break

        if xml_info:
            metadata["play_count"] = int(xml_info.get("Play Count", 0))
            metadata["grouping"]   = xml_info.get("Grouping", "")
            metadata["xml_data"]   = xml_info
            # Fill any fields still at their defaults from the XML
            if metadata["track"]  == "0":             metadata["track"]  = str(xml_info.get("Track Number", "0"))
            if metadata["disc"]   == "1":              metadata["disc"]   = str(xml_info.get("Disc Number", "1"))
            if metadata["artist"] == "Unknown Artist": metadata["artist"] = xml_info.get("Artist",  "Unknown Artist")
            if not metadata["album_artist"]:           metadata["album_artist"] = xml_info.get("Album Artist", "")
            if metadata["album"]  == "Unknown Album":  metadata["album"]  = xml_info.get("Album",   "Unknown Album")
            if metadata["title"]  == os.path.splitext(os.path.basename(file_path))[0]:
                                                       metadata["title"]  = xml_info.get("Name",    metadata["title"])
            if metadata["genre"]  == "Unknown Genre":  metadata["genre"]  = xml_info.get("Genre",   "Unknown Genre")
            if metadata["year"]   == "Unknown Year":   metadata["year"]   = str(xml_info.get("Year", "Unknown Year"))

    metadata['cached_mtime'] = os.path.getmtime(file_path)
    return metadata


def get_song_duration(file_path: str) -> float:
    """Get duration of audio file in seconds."""
    try:
        return MP3(file_path).info.length
    except Exception:
        return 0


# ============================================================================
# Library Operations
# ============================================================================


def build_library(directory, xml_db=None, xml_title_keys=None):
    library = []
    xml_track_count = len(xml_db) // 4 if xml_db else 0 # Rough estimate due to multiple keys

    for root, _, files in os.walk(directory):
        for file in files:
            full_path = os.path.join(root, file)
            if file.lower().endswith(VALID_AUDIO_EXTENSIONS):
                meta = get_metadata(full_path, xml_db, xml_title_keys)
                library.append(meta)
    return library

def save_library_cache(library: list, _async: bool = False):
    """Saves the library to disk safely using a temporary file."""
    def _write():
        # Create a temporary file in the same directory as the cache
        dir_name = CACHE_PATH.parent
        fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(library, f, indent=4)
            # Atomic rename replaces the old file with the new one instantly
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


def refresh_library_entry(library: list, file_path: str, xml_db: dict | None = None) -> dict:
    """
    Re-read metadata for a single file, update its entry in library in-place,
    and persist the cache. Returns the updated track dict.
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


def load_library_cache() -> list:
    """Loads the library from disk, returning an empty list if corrupted."""
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


def get_grouped_data(library: list, category: str) -> dict:
    """
    Group library songs by a category (artist, album, genre, grouping).
    
    Returns dict mapping category value to list of songs.
    """
    grouped = {}
    
    for song in library:
        val = song.get(category) or "Unknown"

        # For artist browsing, prefer album_artist over artist
        if category == "artist":
            val = song.get("album_artist") or song.get("artist") or "Unknown"

        # Skip songs without grouping when browsing by grouping
        if category == "grouping" and val == "Unknown":
            continue
        
        # Special handling for classical artists
        if category == "artist" and song.get("genre", "").lower() == "classical":
            val = val.split(',')[0].split('&')[0].split(';')[0].strip()
        
        grouped.setdefault(val, []).append(song)
    
    return grouped


def get_group_sort_key(display_name: str, songs: list, category: str) -> str:
    """
    Sort key for a group name in browse view.

    Priority:
      1. Explicit sort-order tag on any song in the group (TSOP/TSO2/TSOA)
      2. Display name with leading "The " dropped
      3. Raw display name lowercased

    This means "The Beatles" sorts under B unless overridden by a sort tag.
    """
    sort_tag = {
        'artist':       ('Album Artist Sort Order', 'Performer Sort Order'),
        'album_artist': ('Album Artist Sort Order', 'Performer Sort Order'),
        'album':        ('Album Sort Order',),
    }.get(category, ())

    for tag in sort_tag:
        for song in songs:
            val = song.get(tag, '').strip()
            if val:
                key = val.lower()
                if key.endswith(', the'):
                    key = key[:-5].strip()
                return key

    # No sort tag — use album_artist then artist, strip leading "The "
    name = display_name.lower()
    if name.startswith("the "):
        return name[4:].strip()
    return name


def sort_library_logic(tracks):
    def get_sortable_name(display_name, sort_order_name):
        if sort_order_name and str(sort_order_name).strip():
            return str(sort_order_name).lower()
        if not display_name:
            return ""
        name = str(display_name).lower()
        if name.startswith("the "):
            return name[4:].strip()
        return name

    def sort_key(track_data):
        artist      = track_data.get('album_artist') or track_data.get('artist', 'Unknown Artist')
        artist_sort = track_data.get('Album Artist Sort Order') or track_data.get('Performer Sort Order')
        album       = track_data.get('album', 'Unknown Album')
        album_sort  = track_data.get('Album Sort Order')
        year_val    = track_data.get('year')
        try:
            clean_year = int(str(year_val))
        except (ValueError, TypeError):
            clean_year = 0

        def to_num(v):
            try:
                return float(str(v).split('/')[0])
            except:
                return 0.0
        
        return (
            get_sortable_name(artist, artist_sort),
            -clean_year,
            get_sortable_name(album, album_sort),
            to_num(track_data.get('disc', 1)),   # Changed from raw string
            to_num(track_data.get('track', 0)),  # Changed from raw string
        )

    return sorted(tracks, key=sort_key)


def select_from_alpha_list(
    items: list[str],
    sort_key_fn: Callable[[str], str],
    message: str,
    *,
    extra_top: list[str] | None = None,
) -> list[str] | None:
    """
    Universal alphabet-browse helper.

    Shows a letter picker (built from sort_key_fn) with a [Full List] toggle.
    Returns the filtered, sorted list of items to display — or None if the
    user chose Back at the letter screen.

    Args:
        items:        Pre-sorted list of display strings.
        sort_key_fn:  fn(name) -> str  —  sort key for that name.
        message:      Prompt label shown on the letter picker.
        extra_top:    Optional extra choices prepended (e.g. ["[Play All]"]).
    """
    import string as _string
    from src import prompt as _prompt

    def _letter(name: str) -> str:
        key = sort_key_fn(name)
        ch = key[0].upper() if key else "#"
        return ch if ch in _string.ascii_uppercase else "#"

    letters = sorted({_letter(n) for n in items})
    if "#" in letters:
        letters.remove("#")
        letters.append("#")

    letter_sel = _prompt.select(message, choices=["[Full List]"] + letters + [".. Back"])

    if not letter_sel or letter_sel == ".. Back":
        return None

    if letter_sel == "[Full List]":
        return items

    return [n for n in items if _letter(n) == letter_sel]
    def get_sortable_name(display_name, sort_order_name):
        # 1. Use explicit sort order if it exists
        if sort_order_name and str(sort_order_name).strip():
            return str(sort_order_name).lower()
        
        # 2. Otherwise, use display name but strip "The " for sorting
        if not display_name:
            return ""
        name = str(display_name).lower()
        if name.startswith("the "):
            return name[4:].strip()
        return name

    def sort_key(track_data):
        # Extract fields using the TAG_MAP keys
        artist       = track_data.get('album_artist') or track_data.get('artist', 'Unknown Artist')
        artist_sort  = track_data.get('Album Artist Sort Order') or track_data.get('Performer Sort Order')
        
        album = track_data.get('album', 'Unknown Album')
        album_sort = track_data.get('Album Sort Order') # TSOA

        # Handle the ValueError for years
        year_val = track_data.get('year')
        try:
            clean_year = int(str(year_val))
        except (ValueError, TypeError):
            clean_year = 0

        # Determine sort strings
        final_artist_sort = get_sortable_name(artist, artist_sort)
        final_album_sort = get_sortable_name(album, album_sort)

        return (
            final_artist_sort, 
            -clean_year, 
            final_album_sort, 
            track_data.get('disc', '1'), 
            track_data.get('track', '0')
        )

    return sorted(tracks, key=sort_key)


def search_library(library: list, query: str, active_tags: list | None = None) -> list:
    """
    Search library for songs matching query.

    Scoring rules:
    - Each active tag is checked independently
    - Exact full-field match scores highest
    - Start-of-field match scores higher than mid-field
    - All query words must match somewhere across the active tags (AND logic)
    - Recently played tracks get a small boost, but only if they matched
    - Non-matching tracks are never returned regardless of recent history
    """
    recent = get_recent_paths()
    active_tags = active_tags or ['title', 'artist', 'album', 'genre']
    weights = {'title': 10, 'artist': 8, 'album': 5, 'genre': 3, 'path': 1}

    words = query.lower().split()
    results = []

    for song in library:
        score = 0
        field_vals = {tag: str(song.get(tag, "")).lower() for tag in active_tags}

        # Every word must match at least one active field (AND logic)
        for word in words:
            word_matched = False
            for tag, val in field_vals.items():
                if word in val:
                    w = weights.get(tag, 1)
                    if val == word:
                        score += w * 4        # exact full match
                    elif val.startswith(word):
                        score += w * 2        # prefix match
                    else:
                        score += w            # substring match
                    word_matched = True
            if not word_matched:
                score = 0
                break

        if score <= 0:
            continue

        # Small recency boost — only applied to tracks that already matched
        if song['path'] in recent:
            score += 2

        results.append((score, song))

    results.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in results]