# Terminal Music Player

A feature-rich, terminal-based music player built in Python with support for lyrics synchronisation, library browsing, and metadata management.

## Features

### Core Features

- Browse music library by Artists, Albums, and Genres
- Advanced search with weighted scoring
- Listening history tracking
- Lyrics display (both synced and unsynced)
- Multiple playback modes (linear, shuffle, repeat)
- Beautiful terminal UI with album art in ASCII
- Customisable settings and themes

### Advanced Features

- Lyric synchronisation tool
- Metadata extraction from ID3/MP4 tags
- XML library database support (iTunes export)
- Volume and playback control
- Metadata browser and inspector
- Modular, well-organised codebase

## Project Organisation

The codebase is organised into logical folders for better maintainability:

- **`src/`** - All Python source code with modular components
- **`config/`** - User configuration files
- **`data/`** - Library cache and optional data files
- **`docs/`** - Developer documentation

This structure keeps the workspace clean and makes the codebase easier to navigate and maintain.

### Directory Structure

```
Music Browser/
├── main.py                 # Entry point launcher
├── src/                    # Source code
│   ├── main.py             # Main application logic
│   ├── config.py           # Configuration management
│   ├── music_library.py    # Library building, searching, metadata
│   ├── playback.py         # Music player engine and UI
│   ├── menus.py            # Navigation and menu handlers
│   ├── history.py          # Listening history tracking
│   ├── terminal_input.py   # Terminal input handling (raw mode, escape sequences)
│   ├── ascii_art.py        # Image to ASCII art conversion
│   ├── ui_utils.py         # General UI utilities (colours, formatting)
│   ├── lyric_timer.py      # Lyric synchronisation tool
│   ├── metadata_browser.py # Metadata inspection interface
│   ├── prompt.py           # User input utilities
│   └── state.py            # Shared global state
├── config/                 # Configuration files
│   └── config.json         # User configuration
├── data/                   # Data files
│   ├── Library.xml         # iTunes library export (optional)
│   └── library_cache.json  # Cached library data
├── docs/                   # Documentation
│   └── DEVELOPER.md        # Developer guide
├── requirements.txt        # Python dependencies
├── README.md               # This file
└── history.log             # Listening history (generated)
```

### Module Responsibilities

- **main.py** - Application entry point launcher
- **src/main.py** - Orchestrates all modules
- **src/config.py** - Load/save application configuration (themes, settings, directories)
- **src/music_library.py** - Build library, extract metadata from files/XML, search and filter
- **src/playback.py** - Audio playback engine, UI drawing, lyric display, controls
- **src/menus.py** - All navigation menus (browse, search, history, settings)
- **src/history.py** - Track and manage listening sessions
- **src/terminal_input.py** - Raw mode terminal control, arrow keys, non-blocking input
- **src/ascii_art.py** - Convert images/MP3 art to coloured ASCII
- **src/ui_utils.py** - ANSI colours, text formatting, progress bars
- **src/lyric_timer.py** - Interactive tool to sync lyrics with timestamps
- **src/metadata_browser.py** - Inspect and edit track metadata
- **src/prompt.py** - User input and path selection utilities
- **src/state.py** - Shared application state

## Installation

### Requirements

- Python 3.8 or higher
- Required packages (see `requirements.txt`)

### Setup

1. Clone or download the project

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the application:
   ```bash
   python3 main.py
   ```

On first run, select your music directory. The library will be built and cached automatically.

The application creates the following directories if they don't exist:
- `config/` - User configuration
- `data/` - Library cache and optional iTunes XML data

## Usage

### Main Menu

```
┌─ Main Menu ─────────────────────┐
│ • Browse Library                │
│ • Search                        │
│ • Listening History             │
│ • Settings                      │
│ • Exit                          │
└─────────────────────────────────┘
```

### Browse Library

Navigate by:
- **Artists** - Filtered by first letter for quick access
- **Albums** - Group by album, then by track
- **Genres** - Group by genre, then artist

Select `[Play All]` to queue an entire category.

### Music Player Controls

| Key | Action |
|-----|--------|
| `SPACE` / `P` | Pause/Resume |
| `N` | Next (back to menu) |
| `Q` | Quit all |
| `↑` / `↓` | Navigate lyrics (unsynced only) |
| `+` / `-` | Volume control |

### Search

Search across:
- Title (highest priority)
- Artist
- Album
- Genre
- File path

Results are weighted and sorted by relevance. Recently played songs get a boost.

### Listening History

View and replay recently played songs. Shows timestamp, duration, and metadata.

### Settings

- **Toggle Listening History** - Enable/disable history tracking
- **Clear History Log** - Remove all history entries
- **Adjust Lyric Lead-in** - Lyric sync timing offset
- **Change UI Theme** - Colour scheme customisation
- **Update Music Directory** - Rescan library from new location

### Lyric Synchronisation

Use `Sync Lyrics` from search or history to:
1. Play music while audio plays
2. Press ENTER when you hear each lyric line
3. Timestamps are recorded and associated with lyrics

## Configuration

Configuration is stored in `config/config.json`:

```json
{
    "theme": {
        "primary": "\033[1;37m",
        "accent": "\033[1;31m",
        "success": "\033[1;32m"
    },
    "history_enabled": true,
    "search_weights": {"title": 10, "artist": 8, "album": 5},
    "lyric_lead_in": 2.0,
    "ascii_width": 80,
    "music_directory": "/path/to/music"
}
```

## Supported Formats

- **Audio**: MP3, M4A, MP4, M4P, AAC
- **Metadata Sources**: ID3 tags, MP4 tags, iTunes Library.xml
- **Images**: JPG, JPEG, PNG (converted to ASCII)

## Data Files

- `config/config.json` - Application configuration
- `data/library_cache.json` - Cached library (for quick startup)
- `history.log` - Listening history entries
- `data/Library.xml` - iTunes library export (optional, for metadata fallback)

## Code Quality

The codebase features:

- **Modular Design** - Each module has a single responsibility  
- **Type Hints** - Python 3.8+ type annotations throughout  
- **Docstrings** - Comprehensive documentation for all functions  
- **Error Handling** - Graceful handling of missing files, corrupted tags, etc.  
- **Consistent Formatting** - PEP 8 compliant code style  
- **Separation of Concerns** - UI, business logic, and data clearly separated  

## Troubleshooting

### "No lyrics found" / Blank lyrics window
- Not all files have embedded lyrics
- Ensure ID3 tags are present (use metadata editor)
- Try `Sync Lyrics` to add lyrics manually

### Audio not playing
- Check audio device is connected and working
- Verify file format is supported
- Try a different audio file

### Library not loading
- Ensure music directory exists and is readable
- Check `library_cache.json` isn't corrupted
- Try `Update Music Directory` in settings

### Metadata issues
- Use `Inspect Metadata` to view actual tags
- Metadata fallback uses iTunes Library.xml export
- Update tags using external metadata editor if needed

## Future Enhancements

Potential improvements:
- Gapless playback
- Equaliser support
- Playlist creation and management
- Last.fm scrobbling integration
- M3U/PLS playlist export
- Lyrics search and download
- Tag editing GUI

## License

This project is open source and available under the MIT License.

## Contributing

Contributions are welcome! Please ensure:
- Code follows PEP 8 style guidelines
- Functions include docstrings and type hints
- Changes maintain backward compatibility
- New features are well-tested

## Support

For issues or questions:
1. Check the troubleshooting section above
2. Review existing issues/PRs
3. Create a new issue with detailed information

---
