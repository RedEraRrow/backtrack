# Backtrack

A terminal music player and tag editor for macOS and Linux. Backtrack plays your library with
VLC, renders album art as Unicode half-blocks right in the terminal, shows synced and unsynced
lyrics, and includes a full ID3/MP4 tag editor with a suite of bulk-automation tools.

> **Status:** in active development. Core playback, browsing, search, lyrics, and the tag editor
> (including bulk operations) are working; expect rough edges and changing internals.

---

## Features

**Library & browsing**
- Browse by **artist**, **album**, or **genre**, with an A–Z letter index for large collections.
- Rich track rows (featured-artist marker, cached durations) and clean disc/work separators.
- A background cache keeps the library fresh — it re-scans on an interval and reconciles against the
  filesystem, picking up external adds, deletes, renames, and moves.

**Search**
- **Fuzzy live search** that re-ranks on every keystroke, with the matched characters highlighted in
  each field. Cycle the scope (all / title / artist / album / genre / people) with `Tab`.

**Playback**
- VLC/libvlc-backed audio with full transport controls and a live progress bar.
- **In-terminal album art** rendered as Unicode half-blocks (no external viewer).
- A full-height **volume bar** beside the art, and toggleable panes for **lyrics**, an up-next
  **queue**, and cast/crew **credits**.
- **Equaliser** — 24 presets applied during playback via libvlc; stored per file as an `EQU2` tag.

**Lyrics**
- Display **synced (`SYLT`)** and **unsynced (`USLT`)** lyrics; interactive sync tool to time
  un-timed lyrics; import from `.lrc`; append writing credits.

**Tag editing (MP3)**
- Edit every ID3 frame through a widget suited to its type: date/time with a **world-map timezone
  picker**, track/disc **fractions**, **people/credit lists**, a **star-rating** editor (`POPM`),
  a **graphic equaliser** (`EQU2`) and **dB gain** meter (`RVA2`), **numeric spinners**, and
  enum/bool pickers (musical key, media type, a validated ISRC field, a compilation toggle).
- **Multi-value** frames (artists, composers, genres…), automatic **sort-order** generation, and a
  power-user **plain-text** mode.

**Bulk automation** (MP3 **and** MP4, with a preview before anything is written)
- **Derive from filename** — fill tags from file/folder names.
- **Rename files from tags** — the inverse, collision-safe.
- **Set album art from files** — embed per-track or per-disc/series covers found beside the tracks.
- **Apply sort orders**, **Renumber tracks** (disc ↔ continuous), **Assign by range/schedule**,
  **Copy from first track**, plus set/rename/delete tags across a selection.

**History & settings**
- Listening history with relative timestamps, and a sectioned settings screen.

---

## Requirements

- **Python 3.8+** (developed against 3.13/3.14).
- **VLC / libvlc** installed on your system (provides audio playback).
- Python packages from `requirements.txt`: `mutagen`, `python-vlc`, `opencv-python`, `numpy`,
  `pyperclip`, `colorama`. Album art is rendered **in-project** (OpenCV + NumPy) — no external image
  viewer is needed.

## Installation

**1. Install VLC / libvlc**

macOS (Homebrew):
```bash
brew install vlc
```

Ubuntu / Debian:
```bash
sudo apt update && sudo apt install vlc
```

**2. Install Python dependencies**
```bash
python3 -m pip install -r requirements.txt
```

**3. (Optional) install as a package**
```bash
python3 -m pip install .        # or: pip install -e .  for an editable dev install
```

## Running

```bash
python3 main.py
```
or, if installed as a package:
```bash
backtrack
```

On first run, Backtrack asks for your music directory and builds a cached library for faster
subsequent startups.

---

## Usage

### Main menu

Browse Library · Search · Listening History · Settings · Exit.

Navigation is consistent everywhere: `↑↓`/`jk` move, `→`/`l`/`Enter` confirm, `←`/`b`/`h`/`Esc` go
back, `q` quits to the terminal (saving any in-progress edit first). Lists never wrap, restore the
cursor when you back out, and support mouse clicks and `Tab`-based select-all where relevant.

### Browse

Explore by **Artist**, **Album**, or **Genre**. Drilling into a letter in the A–Z index and backing
out returns you to the index. Selecting a track offers Play / Edit tags (or plays immediately if
*Auto-play on select* is enabled).

### Search

Fuzzy search across title, artist, album, genre, people, and file path. Type to filter; results
re-rank live with the matched characters highlighted. `Tab` cycles the search scope; `Enter` opens
a result; `Esc` backs out.

### Playback controls

| Key | Action |
|-----|--------|
| `space` | Play / pause |
| `←` / `→` | Seek ∓5 s |
| `j` / `l` | Seek ∓1 s |
| `,` / `.` | Seek ∓30 s |
| `+` / `-` | Volume up / down |
| `m` | Toggle extended metadata (year · genre · work/movement …) |
| `w` | Cycle the side panel (lyrics → queue → lyrics+credits) |
| `↑` / `↓` | Scroll the active pane / lyric selection |
| `i` | Toggle the full help/hint line |
| `n` | Next track |
| `b` | Back (stop and return to where you came from) |
| `q` | Quit the application |

### Listening history

Recent tracks in aligned columns (title · artist · album · when · listened), with relative times
(`just now`, `40m ago`, `2w ago`). Replay any entry.

### Lyrics

Tracks with `SYLT`/`USLT` show lyrics during playback. Use the interactive **Sync Lyrics** tool to
time un-timed lyrics as the track plays, **import** from an `.lrc` file, or **append** Music-by /
Words-by credits.

### Metadata editing

From a track, choose **Edit tags** to open the single-track ID3 editor; from **Browse**, choose
*Edit tags* on an album (or press `e`) for the **bulk** editor. Bulk operations live under a two-level
menu — **TAGS** (add / set / rename / delete) and **Automation…** (derive, rename files, set album
art, assign by range/schedule, apply sort orders, renumber tracks, copy from first track). Every
operation previews its changes and, by default, only fills blank tags.

See the guides below to get the most out of tagging and auto-detection.

---

## Documentation

- **[Tag etiquette](docs/tag-etiquette.md)** — good ID3 practice, and how Backtrack reads each tag.
- **[Filesystem etiquette](docs/filesystem-etiquette.md)** — how to organise a library on disk.
- **[Library layout & naming](docs/library-layout.md)** — the exact folder/name patterns the
  *Derive from filename* parser recognises, including template and regex overrides.
- **[Developer notes](docs/DEVELOPER.md)** — internals and architecture.

---

## Configuration

Settings are managed in-app under **Settings** (playback, library, editors, history) and stored in a
JSON config created on first run. Notable keys include `music_directory`, `history_enabled`,
`search_weights`, `lyric_lead_in`, and the editor options (sort-list delimiter, plain-text editing,
auto-play on select, tag-name preferences). Prefer the Settings screen over hand-editing the file.

## Supported formats

- **Audio:** MP3, M4A, MP4, M4P, AAC.
- **Tags:** ID3v2 (MP3) and MP4 atoms. Single-track tag *editing* is MP3-only; the bulk operations
  write both MP3 and MP4.
- **Lyrics:** `USLT` (unsynced) and `SYLT` (synced).
- **Album art:** embedded MP3 `APIC` and MP4 `covr` (JPEG/PNG), rendered in-terminal as half-blocks.

## Troubleshooting

**Playback fails** — ensure VLC / libvlc is installed and the file is a supported format, and that
the terminal can read your music directory.

**Album art doesn't render** — confirm the file actually has embedded art; very narrow terminals
shrink or omit the art. (No external viewer is required — art is rendered in-project.)

**Lyrics don't appear** — not all files have embedded lyrics; use **Sync Lyrics** to add timings, or
import an `.lrc`.

**Tag editing says "MP3 only"** — the single-track editor edits ID3/MP3; use the bulk Automation
tools for MP4 tag changes.

## License

MIT.
