"""Library scanning, metadata extraction, grouping, sort, and background sync."""
from __future__ import annotations
import os
import json
import re
import tempfile
import threading
from collections import Counter
from pathlib import Path
from typing import Any

import mutagen
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4

from src.history import get_recent_paths


VALID_AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac')
SYNC_INTERVAL_SECONDS = 30

# Bump whenever `_get_default_metadata` gains a field or an extractor learns a new
# tag: cached entries carrying an older version are re-read on the next sync even
# though their mtime hasn't moved. (Before this, each new field needed its own
# `'field' not in track` special case.)
METADATA_VERSION = 5


def _default_cache_dir() -> Path:
    """The platform-conventional cache directory when no override is set."""
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


_YEAR_RE = re.compile(r'(\d{4})')


def year_of(value: Any) -> int:
    """The year in any date form — '1970', '1970-04-01', '2005-09-15 18:30:00' —
    or 0 when there is none ('Unknown Year', '').

    ID3's TDRC is a *timestamp*, so `int(str(value))` raised for every dated file
    and silently treated it as year-less. Matching the first four-digit run is what
    the now-playing panel already did.
    """
    m = _YEAR_RE.search(str(value or ''))
    return int(m.group(1)) if m else 0


def album_year(songs: list) -> int:
    """One representative year for an album: the **earliest corroborated** year —
    the oldest that at least two tracks share, or the oldest of all when every
    track carries a different date. 0 if none of them carry a year.

    A per-track year says when that *recording* is from, so an album can hold
    many. Asking for corroboration means a single stray or mis-tagged track can't
    drag the album backwards (a `Bridge Over Troubled Water` with one 1969 track
    stays 1970), while a series whose episodes all differ still reads as the year
    it began rather than its busiest year.
    """
    counts = Counter(y for y in (year_of(s.get('year')) for s in songs) if y)
    if not counts:
        return 0
    shared = [y for y, c in counts.items() if c > 1]
    return min(shared) if shared else min(counts)


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
    """Start (or wake) the daemon thread that periodically reconciles the library
    against disk. Safe to call repeatedly — a live thread is just re-triggered."""
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


def live_library() -> list | None:
    """The in-memory library the background sync keeps current, or None when
    no sync is running (a bare CLI invocation)."""
    return _sync_state.get("library")


def _reconcile_library(library: list, music_dirs, ignore_hidden: bool = False) -> bool:
    """Add files that appeared and drop entries whose file vanished, under any of
    `music_dirs` (an external rename/move shows up as a remove + an add). Cheap:
    a directory walk (no tag parsing) diffed against the known paths; tags are
    read only for genuinely new files. Mutates `library` in place; returns True
    if anything changed.

    Guarded against a temporarily unavailable directory (unmounted/network
    drive) **per root**: a root that scans empty while the cache still holds
    entries under it is skipped entirely, so one absent drive can neither wipe
    its own tracks nor those of the roots that are present.
    """
    # Normalise (case + separators) so a trailing slash or a case-insensitive
    # filesystem doesn't misclassify which cached paths live under a root.
    def _np(p: str) -> str:
        return os.path.normcase(os.path.normpath(p))

    known = {track['path'] for track in library}

    def _under(root: str) -> set[str]:
        """Cached paths that live under `root`."""
        md = _np(root)
        return {p for p in known if _np(p) == md or _np(p).startswith(md + os.sep)}

    on_disk: set[str] = set()          # everything found across the live roots
    covered: set[str] = set()          # cached paths belonging to those roots
    for music_dir in as_dir_list(music_dirs):
        if not os.path.isdir(music_dir):
            continue                                  # missing root: leave it alone
        found: set[str] = set()
        for root, _, files in os.walk(music_dir):
            for f in files:
                if f.lower().endswith(VALID_AUDIO_EXTENSIONS):
                    found.add(os.path.join(root, f))
        under = _under(music_dir)
        if not found and under:
            continue                                  # unmount / empty-scan guard
        on_disk |= found
        covered |= under

    if not on_disk and not covered:
        return False

    changed = False
    # Roots can nest, so removal is judged against every live root at once: a
    # path still present under one of them is never dropped for missing another.
    removed = covered - on_disk
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

        ui_utils.set_status("sync", "Checking library for updates…")
        changed = False

        # Reconcile with the filesystem: pick up files added/removed/renamed
        # outside the app (a rename is just a remove + an add).
        try:
            from src.config import load_config, music_dirs as _music_dirs
            cfg = load_config()
            roots = _music_dirs(cfg)
            ignore_hidden = bool(cfg.get('ignore_hidden_files', False))
        except Exception:
            roots, ignore_hidden = [], False
        if _reconcile_library(library, roots, ignore_hidden):
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

            if (current_mtime != track.get('cached_mtime', 0)
                    or track.get('meta_version') != METADATA_VERSION):
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
        "composer": "",
        "lyricist": "",
        "year": "Unknown Year",
        "grouping": "",
        "work": "",
        "movement_name": "",
        "movement_number": "0",
        "total_movements": "0",
        "play_count": 0,
        "bpm": "0",
        "people": "",
        # The same credits as exact [name, role] pairs. `people` is one string
        # for search to match against, and can't be split back apart: a role
        # may itself contain commas ("Sundry Ruffians, Publishers, and …").
        "credits": [],
        "duration": 0.0,
        "cached_mtime": 0,
        "meta_version": METADATA_VERSION,
    }


def get_song_duration(file_path: str) -> float:
    """
    Get audio duration in seconds, for any supported format (MP3 and MP4/M4A/…).

    Returns:
        Duration in seconds as a float, or 0.0 if unavailable
    """
    try:
        audio = mutagen.File(file_path)  # type: ignore[reportPrivateImportUsage]
        if audio is not None and audio.info is not None:
            return float(audio.info.length)
    except (mutagen.MutagenError, OSError, AttributeError):  # type: ignore[reportPrivateImportUsage]
        pass
    return 0.0


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
    """Map an ID3 tag object's frames to the library's metadata field names."""
    result = {}

    # Standard text frames
    frame_map = {
        'TIT2': 'title',
        'TPE1': 'artist',
        'TPE2': 'album_artist',
        'TALB': 'album',
        'TCON': 'genre',
        'TCOM': 'composer',
        'TEXT': 'lyricist',
        'TDRC': 'year',
        'TIT3': 'work',
        'TBPM': 'bpm',
        # Sort-order frames, under the friendly names the sort keys look up.
        'TSOT': 'Title Sort Order',
        'TSOP': 'Performer Sort Order',
        'TSO2': 'Album Artist Sort Order',
        'TSOA': 'Album Sort Order',
        'TSOC': 'Composer Sort Order',
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
    credits = []
    for frame_id in ('TMCL', 'TIPL'):
        frame = tags.get(frame_id)
        if frame and hasattr(frame, 'people'):
            for role, name in frame.people:
                n, r = name.strip(), role.strip()
                if n:
                    credits.append([n, r])
    if credits:
        result['credits'] = credits
        result['people'] = ', '.join(f"{n} ({r})" if r else n for n, r in credits)

    return result


def _extract_mp4_metadata(tags: MP4) -> dict:
    """Map an MP4 tag object's atoms to the library's metadata field names."""
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
        '\xa9wrt': 'composer',
        '\xa9day': 'year',
        '\xa9wrk': 'work',
        'tmpo': 'bpm',
        # Sort atoms — the MP4 counterparts of the TSO* frames.
        'sonm': 'Title Sort Order',
        'soar': 'Performer Sort Order',
        'soaa': 'Album Artist Sort Order',
        'soal': 'Album Sort Order',
        'soco': 'Composer Sort Order',
    }

    for mp4_atom, field in field_map.items():
        try:
            if mp4_atom in tags:
                val = tags[mp4_atom]
                if val:
                    # Handle both list and direct values. A multi-value atom
                    # (artist/genre) joins with '; ' — same shape as the ID3 side,
                    # so browsing splits it back into one group per value.
                    if isinstance(val, list):
                        parts = [str(v).strip() for v in val if str(v).strip()]
                        text = "; ".join(parts)
                    else:
                        text = val
                    if text:
                        result[field] = str(text).strip()
        except (KeyError, IndexError, TypeError):
            continue

    # Freeform ('----') atoms. MP4 has no standard atom for some fields the ID3
    # side covers, and iTunes puts them here instead. Values come back as bytes,
    # so they are decoded rather than str()'d — str() on bytes yields "b'...'".
    freeform_map = {
        '----:com.apple.iTunes:LYRICIST': 'lyricist',
    }
    for atom, field in freeform_map.items():
        try:
            if atom in tags and tags[atom] and not result.get(field):
                parts = []
                for v in tags[atom]:
                    text = v.decode('utf-8', 'replace') if isinstance(v, bytes) else str(v)
                    if text.strip():
                        parts.append(text.strip())
                if parts:
                    result[field] = "; ".join(parts)
        except (KeyError, IndexError, TypeError, UnicodeDecodeError):
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


def as_dir_list(directories) -> list[str]:
    """One directory or many → a list of absolute paths (blanks dropped).

    Every scanning entry point takes either, so a caller holding a single root
    (first run, a test) needs no ceremony.
    """
    if isinstance(directories, (str, os.PathLike)):
        directories = [directories]
    out: list[str] = []
    seen: set[str] = set()
    for d in directories or []:
        full = os.path.abspath(os.path.expanduser(str(d).strip())) if str(d).strip() else ''
        key = os.path.normcase(full)
        if full and key not in seen:
            seen.add(key)
            out.append(full)
    return out


def build_library(directories, ignore_hidden: bool = False) -> list:
    """
    Build the music library by scanning one or several root directories.

    Args:
        directories: A root directory, or a list of them
        ignore_hidden: Skip tracks marked HIDDEN

    Returns:
        List of track metadata dictionaries
    """
    library = []
    # Absolute paths, so stored track paths stay valid no matter what the working
    # directory is when the library is later loaded. Roots may nest (one inside
    # another), so a path already seen is skipped rather than scanned twice.
    seen: set[str] = set()
    for directory in as_dir_list(directories):
        for root, _, files in os.walk(directory):
            for file in files:
                if not file.lower().endswith(VALID_AUDIO_EXTENSIONS):
                    continue
                full_path = os.path.join(root, file)
                key = os.path.normcase(full_path)
                if key in seen:
                    continue
                seen.add(key)
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
        """Write to a temp file in the cache dir, then atomically replace the cache."""
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
    """Load the cached library from disk, or [] if missing/corrupt."""
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


def refresh_library_entry(library: list | None, file_path: str) -> dict:
    """
    Re-read and update metadata for a single file.

    Args:
        library: Library to update
        file_path: Path to file to refresh

    Returns:
        Updated track metadata

    `library` None means the caller isn't tracking one: the fresh entry is
    returned and nothing is saved. Never pass a stand-in list for "no library"
    — it would be saved as the whole cache, one track long.
    """
    fresh = get_metadata(file_path)
    if library is None:
        return fresh
    for i, track in enumerate(library):
        if track['path'] == file_path:
            library[i] = fresh
            break
    else:
        library.append(fresh)

    save_library_cache(library)
    return fresh


# Values from a multi-value frame reach the library joined with '; ' (see
# `_extract_id3_metadata`). List-like fields are split apart again for browsing,
# so a track tagged "Pop; Rock" is filed under Pop *and* under Rock rather than
# under a merged "Pop; Rock" pseudo-genre — both entries lead to the same album.
# Fields whose text is one single title (album, grouping) are never split: a
# semicolon there belongs to the name.
_LIST_FIELDS = frozenset({'artist', 'album_artist', 'genre', 'composer', 'lyricist'})


def split_tag_values(value: Any) -> list[str]:
    """Split a joined multi-value tag into its individual values, in tag order.

    Case-insensitive duplicates collapse, so one track can never land twice in
    the same group. An empty (or all-whitespace) value yields [].
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in str(value or '').split(';'):
        part = part.strip()
        if part and part.lower() not in seen:
            seen.add(part.lower())
            out.append(part)
    return out


def format_tag_values(value: Any) -> str:
    """Render a stored multi-value field for the screen: "Ada Lark, Bo Vale".

    Values are *stored* semicolon-joined (that is what the tag frames hold and
    what round-trips through the editors) and only *displayed* comma-separated,
    which reads as a list rather than as machine output.

    Only for list-like fields — artist, album artist, genre, composer, lyricist, people.
    Never pass a single-title field: an album really called "Songs; Ohia" would
    come back as "Songs, Ohia".

    One exception keeps the display honest: when a value contains a comma of its
    own (a surname-first credit like "Karajan, Herbert"), comma-separating the
    list would read as four names instead of two, so the stored separator is kept
    for that field.
    """
    return format_value_list(split_tag_values(value))


def format_value_list(values: list[str]) -> str:
    """Render already-separated values for the screen — the list-taking form of
    `format_tag_values`, for callers holding a frame's real value list (no
    re-splitting, so a value containing ';' stays whole).
    """
    vals = [v for v in (str(x).strip() for x in values) if v]
    sep = "; " if any("," in v for v in vals) else ", "
    return sep.join(vals)


def group_values(field: str, value: Any) -> list[str]:
    """The group name(s) one track's `field` value contributes to."""
    if field in _LIST_FIELDS:
        return split_tag_values(value)
    val = str(value or '').strip()
    return [val] if val else []


def people_names(song: dict) -> list[str]:
    """Everyone credited on a track (from its exact name/role pairs), each once,
    in credit order."""
    return list(dict.fromkeys(n for n, _r in song.get('credits') or [] if n))


def derive_album_credit(songs: list) -> str:
    """The credit an album carrying no album-artist tag is filed under ('' if its
    tracks name no artist at all).

    Each track's artist tag is one *cast* — a multi-value credit "A; B" is a duo,
    not two separate artists. The album is filed under its **anchor**: the artists
    credited on every track. So a duet album survives a guest appearance on one
    track, while a genuinely disjoint line-up (nobody credited throughout) is a
    compilation and collapses to "Various Artists".
    """
    casts = [c for c in (split_tag_values(s.get("artist")) for s in songs) if c]
    if not casts:
        return ""

    anchor = set.intersection(*({v.lower() for v in c} for c in casts))
    if not anchor:
        return "Various Artists"
    # Anchor names in first-credited order, keeping the tag's own casing.
    return "; ".join(v for v in casts[0] if v.lower() in anchor)


def get_grouped_data(library: list, category: str) -> dict:
    """Group library by category.

    A multi-value **genre** files the track under each of its values (see
    `_LIST_FIELDS`); a multi-value **artist credit** stays whole, because two
    names on one album are a joint billing rather than two separate acts.
    """
    grouped = {}

    if category == "artist":
        # Prefer album_artist for artist grouping; with none, derive the album's
        # credit from its track casts (see derive_album_credit).
        album_groups: dict[tuple[str, str], list[dict]] = {}
        for song in library:
            album = (song.get("album") or "Unknown").strip()
            album_artist = (song.get("album_artist") or "").strip()
            album_groups.setdefault((album, album_artist), []).append(song)

        compilation_map: dict[tuple[str, str], str] = {
            key: (key[1] or derive_album_credit(songs) or "Unknown")
            for key, songs in album_groups.items()
        }

        for song in library:
            album = (song.get("album") or "Unknown").strip()
            album_artist = (song.get("album_artist") or "").strip()
            val = compilation_map.get((album, album_artist), "Unknown")

            values = split_tag_values(val) or ["Unknown"]

            if len(values) == 1 and song.get("genre", "").lower() == "classical":
                # Classical single-value credit: one string jamming several names
                # together ("Karajan, Herbert & Berlin Phil") files under the first.
                name = values[0].split(',')[0].split('&')[0].strip() or values[0]
            else:
                # A credit naming several people is one *billing*, so it gets one
                # entry reading "Ada Lark, Bo Vale" — not an entry per name. (Genre
                # is the opposite: its values are independent facets and do split.)
                name = format_tag_values(val) or "Unknown"

            grouped.setdefault(name, []).append(song)
        return grouped

    if category in ("year", "decade"):
        # By the year in any date form; a decade is "1990s". Undated tracks
        # are left out rather than filed under an "Unknown" year.
        for song in library:
            y = year_of(song.get("year"))
            if y:
                grouped.setdefault(str(y) if category == "year" else f"{y // 10 * 10}s", []).append(song)
        return grouped

    if category == "people":
        # Each credited person once, whatever their role(s), so someone in
        # several shows is one entry.
        for song in library:
            for name in people_names(song):
                grouped.setdefault(name, []).append(song)
        return grouped

    for song in library:
        vals = group_values(category, song.get(category)) or ["Unknown"]

        for val in vals:
            # Optional credits: a track without one isn't filed under "Unknown"
            # (that would be most of the library in one row).
            if category in ("grouping", "composer", "lyricist", "work") and val == "Unknown":
                continue

            grouped.setdefault(val, []).append(song)

    return grouped


# Which tag pairs with which credit, per browse category, best source first: the
# displayed name comes from the first field, its sort key from the second.
_SORT_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    'artist':       (('album_artist', 'Album Artist Sort Order'),
                     ('artist', 'Performer Sort Order')),
    'album_artist': (('album_artist', 'Album Artist Sort Order'),
                     ('artist', 'Performer Sort Order')),
    'album':        (('album', 'Album Sort Order'),),
}


def _sort_text(value: str) -> str:
    """Normalise one sort-order value into a comparable key."""
    key = value.strip().lower()
    return key[:-5].strip() if key.endswith(', the') else key


def _tagged_sort_key(display_name: str, songs: list, category: str) -> str | None:
    """The sort-order value that belongs to this group, or None if none applies.

    A multi-value credit and its sort frame pair up **by position** —
    `TPE2 = "Cee Dot" / "Dee Ray"` alongside `TSO2 = "Dot, Cee" / "Ray, Dee"`. A
    group named after the whole billing takes the first value's sort name; a group
    named after one artist may only claim the value at its own index, never a
    co-credited artist's. Counts that disagree are ambiguous, and a derived group
    name ("Various Artists", a classical-normalised surname) belongs to no index
    at all: both fall through to the display name.
    """
    target = display_name.strip().lower()
    for song in songs:
        for name_field, sort_field in _SORT_PAIRS.get(category, ()):
            names = group_values(name_field, song.get(name_field))
            sorts = split_tag_values(song.get(sort_field))
            if not sorts or len(sorts) != len(names):
                continue
            # A joint billing ("Cee Dot, Dee Ray") is one group, and files under
            # the first artist's sort name.
            if len(names) > 1 and format_tag_values(song.get(name_field)).lower() == target:
                return _sort_text(sorts[0])
            for name, sort in zip(names, sorts):
                if name.strip().lower() == target:
                    return _sort_text(sort)
    return None


def get_group_sort_key(display_name: str, songs: list, category: str,
                       cfg: dict | None = None) -> str:
    """
    Sort key for group name.

    Priority:
        1. Explicit sort-order tag (TSOP/TSO2/TSOA), matched to this group by
           position within a multi-value credit — unless sort tags are off
        2. Display name through `sort_text` (leading words dropped, case and
           accents folded)
    """
    opts = sort_options(cfg)
    if opts['use_tags']:
        tagged = _tagged_sort_key(display_name, songs, category)
        if tagged:
            return tagged
    return sort_text(display_name, opts)


# --- Sorting ------------------------------------------------------------------
# One engine orders every list of albums and tracks: a *chain* of levels, each a
# (field, direction) pair, compared in turn. Album-level fields are worked out
# once per album, so an album can only be split up by a track-level field placed
# ahead of it (broadcast order does this on purpose). Missing values always sort
# last, whichever the direction.

# field → (label, level, kind)
SORT_FIELDS: dict[str, tuple[str, str, str]] = {
    'album':        ("Album",          'album', 'text'),
    'album_artist': ("Album artist",   'album', 'text'),
    'album_year':   ("Album year",     'album', 'num'),
    'disc':         ("Disc",           'track', 'num'),
    'track':        ("Track number",   'track', 'num'),
    'movement':     ("Movement",       'track', 'num'),
    'title':        ("Title",          'track', 'text'),
    'artist':       ("Track artist",   'track', 'text'),
    'date':         ("Date",           'track', 'date'),
    'play_count':   ("Play count",     'track', 'num'),
    'duration':     ("Duration",       'track', 'num'),
}

DEFAULT_SORT_LEVELS = [['album', 'asc'], ['album_year', 'asc'], ['disc', 'asc'],
                       ['track', 'asc'], ['title', 'asc'], ['date', 'asc']]

_opts_cache: dict = {}


def sort_options(cfg: dict | None = None) -> dict:
    """The sort settings: `levels`, `use_tags`, `ignore_words`, `libraries`
    (directory → its own levels). Read from `cfg`, else from the config file
    (cached on its mtime — group keys are computed per row, per render)."""
    from src.config import music_dirs
    if cfg is None:
        from src.config import CONFIG_FILE, load_config
        try:
            mtime = CONFIG_FILE.stat().st_mtime
        except OSError:
            mtime = None
        if _opts_cache.get('mtime') != mtime or 'cfg' not in _opts_cache:
            _opts_cache.update(mtime=mtime, cfg=load_config())
        cfg = _opts_cache['cfg']
    return {
        'levels':       valid_levels(cfg.get('sort_levels')) or DEFAULT_SORT_LEVELS,
        'use_tags':     cfg.get('sort_use_tags', True),
        'ignore_words': [w.strip().lower() for w in cfg.get('sort_ignore_words', ['The', 'A', 'An'])
                         if w.strip()],
        'libraries':    {d: valid_levels(l) for d, l in (cfg.get('library_sort_levels') or {}).items()
                         if valid_levels(l)},
        'dirs':         music_dirs(cfg),
    }


def valid_levels(levels) -> list:
    """A stored chain with unknown fields and bad directions dropped."""
    out = []
    for lv in levels or []:
        if (isinstance(lv, (list, tuple)) and len(lv) == 2
                and lv[0] in SORT_FIELDS and lv[1] in ('asc', 'desc')):
            out.append([lv[0], lv[1]])
    return out


def describe_levels(levels: list) -> str:
    """One-line summary of a chain, e.g. "Album ↑, Album year ↑, Disc ↑"."""
    return ", ".join(f"{SORT_FIELDS[f][0]} {'↑' if d == 'asc' else '↓'}" for f, d in levels)


def sort_text(value, opts: dict | None = None) -> str:
    """A name as it sorts: casefolded, accents folded (Café = Cafe), and a
    leading ignored word dropped ("The Beatles" → "beatles")."""
    import unicodedata
    opts = opts or sort_options()
    s = unicodedata.normalize('NFKD', str(value or '').strip())
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    for w in opts['ignore_words']:
        if s.startswith(w + " ") and len(s) > len(w) + 1:
            return s[len(w) + 1:].strip()
    return s


def _natural(s: str) -> tuple:
    """Split digits out so "vol. 2" sorts before "vol. 10". Always starts with
    a str, so any two keys compare element by element without a type clash."""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r'(\d+)', s))


def library_of(path: str, dirs: list) -> str | None:
    """The configured directory a file lives under (the deepest, when they nest)."""
    best = None
    p = os.path.normcase(os.path.abspath(path))
    for d in dirs:
        nd = os.path.normcase(os.path.abspath(d))
        if (p == nd or p.startswith(nd.rstrip(os.sep) + os.sep)) and (best is None or len(nd) > len(best[0])):
            best = (nd, d)
    return best[1] if best else None


def resolve_levels(tracks: list, cfg: dict | None = None) -> tuple[list, str | None]:
    """The chain a set of tracks sorts by, and whose it is: a library's own
    order when every track lives in that one library, else the shared order
    (owner None)."""
    opts = sort_options(cfg)
    if opts['libraries'] and tracks:
        libs = {library_of(t.get('path', ''), opts['dirs']) for t in tracks}
        if len(libs) == 1:
            (lib,) = libs
            if lib in opts['libraries']:
                return opts['libraries'][lib], lib
    return opts['levels'], None


def _album_id(t: dict) -> tuple:
    return ((t.get('album') or '').strip().lower(), (t.get('album_artist') or '').strip().lower())


def _field_value(field: str, t: dict, album: list, opts: dict):
    """One track's value for `field` (album-level fields from its whole album),
    or None when it has none."""
    if field == 'album':
        tag = next((s.get('Album Sort Order') for s in album if s.get('Album Sort Order')), None)
        name = (tag if opts['use_tags'] and tag else t.get('album')) or ''
        return _natural(sort_text(name, opts)) if name.strip() else None
    if field == 'album_artist':
        tag = next((s.get('Album Artist Sort Order') for s in album if s.get('Album Artist Sort Order')), None)
        name = tag if opts['use_tags'] and tag else (_first_album_artist(album) or derive_album_credit(album))
        return _natural(sort_text(name, opts)) if name and name.strip() else None
    if field == 'album_year':
        return album_year(album) or None
    if field in ('disc', 'track', 'movement'):
        raw = t.get('movement_number' if field == 'movement' else field)
        return to_num(raw) if str(raw or '').strip() else None
    if field == 'title':
        return _natural(sort_text(t['title'], opts)) if (t.get('title') or '').strip() else None
    if field == 'artist':
        tag = t.get('Performer Sort Order')
        name = tag if opts['use_tags'] and tag else t.get('artist')
        return _natural(sort_text(name, opts)) if (name or '').strip() else None
    if field == 'date':
        d = str(t.get('year') or '').strip()
        return d if year_of(d) else None
    if field == 'play_count':
        return to_num(t.get('play_count') or 0)
    if field == 'duration':
        return to_num(t.get('duration')) or None
    return None


def _first_album_artist(album: list) -> str:
    return next(((s.get('album_artist') or '').strip() for s in album if s.get('album_artist')), '')


def _chain_sort(items: list, levels: list, value) -> list:
    """Stable multi-pass sort: the last level first, the first level last, so the
    first level wins. `value(item, field)` gives a comparable value or None;
    None sorts last in either direction."""
    out = list(items)
    for field, direction in reversed(levels):
        desc = direction == 'desc'
        vals = {id(it): value(it, field) for it in out}

        def key(it, _vals=vals, _desc=desc):
            v = _vals[id(it)]
            present = v is not None
            return (present if _desc else not present, v if present else 0)
        # Present and missing values never meet in a comparison: the flag
        # separates them first, and missing ones all share the value 0.
        out.sort(key=key, reverse=desc)
    return out


def sort_tracks(tracks: list, cfg: dict | None = None, levels: list | None = None) -> list:
    """Tracks in the sort chain's order (the resolved chain unless `levels` is
    given). Ties fall back to album, disc, track number, then path, so the
    result never depends on the order the tracks arrived in."""
    opts = sort_options(cfg)
    chain = levels if levels is not None else resolve_levels(tracks, cfg)[0]
    albums: dict = {}
    for t in tracks:
        albums.setdefault(_album_id(t), []).append(t)
    cache: dict = {}

    def value(t, field):
        k = (id(t), field)
        if k not in cache:
            cache[k] = _field_value(field, t, albums[_album_id(t)], opts)
        return cache[k]

    base = sorted(tracks, key=lambda t: t.get('path', ''))
    tiebreak = [['album', 'asc'], ['disc', 'asc'], ['track', 'asc'], ['movement', 'asc']]
    return _chain_sort(base, list(chain) + tiebreak, value)


def sort_albums(names: list, albums: dict, cfg: dict | None = None,
                levels: list | None = None) -> list:
    """Album names (each mapped to its tracks in `albums`) in the chain's
    order, using only its album-level fields; the album name breaks ties."""
    opts = sort_options(cfg)
    if levels is None:
        levels = resolve_levels([t for n in names for t in albums[n]], cfg)[0]
    chain = [lv for lv in levels if SORT_FIELDS[lv[0]][1] == 'album'] + [['album', 'asc']]

    def value(name, field):
        songs = albums[name]
        v = _field_value(field, songs[0], songs, opts) if songs else None
        return v if (v is not None or field != 'album') else _natural(sort_text(name, opts))
    return _chain_sort(sorted(names), chain, value)
