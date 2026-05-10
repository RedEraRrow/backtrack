# Developer Guide

This document explains the architecture and development practices for the Terminal Music Player.

## Architecture

## Architecture

### Project Structure

```
Music Browser/
├── main.py                 # Entry point launcher (7 lines)
├── src/                    # Source code directory
│   ├── main.py             # Main application logic (58 lines)
│   ├── config.py           # Configuration management (48 lines)
│   ├── state.py            # Shared global state (8 lines)
│   ├── music_library.py    # Library operations (289 lines)
│   ├── playback.py         # Music player engine (494 lines)
│   ├── menus.py            # Navigation menus (414 lines)
│   ├── history.py          # History tracking (86 lines)
│   ├── terminal_input.py   # Terminal I/O (99 lines)
│   ├── ascii_art.py        # Image processing (95 lines)
│   ├── ui_utils.py         # UI utilities (196 lines)
│   ├── lyric_timer.py      # Lyric sync tool (91 lines)
│   ├── metadata_browser.py # Metadata UI (323 lines)
│   └── prompt.py           # User input utilities (89 lines)
├── config/                 # Configuration directory
│   └── config.json         # User configuration (generated)
├── data/                   # Data directory
│   ├── library_cache.json  # Cached library (generated)
│   └── Library.xml         # iTunes export (optional)
├── docs/                   # Documentation directory
│   └── DEVELOPER.md        # This file
├── requirements.txt        # Python dependencies
├── README.md               # User guide
└── history.log             # Listening history (generated)
```

## Module Dependencies

```
main.py (launcher)
  └─ src.main

src/main.py
  ├─ src.config
  ├─ src.music_library
  │   ├─ src.history
  │   └─ (mutagen, xml)
  └─ src.menus
      ├─ src.music_library
      ├─ src.history
      ├─ src.playback
      │   ├─ src.ui_utils
      │   ├─ src.terminal_input
      │   ├─ src.ascii_art
      │   ├─ src.history
      │   ├─ src.state
      │   └─ (miniaudio, mutagen)
      ├─ src.lyric_timer
      ├─ src.config
      ├─ src.state
      └─ src.metadata_browser
          ├─ src.ui_utils
          ├─ src.ascii_art
          └─ (mutagen, cv2, numpy)
```

## Design Principles

### 1. Single Responsibility
Each module has one clear purpose:
- `src/config.py` - Configuration only
- `src/music_library.py` - Library operations only
- `src/playback.py` - Audio playback only
- `src/menus.py` - Menu navigation only

### 2. Type Hints
All functions include Python 3.8+ type hints:

```python
def search_library(library: list, query: str, active_tags: list | None = None) -> list:
    """Search library implementation..."""
    pass
```

### 3. Docstrings
Functions include comprehensive docstrings:

```python
def musicplayer(file_path: str, preloaded_data: dict | None = None) -> dict:
    """
    Main music player engine.
    
    Handles playback, lyric display, and user controls.
    """
    pass
```

### 4. Error Handling
Graceful error handling with informative messages:

```python
try:
    audio = ID3(file_path)
except Exception as e:
    print(f"Error loading metadata: {e}")
    return {"status": "ERROR"}
```

### 5. Constants
Constants are defined at module level in UPPERCASE:

```python
VALID_AUDIO_EXTENSIONS = ('.mp3', '.m4a', '.mp4', '.m4p', '.aac')
CACHE_PATH = os.path.join(os.path.dirname(__file__), "../data/library_cache.json")
```

## Adding New Features

### Example: Add Album Art Display

1. **Identify the module** - Image handling goes in `src/ascii_art.py`

2. **Write the function**:
```python
def display_album_art_large(file_path: str, width: int = 200) -> None:
    """Display large album art for current track."""
    art = get_ascii(file_path, width)
    print(art)
```

3. **Update playback UI** - Call from `src/playback.py`:
```python
# In draw_full_ui()
display_album_art_large(file_path, width=max_art_h)
```

4. **Test** - Import and run:
```bash
python3 -c "from src.ascii_art import display_album_art_large; display_album_art_large('song.mp3')"
```

### Example: Add New Menu Option

1. **Create the handler** in `src/menus.py`:
```python
def handle_now_playing(library: list) -> None:
    """Show now playing information."""
    # Implementation
    pass
```

2. **Add to main menu** in `main_menu()`:
```python
choices=["Browse Library", "Search", "Now Playing", "Settings", "Exit"]
elif choice == "Now Playing":
    handle_now_playing(library_ref[0])
```

3. **Test the menu** - Run the app and check the new option

## Code Style

### Naming Conventions

- **Modules**: lowercase with underscores (`ascii_art.py`)
- **Classes**: PascalCase (`Colours`)
- **Functions**: snake_case (`format_time()`)
- **Constants**: UPPERCASE (`CACHE_PATH`)
- **Private functions**: Leading underscore (`_parse_sylt()`)
- **Private modules**: Leading underscore (`_helpers.py`)

### Formatting

- **Line length**: 100 characters max (for readability in terminals)
- **Spacing**: 2 blank lines between module-level functions
- **Imports**: Group by type (stdlib, third-party, local)

```python
import os
import json

import questionary

from library import build_library
from config import load_config
```

### Comments

Use comments sparingly - code should be self-documenting:

```python
# Good: explains WHY
if metadata["artist"] == "Unknown Artist":
    # XML fallback only if we couldn't extract from ID3 tags
    metadata.update(xml_info)

# Avoid: restates the code
# Set i to 0
i = 0
```

## Testing

### Manual Testing

Test individual functions:
```bash
python3 -c "
from src.music_library import search_library, load_library_cache
lib = load_library_cache()
results = search_library(lib, 'Beatles')
print(f'Found {len(results)} matches')
"
```

Test module imports:
```bash
python3 -m py_compile src/playback.py src/menus.py
```

### Integration Testing

Run the full application:
```bash
python3 main.py
```

Test specific features:
- Browse a category
- Search for a song
- Play a track
- Sync lyrics
- Check history

## Debugging

### Enable Verbose Output

Add debug prints to trace execution:
```python
def musicplayer(file_path: str, preloaded_data=None):
    print(f"DEBUG: Loading {file_path}")
    # ... rest of function
```

### Check Configuration

Inspect current configuration:
```python
from src.config import load_config
config = load_config()
print(config)
```

### Inspect Library State

Check cached library:
```python
from src.music_library import load_library_cache
lib = load_library_cache()
print(f"Library has {len(lib)} tracks")
for song in lib[:5]:
    print(f"  {song['artist']} - {song['title']}")
```

## Performance Considerations

### Library Caching

- Library is cached in `data/library_cache.json` for fast startup
- Cache is invalidated when music directory changes
- Large libraries (10000+ songs) may take time on first scan

### UI Rendering

- Progress UI updates occur every 50ms (line 557 in playback.py)
- Lyric display only updates when index changes (not every frame)
- Album art is cached during playback

### Memory Usage

- Full library is loaded into memory (acceptable for most use cases)
- ID3 tags are read per-file (could be optimised with caching)
- Image conversion happens on-demand (not precomputed)

## Future Improvements

### Short Term
- [ ] Add unit tests for library functions
- [ ] Implement gapless playback
- [ ] Add repeat/shuffle mode persistence
- [ ] Improve metadata editor UI

### Medium Term
- [ ] Add equaliser support
- [ ] Implement Last.fm scrobbling
- [ ] Create playlist management
- [ ] Add tag editing from UI

### Long Term
- [ ] Streaming service integration
- [ ] Advanced search/filtering (regex, date ranges)
- [ ] Plugin system for extensions
- [ ] Web interface for remote control

## Release Checklist

Before releasing a new version:

- [ ] All modules in `src/` compile without syntax errors
- [ ] All imports work correctly with `src.` prefix
- [ ] Manual testing of key features passes
- [ ] Configuration files are created in correct `config/` directory
- [ ] Data files are stored in `data/` directory
- [ ] README updated with new features
- [ ] No debug print statements left in code
- [ ] No broken commented-out code
- [ ] Performance tested with large library
- [ ] Version number updated
- [ ] CHANGELOG.md updated

## Support & Resources

### Code Navigation

- **Music playback** → `playback.py`
- **Menu navigation** → `menus.py`
- **Library operations** → `library.py`
- **Configuration** → `config.py`
- **UI elements** → `ui_utils.py` and `ascii_art.py`
- **Terminal I/O** → `terminal_input.py`

### Common Issues & FAQs

**Q: How do I add a keyboard shortcut?**
A: Add handling in `terminal_input.py` and process in `playback.py` key handler.

**Q: How do I add new metadata field?**
A: Update `TAG_MAP` in `playback.py` and `library.py`.

**Q: How do I change the UI colours?**
A: Modify `Colours` class in `ui_utils.py` or `config.json` theme settings.

**Q: How do I add a new browse category?**
A: Add to `key_map` dictionary in `browse_menu()` in `menus.py`.

## Contributing

To contribute to this project:

1. Review the existing code and comments first
2. Follow the style guidelines above
3. Add tests for new features
4. Update documentation
5. Submit a pull request

For questions, please open an issue on the project repository.

---
