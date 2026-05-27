# Backtrack

Backtrack is a terminal music player written in Python with VLC playback, lyrics synchronization, metadata inspection, and album art rendering.

## Features

- Browse a cached music library by Artist, Album, and Genre
- Search with weighted scoring across title, artist, album, genre, and path
- Playback using VLC backend with rich terminal UI
- Embedded lyrics support for `USLT` and `SYLT`
- Interactive lyric synchronization tool
- Metadata inspection and editing for ID3/MP4 tags
- Album art rendering via `viu` and MP3 APIC extraction
- Listening history tracking
- Settings for theme, lyric lead-in, player view, and metadata editor visibility
- Background metadata sync using optional iTunes XML data

## Project Structure

The repository is organised to keep UI, app logic, and data separate.

```
backtrack/
├── main.py                 # Entry point launcher
├── src/                    # Python application code
│   ├── album_art.py        # Album art rendering and image conversion
│   ├── config.py           # Load/save app configuration
│   ├── history.py          # Listening history logging and retrieval
│   ├── lyric_timer.py      # Interactive lyric synchronization tool
│   ├── main.py             # Startup and application orchestration
│   ├── metadata_browser.py # Inspect and edit track metadata
│   ├── menus.py            # Main menu and navigation handlers
│   ├── music_library.py    # Library scanning, metadata extraction, search, and sync
│   ├── playback.py         # Playback engine, controls, and rendering
│   ├── playback_lyrics.py  # Lyrics display and SYLT/USLT handling
│   ├── prompt.py           # Terminal prompt widgets
│   ├── state.py            # Shared application state
│   ├── terminal_input.py   # Raw terminal input and escape handling
│   └── ui_utils.py         # ANSI utilities and formatting helpers
├── config/                 # Configuration directory
│   └── config.json         # User configuration file
├── data/                   # Cached and generated data
│   ├── library_cache.json  # Cached library data
│   ├── history.log         # Listening history log
│   └── Library.xml         # Optional iTunes metadata export
├── docs/                   # Project documentation
│   └── DEVELOPER.md        # Developer guide
├── requirements.txt        # Python dependencies
└── README.md               # User guide
```

## Installation

### Requirements

- Python 3.8 or newer
- System VLC / libvlc installed for playback
- `viu` CLI installed for album art rendering
- Optional: `data/Library.xml` for iTunes metadata fallback

### Install dependencies

```bash
python3 -m pip install -r requirements.txt
```

### Run the app

```bash
python3 main.py
```

On first run, Backtrack prompts for your music directory and builds the library cache.

## Usage

### Main Menu

The main menu includes:
- Browse Library
- Search
- Listening History
- Settings
- Exit

### Browse Library

Browse the library by artist, album, or genre. Groups are sorted for easier terminal navigation.

### Search

Search supports matching the selected fields and returns top ranked results.

### Playback Controls

| Key | Action |
|-----|--------|
| `SPACE` / `P` | Pause / resume |
| `N` | Stop playback and return to menu |
| `Q` | Quit the application |
| `→` | Seek forward 5 seconds |
| `←` | Seek backward 5 seconds |
| `.` | Seek forward 30 seconds |
| `,` | Seek backward 30 seconds |
| `L` | Seek forward 1 second |
| `J` | Seek backward 1 second |
| `+` / `=` | Increase volume |
| `-` / `_` | Decrease volume |
| `↑` / `↓` | Move synchronized lyric focus for USLT/SYLT lyrics |

### Player Views

Backtrack supports two playback modes:
- `default` — metadata plus album art
- `ipod` — classic iPod-style now playing screen

### Lyric Synchronisation

The Sync Lyrics tool plays a track and records timestamps as you mark each line. It writes synchronized timecodes back to the file.

### Metadata Browser

Search results expose metadata actions that let you inspect and edit ID3 frames, lyrics tags, album art, and more.

## Configuration

User settings are saved in `config/config.json`.

Key configuration options:

- `theme` — ANSI colour theme values
- `history_enabled` — enable or disable history tracking
- `search_weights` — search scoring weights
- `lyric_lead_in` — lyric sync offset in seconds
- `ascii_width` — album art rendering width
- `music_directory` — scanned music folder
- `player_view` — `default` or `ipod`
- `show_metadata_editor` — show/hide metadata editor actions

Example:

```json
{
    "theme": {
        "primary": "\u001b[1;37m",
        "accent": "\u001b[1;31m",
        "success": "\u001b[1;32m"
    },
    "history_enabled": true,
    "search_weights": {"title": 10, "artist": 8, "album": 5},
    "lyric_lead_in": 2.0,
    "ascii_width": 80,
    "music_directory": "/path/to/music",
    "player_view": "default",
    "show_metadata_editor": true
}
```

## Supported Formats

- Audio: `MP3`, `M4A`, `MP4`, `M4P`, `AAC`
- Metadata: ID3 tags, MP4 tags, `USLT`, `SYLT`
- Optional iTunes XML metadata: `data/Library.xml`
- Album art: embedded MP3 APIC frames and external images via `viu`

## Data Files

- `config/config.json` — application settings
- `data/library_cache.json` — cached music library
- `data/history.log` — listening history
- `data/Library.xml` — optional iTunes metadata export

## Development

### Run the application

```bash
python3 main.py
```

### Verify syntax

```bash
python3 -m py_compile src/*.py
```

## Notes

- `viu` is required for art rendering
- `python-vlc` requires a system VLC installation
- Library cache and history log are generated automatically

## Contributing

When contributing:
- Keep modules focused on a single responsibility
- Add docstrings and type hints for new functions
- Preserve the terminal-first workflow
- Test new features by running the app and compiling changed modules
- Update documentation when behavior or settings change
