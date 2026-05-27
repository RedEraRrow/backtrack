# Developer Guide

This document explains the architecture and development practices for Backtrack.

## Architecture

Backtrack is designed as a terminal-first music player with clear separation between configuration, library management, playback, and UI.

### Project Layout

```
backtrack/
├── main.py                 # Entry point launcher
├── src/                    # Python application code
│   ├── album_art.py        # Album art rendering and image conversion
│   ├── config.py           # Load/save application configuration
│   ├── history.py          # Listening history logging and retrieval
│   ├── lyric_timer.py      # Interactive lyric synchronization tool
│   ├── main.py             # Startup and orchestration
│   ├── metadata_browser.py # Inspect and edit track metadata
│   ├── menus.py            # Navigation and menu handlers
│   ├── music_library.py    # Library scanning, metadata extraction, search, and sync
│   ├── playback.py         # Playback engine and rendering
│   ├── playback_lyrics.py  # Lyrics display and SYLT/USLT handling
│   ├── prompt.py           # Terminal prompt widgets
│   ├── state.py            # Shared application state
│   ├── terminal_input.py   # Raw terminal input handling
│   └── ui_utils.py         # ANSI utilities and formatting helpers
├── config/                 # Configuration directory
│   └── config.json         # User settings file
├── data/                   # Cached and runtime data
│   ├── library_cache.json  # Cached music library data
│   ├── history.log         # Listening history log
│   └── Library.xml         # Optional iTunes metadata export
├── docs/                   # Documentation directory
│   └── DEVELOPER.md        # This file
└── requirements.txt        # Python dependencies
```

## Module Responsibilities

- `main.py` - Application launcher
- `src/main.py` - Startup flow, loading config/library, launching menu
- `src/config.py` - Manage persistent settings
- `src/music_library.py` - Scan audio files, extract metadata, search, and background sync
- `src/playback.py` - Playback engine, UI render, controls, transitions
- `src/playback_lyrics.py` - Lyrics rendering for SYLT/USLT content
- `src/menus.py` - Primary menus: browse, search, history, settings
- `src/history.py` - History log persistence and retrieval
- `src/terminal_input.py` - Raw terminal input and escape handling
- `src/album_art.py` - Album art capture and ASCII/ANSI rendering using `viu`
- `src/ui_utils.py` - Terminal utilities, progress bars, and formatting
- `src/lyric_timer.py` - Interactive lyric sync session
- `src/metadata_browser.py` - Metadata inspection and editing UI
- `src/prompt.py` - Terminal prompt abstractions
- `src/state.py` - Shared navigation state

## Dependency Overview

- `python-vlc` for audio playback
- `mutagen` for ID3 and MP4 tag parsing
- `opencv-python` and `numpy` for metadata image handling
- `pyperclip` for clipboard support in metadata editing
- `viu` CLI for album art rendering

## Design Principles

### Single Responsibility
Modules are arranged so each file owns one primary domain.

### Type Hints and Docstrings
All public functions should use Python 3.8+ type hints and include descriptive docstrings.

### Robust Error Handling
Recover gracefully from missing files, invalid tags, and playback errors.

### Terminal-first UX
Keep interactions keyboard-driven and compatible with narrow terminal widths.

## Key Systems

### Library & Metadata

`src/music_library.py` builds the library from the configured music folder, extracts metadata from audio tags, and can enrich tracks from an optional iTunes XML export (`data/Library.xml`).

It also maintains a background sync thread to refresh metadata and reload the cache when files change.

### Playback

`src/playback.py` uses VLC to play audio and render the terminal UI. It supports seek, volume changes, two player view modes, and lyric rendering.

`src/playback_lyrics.py` handles SYLT/USLT lyrics display and mapping to current playback time.

### Lyric Sync

`src/lyric_timer.py` launches an interactive sync session for tracks with `USLT` lyrics and saves timestamps as `SYLT`.

### Metadata Browser

`src/metadata_browser.py` provides a terminal editor for ID3 tags, lyrics frames, and album art. It can preview artwork as ASCII, open images, and replace APIC frames.

## Adding Features

### Add a Menu Option

1. Add a new handler in `src/menus.py`.
2. Insert the command into the appropriate menu choice list.
3. Ensure the handler returns cleanly to the menu or application loop.
4. Test navigation and action flow.

### Add Playback Behavior

1. Update `src/playback.py` with the new key handling or UI rendering logic.
2. Keep playback state separate from rendering code where possible.
3. Use `src.config.py` for any new user-configurable value.
4. Test with actual audio files.

### Add Metadata Support

1. Use `mutagen` patterns from existing metadata handling in `src/music_library.py`.
2. Extend `src/metadata_browser.py` only when UI editing is required.
3. Save changes back to the file and refresh the library cache if needed.

## Configuration Details

Persistent settings live in `config/config.json`.

Important keys:
- `theme`
- `history_enabled`
- `search_weights`
- `lyric_lead_in`
- `ascii_width`
- `music_directory`
- `player_view`
- `show_metadata_editor`

## Testing

### Syntax and Import Checks

```bash
python3 -m py_compile src/*.py
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

Add targeted logging in `src/playback.py` and `src/playback_lyrics.py` to trace state, elapsed time, and UI refresh cycles.

## Developer Notes

- `src/state.py` stores navigation breadcrumbs
- `src/prompt.py` provides terminal prompt controls without external dependencies
- `src/album_art.py` depends on `viu` and gracefully returns an error message if it is not installed
- `src/history.py` writes `data/history.log` as `timestamp | duration | path`

## Contributing

When contributing:
- Keep code modular and readable
- Add docstrings and type hints
- Preserve existing terminal UX and menu flow
- Update documentation when behavior changes
