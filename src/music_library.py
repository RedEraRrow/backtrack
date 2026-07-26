"""Library scanning, metadata extraction, search, sort, and background sync."""
from __future__ import annotations
import os
import json
import tempfile
import threading
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from src.history import get_recent_paths


VALID_AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac')
SYNC_INTERVAL_SECONDS = 30


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

# Background sync state
_sync_thread: threading.Thread | None = None
_sync_trigger = threading.Event()
_cache_mtime = 0
_sync_lock = threading.Lock()
_sync_state: dict[str, Any] = {
    "library": None,
}


def to_num(v: Any) -> float:
    """Convert value to number, handling fractions like '1/12'."""
    try:
        s = str(v).strip()
        if '/' in s:
            return float(s.split('/')[0])
        return float(s) if s else 0.0
    except (ValueError, TypeError, IndexError):
        return 0.0


def start_background_sync(library: list) -> None:
    global _sync_thread

    with _sync_lock:
        _sync_state["library"] = library

        if _sync_thread and _sync_thread.is_alive():
            _sync_trigger.set()
            return

        _sync_thread = threading.Thread(
            target=_sync_worker,
            args=(library,),
            daemon=True,
        )
        _sync_thread.start()


def _reconcile_library(library: list, music_dir: str, ignore_hidden: bool = False) -> bool:
    """Add files that appeared and drop entries whose file vanished, under
    `music_dir` (an external rename/move shows up as a remove + an add). Cheap:
    a directory walk (no tag parsing) diffed against the known paths; tags are
    read only for genuinely new files. Mutates `library` in place; returns True
    if anything changed.

    Guarded against a temporarily unavailable directory (unmounted/network
    drive): if the scan finds no files while the cache still holds entries under
    `music_dir`, nothing is removed — otherwise the whole cache would be wiped.
    """
    if not music_dir or not os.path.isdir(music_dir):
        return False

    on_disk: set[str] = set()
    for root, _, files in os.walk(music_dir):
        for f in files:
            if f.lower().endswith(VALID_AUDIO_EXTENSIONS):
                on_disk.add(os.path.join(root, f))

    known = {track['path'] for track in library}
    under = {p for p in known if p == music_dir or p.startswith(music_dir + os.sep)}
    if not on_disk and under:
        return False                                  # unmount / empty-scan guard

    changed = False
    removed = under - on_disk
    if removed:
        library[:] = [t for t in library if t['path'] not in removed]
        changed = True

    current = {track['path'] for track in library}
    for path in sorted(on_disk - current):
        meta = get_metadata(path)
        if ignore_hidden and meta.get('grouping') == 'HIDDEN':
            continue
        library.append(meta)
        changed = True
    return changed


def _sync_worker(library: list) -> None:
    """Worker thread for background library synchronization."""
    global _cache_mtime
    from src.utils import ui_utils

    while True:
        library = _sync_state.get("library") or library

        ui_utils.set_status("sync", "Checking library for updates...")
        changed = False

        # Reconcile with the filesystem: pick up files added/removed/renamed
        # outside the app (a rename is just a remove + an add).
        try:
            from src.config import load_config
            cfg = load_config()
            music_dir = os.path.abspath(os.path.expanduser(cfg.get('music_directory', '') or ''))
            ignore_hidden = bool(cfg.get('ignore_hidden_files', False))
        except Exception:
            music_dir, ignore_hidden = '', False
        if _reconcile_library(library, music_dir, ignore_hidden):
            changed = True

        # Check for modified files
        path_map = {track['path']: track for track in library}
        for i, (path, track) in enumerate(path_map.items()):
            if i % 20 == 0:
                ui_utils.set_status("sync", f"Syncing library ({i}/{len(library)})")

            try:
                current_mtime = os.path.getmtime(path)
            except OSError:
                continue

            if current_mtime != track.get('cached_mtime', 0) or 'people' not in track:
                fresh = get_metadata(path)
                track.update(fresh)
                track.pop('performers', None)
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
        "movement_number": "0",
        "total_movements": "0",
        "play_count": 0,
        "bpm": "0",
        "people": "",
        "duration": 0.0,
        "cached_mtime": 0,
    }


def get_song_duration(file_path: str) -> float:
    """
    Get audio duration in seconds.

    Returns:
        Duration in seconds, or 0 if unavailable
    """
    try:
        return MP3(file_path).info.length
    except (mutagen.MutagenError, OSError, AttributeError):  # type: ignore[reportPrivateImportUsage]
        return 0


def get_metadata(file_path: str) -> dict:
    """
    Extract metadata from audio file.

    All required metadata fields are initialized to prevent KeyErrors.
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
        except (mutagen.MutagenError, OSError):  # type: ignore[reportPrivateImportUsage]
            pass

    # Cache the audio duration (seconds) so track lists can show it without
    # re-reading every file during browse.
    try:
        mf = mutagen.File(file_path)  # type: ignore[reportPrivateImportUsage]
        if mf is not None and getattr(mf, "info", None) is not None:
            metadata["duration"] = float(getattr(mf.info, "length", 0.0) or 0.0)
    except Exception:
        pass

    return metadata


def _extract_id3_metadata(tags: ID3) -> dict:
    result = {}

    # Standard text frames
    frame_map = {
        'TIT2': 'title',
        'TPE1': 'artist',
        'TPE2': 'album_artist',
        'TALB': 'album',
        'TCON': 'genre',
        'TDRC': 'year',
        'TIT3': 'work',
        'TBPM': 'bpm',
    }

    for frame_id, field in frame_map.items():
        if frame_id in tags:
            # Multi-value frames (#60, e.g. artist/genre) join with '; ' so every
            # value is searchable and shown; single-value frames are unaffected.
            vals = [str(t) for t in tags[frame_id].text] if tags[frame_id].text else []
            result[field] = "; ".join(vals)

    # Track and disc numbers
    if 'TRCK' in tags:
        parts = tags['TRCK'].text[0].replace('⁄', '/').split('/') if tags['TRCK'].text else []
        result['track'] = str(parts[0]) if parts else '0'
        result['total_tracks'] = str(parts[1]) if len(parts) > 1 else '0'
    if 'TPOS' in tags:
        parts = tags['TPOS'].text[0].replace('⁄', '/').split('/') if tags['TPOS'].text else []
        result['disc'] = str(parts[0]) if parts else '0'
        result['total_discs'] = str(parts[1]) if len(parts) > 1 else '0'

    # Classical music extensions
    if 'MVIN' in tags:
        parts = tags['MVIN'].text[0].replace('⁄', '/').split('/') if tags['MVIN'].text else []
        result['movement_number'] = str(parts[0]) if parts else '0'
        result['total_movements'] = str(parts[1]) if len(parts) > 1 else '0'
    if 'MVNM' in tags:
        result['movement_name'] = str(tags['MVNM'].text[0]) if tags['MVNM'].text else ""
    if 'TSST' in tags:
        result['disc_subtitle'] = str(tags['TSST'].text[0]) if tags['TSST'].text else ""

    # Grouping (TIT1 = Content Group / Grouping per ID3 spec)
    if 'TIT1' in tags:
        result['grouping'] = str(tags['TIT1'].text[0]) if tags['TIT1'].text else ""

    # Work name — try sources in priority order:
    #   1. TXXX:WORK (MusicBrainz Picard / standard classical convention)
    #   2. TIT1 (Content Group / Grouping — iTunes and many editors store the work here)
    #   3. TIT3 (Subtitle) is already mapped via frame_map above
    if not result.get('work'):
        for txxx_key in ('TXXX:WORK', 'TXXX:work', 'TXXX:Work'):
            frame = tags.get(txxx_key)
            if frame and frame.text:
                result['work'] = str(frame.text[0]).strip()
                break
    if not result.get('work') and result.get('grouping'):
        result['work'] = result['grouping']

    # People from TMCL (performers) / TIPL (involved people). Store each as
    # "Name (Role)" when a role is present so search matches — and the results
    # people column can show — both the person and their role/character.
    people_entries = []
    for frame_id in ('TMCL', 'TIPL'):
        frame = tags.get(frame_id)
        if frame and hasattr(frame, 'people'):
            for role, name in frame.people:
                n, r = name.strip(), role.strip()
                if n:
                    people_entries.append(f"{n} ({r})" if r else n)
    if people_entries:
        result['people'] = ', '.join(people_entries)

    return result


def _extract_mp4_metadata(tags: MP4) -> dict:
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

    # Grouping (©grp) — fall back to it as work name if ©wrk is absent
    try:
        if '©grp' in tags and tags['©grp']:
            val = tags['©grp']
            grp = str(val[0]).strip() if val else ""
            if grp:
                result['grouping'] = grp
                if not result.get('work'):
                    result['work'] = grp
    except (KeyError, IndexError, TypeError):
        pass

    return result


def build_library(directory: str, ignore_hidden: bool = False) -> list:
    """
    Build music library from directory.

    Args:
        directory: Root directory to scan
        ignore_hidden: Skip tracks marked HIDDEN

    Returns:
        List of track metadata dictionaries
    """
    library = []
    # Resolve to an absolute path so stored track paths remain valid no matter
    # what the working directory is when the library is later loaded.
    directory = os.path.abspath(os.path.expanduser(directory))
    for root, _, files in os.walk(directory):
        for file in files:
            if file.lower().endswith(VALID_AUDIO_EXTENSIONS):
                full_path = os.path.join(root, file)
                meta = get_metadata(full_path)
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
                _sync_trigger.set()
        except (OSError, TypeError, ValueError) as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            from src.utils.ui_utils import show_status as _show
            _show(f"Cache save error: {e}", duration=4.0)

    if _async:
        threading.Thread(target=_write, daemon=True).start()
    else:
        _write()


def load_library_cache() -> list:
    global _cache_mtime
    if not os.path.exists(CACHE_PATH):
        _cache_mtime = 0
        return []

    try:
        with open(CACHE_PATH, 'r') as f:
            library = json.load(f)
        _cache_mtime = os.path.getmtime(CACHE_PATH)
        for track in library:
            track.pop('performers', None)
        return library
    except (json.JSONDecodeError, IOError):
        _cache_mtime = 0
        return []


def refresh_library_entry(library: list, file_path: str) -> dict:
    """
    Re-read and update metadata for a single file.

    Args:
        library: Library to update
        file_path: Path to file to refresh

    Returns:
        Updated track metadata
    """
    fresh = get_metadata(file_path)
    for i, track in enumerate(library):
        if track['path'] == file_path:
            library[i] = fresh
            break
    else:
        library.append(fresh)

    save_library_cache(library)
    return fresh


def get_grouped_data(library: list, category: str) -> dict:
    """Group library by category."""
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
    """Sort tracks by artist, year, album, disc, and track number."""
    def get_sortable_name(display_name: str, sort_order: str | None) -> str:
        if sort_order and str(sort_order).strip():
            return str(sort_order).lower()
        if not display_name:
            return ""
        name = str(display_name).lower()
        if name.startswith("the "):
            return name[4:].strip()
        return name

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

        mv_num = to_num(track.get('movement_number', 0))

        return (
            get_sortable_name(artist, artist_sort),
            -clean_year,
            get_sortable_name(album, album_sort),
            to_num(track.get('disc', 1)),
            to_num(track.get('track', 0)),
            mv_num,
        )

    return sorted(tracks, key=sort_key)


def sort_album_tracks(tracks: list) -> list:
    """Sort tracks within a single album by disc → track → movement.

    Does not consider artist or album — callers have already scoped to one album.
    """
    def key(track: dict) -> tuple:
        disc = to_num(track.get('disc', 0))
        trk  = to_num(track.get('track', 0))
        mv   = to_num(track.get('movement_number', 0))
        return (disc, trk, mv)

    return sorted(tracks, key=key)


def search_library(library: list, query: str,
                   active_tags: list[str] | None = None) -> list:
    """
    Search library with AND logic and relevance scoring.

    Scoring:
        - Exact match: 4x weight
        - Start-of-field: 2x weight
        - Substring: 1x weight
        - Recent play: +2 bonus
    """
    recent = get_recent_paths()
    active_tags = active_tags or ['title', 'artist', 'album', 'genre', 'people']
    weights = {'title': 10, 'artist': 8, 'album': 5, 'genre': 3, 'people': 2}

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
