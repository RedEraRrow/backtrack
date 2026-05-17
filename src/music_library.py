"""
Music library management and metadata extraction.

Handles building library, searching, grouping, and loading metadata from files and XML.
"""

import os
import json
import urllib.parse
import re
import xml.etree.ElementTree as ET
import unicodedata
from typing import Any
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from src.history import get_recent_paths

CACHE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/library_cache.json"))
VALID_AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac')

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
    

# ============================================================================
# Metadata Extraction
# ============================================================================


def get_metadata(file_path: str, xml_db: dict | None = None, xml_title_keys: set | None = None) -> dict:
    """Extract metadata from audio file, ensuring all required keys exist."""
    # INITIALISE ALL KEYS to prevent KeyErrors
    metadata = {
        "title": os.path.splitext(os.path.basename(file_path))[0],
        "artist": "Unknown Artist",
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
            if 'TPE1' in tags: metadata["artist"] = str(tags['TPE1'])
            if 'TALB' in tags: metadata["album"]  = str(tags['TALB'])
            if 'TRCK' in tags: metadata["track"]  = str(tags['TRCK']).split('/')[0]
            if 'TCON' in tags: metadata["genre"]  = str(tags['TCON'])
            if 'TDRC' in tags: metadata["year"]   = str(tags['TDRC'])
            if 'TPOS' in tags: metadata["disc"]   = str(tags['TPOS']).split('/')[0]
            if 'TSST' in tags: metadata["disc_subtitle"] = str(tags['TSST'])
        elif file_path.endswith(('.m4a', '.m4p', '.mp4')):
            tags = MP4(file_path)
            metadata["title"] = str(tags.get('\xa9nam', [metadata["title"]])[0])
            metadata["artist"] = str(tags.get('\xa9ART', [metadata["artist"]])[0])
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
            if metadata["album"]  == "Unknown Album":  metadata["album"]  = xml_info.get("Album",   "Unknown Album")
            if metadata["title"]  == os.path.splitext(os.path.basename(file_path))[0]:
                                                       metadata["title"]  = xml_info.get("Name",    metadata["title"])
            if metadata["genre"]  == "Unknown Genre":  metadata["genre"]  = xml_info.get("Genre",   "Unknown Genre")
            if metadata["year"]   == "Unknown Year":   metadata["year"]   = str(xml_info.get("Year", "Unknown Year"))

    return metadata # ALWAYS return the dictionary


def get_song_duration(file_path: str) -> float:
    """Get duration of audio file in seconds."""
    try:
        return MP3(file_path).info.length
    except Exception:
        return 0


# ============================================================================
# Library Operations
# ============================================================================


def build_library(directory: str, xml_db: dict | None = None, xml_title_keys: set | None = None) -> list:
    """Scan directory and build library of audio files."""
    library = []
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(VALID_AUDIO_EXTENSIONS):
                library.append(get_metadata(os.path.join(root, file), xml_db, xml_title_keys))
    return library


def save_library_cache(library: list, _async: bool = True) -> None:
    """Save library to cache file. Runs in a background thread by default."""
    import threading

    def _write():
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(library, f)  # no indent — faster write, same correctness

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


def load_library_cache() -> list | None:
    """Load library from cache file."""
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def get_grouped_data(library: list, category: str) -> dict:
    """
    Group library songs by a category (artist, album, genre, grouping).
    
    Returns dict mapping category value to list of songs.
    """
    grouped = {}
    
    for song in library:
        val = song.get(category) or "Unknown"
        
        # Skip songs without grouping when browsing by grouping
        if category == "grouping" and val == "Unknown":
            continue
        
        # Special handling for classical artists
        if category == "artist" and song.get("genre", "").lower() == "classical":
            val = val.split(',')[0].split('&')[0].split(';')[0].strip()
        
        grouped.setdefault(val, []).append(song)
    
    return grouped


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