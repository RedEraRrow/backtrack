# Developer Guide

This document explains the architecture and development practices for Backtrack.

## Architecture

Backtrack is designed as a terminal-first music player with clear separation between configuration, library management, playback, and UI.

### Project Layout

```
backtrack/
├── main.py                 # Entry point launcher
├── src/                    # Python application code
│   ├── art/
│   │   └── album_art.py            # Album art rendering via viu
│   ├── id3/
│   │   ├── bulk_id3_manager.py     # Batch metadata operations
│   │   ├── id3_browser.py          # Metadata inspection and editing UI
│   │   ├── id3_tag_handler.py      # Read/write individual ID3 tags
│   │   └── tag_registry.py         # Registry of known tag types
│   ├── playback/
│   │   ├── lyrics/
│   │   │   ├── lyric_timer.py      # Interactive lyric sync session (USLT → SYLT)
│   │   │   ├── lyrics.py           # Lyrics display and SYLT/USLT handling
│   │   │   ├── lyrics_editor.py    # Unified lyrics editor (edit, sync, tap-in)
│   │   │   └── transcript_editor.py # Backward-compatible wrapper around lyrics_editor
│   │   ├── playback.py             # Playback engine and controls
│   │   └── playback_ui.py          # Terminal UI rendering for the playback screen
│   ├── utils/
│   │   ├── prompt.py               # Terminal prompt widgets (select, checkbox, text, …)
│   │   ├── terminal_input.py       # Raw terminal input and escape sequence handling
│   │   └── ui_utils.py             # ANSI utilities, formatting helpers, status bar
│   ├── config.py           # Load/save application configuration
│   ├── history.py          # Listening history logging and retrieval
│   ├── main.py             # Startup and orchestration
│   ├── menus.py            # Navigation and menu handlers
│   ├── music_library.py    # Library scanning, metadata extraction, search, and sync
│   └── state.py            # Shared navigation state (NAV_STACK)
├── docs/                   # Documentation
│   └── DEVELOPER.md        # This file
└── requirements.txt        # Python dependencies
```

User data is stored outside the repository:
- `~/.config/backtrack/config.json` — settings (created on first run)
- `~/.cache/backtrack/library_cache.json` — cached library metadata
- `~/.config/backtrack/history.log` — listening history
- iTunes `Library.xml` path is configured in Settings (optional)

## Module Responsibilities

- `main.py` - Application launcher
- `src/main.py` - Startup flow, loading config/library, launching menu
- `src/config.py` - Manage persistent settings
- `src/music_library.py` - Scan audio files, extract metadata, search, and background sync
- `src/playback/playback.py` - Playback engine, controls, and transitions
- `src/playback/playback_ui.py` - Terminal UI rendering for the playback screen
- `src/playback/lyrics/lyrics.py` - Lyrics rendering for SYLT/USLT content
- `src/playback/lyrics/lyric_timer.py` - Interactive lyric sync session
- `src/playback/lyrics/lyrics_editor.py` - Lyrics editing UI
- `src/playback/lyrics/transcript_editor.py` - Transcript-based lyric editing
- `src/menus.py` - Primary menus: browse, search, history, settings
- `src/history.py` - History log persistence and retrieval
- `src/utils/terminal_input.py` - Raw terminal input and escape handling
- `src/art/album_art.py` - Album art capture and ASCII/ANSI rendering using `viu`
- `src/utils/ui_utils.py` - Terminal utilities, progress bars, and formatting
- `src/utils/prompt.py` - Terminal prompt abstractions
- `src/state.py` - Shared navigation state
- `src/id3/id3_browser.py` - Metadata inspection and editing UI
- `src/id3/id3_tag_handler.py` - Read/write individual ID3 tags
- `src/id3/tag_registry.py` - Registry of known tag types
- `src/id3/bulk_id3_manager.py` - Batch metadata operations

## Dependency Overview

- `python-vlc` for audio playback
- `mutagen` for ID3 and MP4 tag parsing
- `opencv-python` and `numpy` for metadata image handling
- `pyperclip` for clipboard support in metadata editing
- `viu` CLI for album art rendering

## Design Principles

### Single Responsibility
Modules are arranged so each file owns one primary domain.

### Type Hints
Use Python 3.10+ union syntax (`X | Y`, `X | None`) for type annotations. Comments are only added when the *why* is non-obvious — avoid docstrings or inline comments that restate what the code already says.

### Robust Error Handling
Recover gracefully from missing files, invalid tags, and playback errors.

### Terminal-first UX
Keep interactions keyboard-driven and compatible with narrow terminal widths.

## Key Systems

### Library & Metadata

`src/music_library.py` builds the library from the configured music folder, extracts metadata from audio tags, and can enrich tracks from an optional iTunes XML export (`data/Library.xml`).

It also maintains a background sync thread to refresh metadata and reload the cache when files change.

### Playback

`src/playback/playback.py` uses VLC to play audio and manage controls. It supports seek, volume changes, and two player view modes.

`src/playback/playback_ui.py` handles terminal UI rendering for the playback screen.

`src/playback/lyrics/lyrics.py` handles SYLT/USLT lyrics display and mapping to current playback time.

### Lyric Sync

`src/playback/lyrics/lyric_timer.py` launches an interactive sync session for tracks with `USLT` lyrics and saves timestamps as `SYLT`.

`src/playback/lyrics/lyrics_editor.py` provides a UI for editing lyric content directly.

`src/playback/lyrics/transcript_editor.py` provides transcript-based lyric editing.

### Metadata Browser

`src/id3/id3_browser.py` provides a terminal editor for ID3 tags, lyrics frames, and album art within the id3 package. It can preview artwork as half-blocks, open images, and replace APIC frames.

`src/id3/id3_tag_handler.py` handles reading and writing individual ID3 tags.

`src/id3/tag_registry.py` maintains a registry of known tag types.

`src/id3/bulk_id3_manager.py` supports batch metadata operations across multiple files.

## Adding Features

### Add a Menu Option

1. Add a new handler in `src/menus.py`.
2. Insert the command into the appropriate menu choice list.
3. Ensure the handler returns cleanly to the menu or application loop.
4. Test navigation and action flow.

### Add Playback Behavior

1. Update `src/playback/playback.py` with the new key handling or UI rendering logic.
2. Keep playback state separate from rendering code where possible.
3. Use `src/config.py` for any new user-configurable value.
4. Test with actual audio files.

### Add Metadata Support

1. Use `mutagen` patterns from existing metadata handling in `src/music_library.py`.
2. Extend `src/id3/id3_browser.py` only when UI editing is required.
3. Save changes back to the file and refresh the library cache if needed.

## Configuration Details

Persistent settings live in `~/.config/backtrack/config.json` (created automatically on first run).

Important keys:
- `music_directory` — path to the music folder
- `history_enabled` — enable/disable listening history
- `lyric_lead_in` — lyric timing offset in seconds
- `show_metadata_editor` — make the metadata editor available on track selection
- `show_lyrics_editor` — make the lyrics editor available on track selection
- `xml_db_path` — optional path to an iTunes `Library.xml` for enriched metadata

## Testing

### Syntax and Import Checks

```bash
python3 -m py_compile $(find src -name '*.py')
```

### Runtime Testing

```bash
python3 main.py
```

Verify:
- library scanning
- search results
- playback start/stop
- lyric sync
- history and settings menus

## Debugging

### Configuration Inspection

```python
from src.config import load_config
print(load_config())
```

### Library Inspection

Open `data/library_cache.json` to verify cached entries and metadata.

### Playback Debugging

Add targeted logging in `src/playback/playback.py` and `src/playback/lyrics/lyrics.py` to trace state, elapsed time, and UI refresh cycles.

## Developer Notes

- `src/state.py` — `NAV_STACK` list used by `ui_utils.get_status_line()` to render breadcrumbs in the persistent status bar
- `src/utils/prompt.py` — all terminal prompt widgets; no external dependencies
- `src/art/album_art.py` — depends on the `viu` CLI; returns an empty string gracefully when not installed
- `src/history.py` — writes to `~/.config/backtrack/history.log` as `timestamp | duration | path`
- `src/playback/lyrics/transcript_editor.py` — thin backward-compatible wrapper; new code should call `lyrics_editor()` directly

## Contributing

When contributing:
- Keep code modular and single-responsibility
- Use type annotations; avoid docstrings that restate the function name
- Preserve existing terminal UX and menu flow
- Update this file and README.md when behaviour or structure changes
