# Backtrack

Backtrack is a terminal music player for macOS/Linux that plays audio with VLC, shows embedded lyrics, and lets you browse your music library in the terminal.

## Quick Start

```bash
python3 -m pip install -r requirements.txt
python3 main.py
```

On first run, Backtrack asks for your music directory and builds a library cache for faster startup.

## What Backtrack Does

- Browse music by artist, album, or genre
- Search your library by title, artist, album, genre, or file path
- Play audio using VLC with playback controls
- View synced lyrics from `SYLT`/`USLT` tags
- Sync un-timed lyrics interactively
- View and edit track metadata
- Render album art in the terminal via `viu`
- Track listening history

## Installation

### Requirements

- Python 3.8 or newer
- VLC / libvlc installed on your system
- `viu` CLI installed for album art rendering

### Install Python dependencies

```bash
python3 -m pip install -r requirements.txt
```

### Launch Backtrack

```bash
python3 main.py
```

## Usage

### Main Menu

After startup, the main menu lets you:
- Browse Library
- Search
- Listening History
- Settings
- Exit

### Browse Library

Use Browse Library to explore your collection by:
- Artist
- Album
- Genre

### Search

Search your library across:
- Title
- Artist
- Album
- Genre
- File path

Pick a result to play the track, sync lyrics, or edit metadata.

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
| `↑` / `↓` | Move lyric selection when viewing unsynced lyrics |

### Player View Modes

Use Settings to choose the player view:
- `default` — metadata plus album art
- `ipod` — classic iPod-style now playing view

### Listening History

The Listening History menu shows recent tracks you played and lets you replay them.

### Sync Lyrics

If a track contains unsynchronized lyrics (`USLT`), use Sync Lyrics to mark each line while the track plays. The recorded timestamps are saved back to the track.

### Metadata Editing

Search results may offer metadata editing options for title, artist, album art, lyrics, and other tags.

## Configuration

Backtrack stores settings in `config/config.json`.

Common keys:
- `music_directory` — path to your music folder
- `theme` — terminal colour values
- `history_enabled` — track listening history
- `search_weights` — search relevance weights
- `lyric_lead_in` — lyric sync timing offset
- `ascii_width` — album art width
- `player_view` — `default` or `ipod`
- `show_metadata_editor` — toggle metadata editor availability

## Supported Formats

- Audio: `MP3`, `M4A`, `MP4`, `M4P`, `AAC`
- Lyrics: `USLT`, `SYLT`
- Metadata: ID3 tags and MP4 tags
- Optional library metadata: `data/Library.xml`
- Album art: embedded MP3 APIC frames and external images via `viu`

## Troubleshooting

### Playback fails

- Ensure VLC / libvlc is installed
- Verify the file is a supported audio format
- Confirm the terminal can access the music directory

### Album art does not render

- Install the `viu` CLI
- Try using a different file with embedded art

### Lyrics do not appear

- Not all files have embedded lyrics
- Use Sync Lyrics to generate timestamps manually

## Notes

- `data/library_cache.json` is generated automatically
- Listening history is logged to `data/history.log`
- `config/config.json` is created on first run

## License

Backtrack is provided under the MIT License.
