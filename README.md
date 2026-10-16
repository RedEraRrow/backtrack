# Backtrack

A music player and tag editor that runs in your terminal, on macOS and Linux. It plays your
library through VLC, shows album art and lyrics as it plays, and edits tags one track at a time
or a whole album at once.

> **Status:** in active development. Playback, browsing, search, lyrics and the tag editor all
> work; expect rough edges.

## What it does

- **Browse and search** your library by artist, album, genre, composer and more, or by folder.
  Search is fuzzy and updates as you type.
- **Play** with album art (as a real image in iTerm2), a queue that's kept between runs, an
  equaliser, a sleep timer and speed control for audiobooks.
- **Panels** beside, above or below the player, arranged however you like: lyrics, the queue,
  chapters, the people credited, a visualiser, release and file info, notes, and an album's
  booklet to page through.
- **Lyrics**, synced or not, from the tags or a file beside the track (`.lrc`, subtitles, Whisper
  JSON). Spoken-word recordings can show a full script with speakers and stage directions, in time
  with the audio, and a lyric editor times lyrics as the track plays.
- **Edit tags** with an editor suited to each kind of tag (dates, ratings, credits, pictures,
  lyrics), and **in bulk**: fill tags from file names, rename files from tags, renumber discs, set
  album art, and more, always previewed before anything is written.
- **Trim** silence or idents off the start and end of MP3s without re-encoding, and set
  ReplayGain (both need ffmpeg).
- A full **command line** for everything the app does, for scripts and shell pipes.

## Install

macOS:
```bash
# Homebrew, if you don't have it yet
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
echo 'eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)"

brew install pipx
brew install --cask vlc
pipx ensurepath             # then open a new terminal window
pipx install backpack-backtrack
```

Ubuntu / Debian:
```bash
sudo apt install vlc pipx
pipx ensurepath             # then open a new terminal window
pipx install backpack-backtrack
```

Then run `backtrack`. The first time, it asks where your music is.

ffmpeg is optional: it adds trimming, ReplayGain and the visualiser (`brew install ffmpeg` or
`sudo apt install ffmpeg`). `backtrack doctor` checks what's installed and says how to add
anything missing. Upgrade with `pipx upgrade backpack-backtrack`.

## Getting around

The tabs along the top are **Now playing**, **Browse**, **Directory**, **Search** and
**Settings**: `F1` to `F5`, `Tab`, or a click.

| Key | Does |
|---|---|
| `↑` `↓` | Move |
| `→` / `Enter` | Open or play |
| `←` / `b` / `Esc` | Back |
| `p` / `x` | Play / shuffle the list |
| `o` | Everything you can do with the highlighted row |
| `e` | Edit tags |
| `:` | Type a command |
| `?` | Show or hide the keys for this screen |
| `q` | Quit |

The mouse works too: clicks, the scroll wheel, and the keys in the hint bar. Every key can be
changed in Settings → Key bindings.

## The player

| Key | Does |
|---|---|
| `space` | Play / pause |
| `←` `→` | Back / forward 5 seconds (`,` `.` for 30) |
| `[` `]` | Previous / next track or chapter |
| `+` `-` | Volume |
| `w` | Choose and arrange the panels |
| `b` | Leave the player, keep playing |
| `s` | Stop |

`w` ticks which panels show. Its **Arrange panels…** lets you move each one to a column beside
the player, above or below the art, or a full-width strip across the top or bottom. A wide and
a tall window each remember their own arrangement.

Start a second `backtrack` while one is playing and you can join it, to browse and control the
same music from another window.

## Lyrics and scripts

Backtrack shows the lyrics in a track's tags, or a lyrics file with the same name beside it.
Settings → Lyrics from chooses which comes first.

For radio comedy, audiobooks and other spoken word, put a markdown script and a timed transcript
beside the track, or pack them into one `.bkts` file with `backtrack lyrics pack`. The player then
shows who's speaking and the stage directions, in time with the audio. Writing a script that
lines up well is covered in [script etiquette](docs/script-etiquette.md).

## Editing tags

Press `e` on a track for the single-track editor, or on an album, artist or any list for the bulk
editor. The bulk editor's **Automation…** menu has the bigger jobs: tags from file names, file
names from tags, album art from image files, renumbering and more. Every change is previewed, and
by default only fills tags that are blank.

MP3, WAV and AIFF are edited frame by frame. M4A, FLAC, Ogg and Opus are edited as plain
tags, and most bulk tools work on them too.

## Command line

With arguments, `backtrack` is a normal command-line tool:

```bash
backtrack track list --artist "Duran Duran"
backtrack tag read track.mp3 --tag TIT2
backtrack play --album Rio
backtrack feed sync --name comedy
```

`backtrack --help` lists everything, and `--json` gives output for scripts.

## Documentation

- **[Tag etiquette](docs/tag-etiquette.md)**: good tagging practice, and how Backtrack reads each tag.
- **[Filesystem etiquette](docs/filesystem-etiquette.md)**: organising a library on disk.
- **[Library layout](docs/library-layout.md)**: the folder and file names *Derive from filename* understands.
- **[Script etiquette](docs/script-etiquette.md)**: writing a dialogue script for spoken-word tracks.
- **[Reference](docs/reference.md)**: every screen, key, setting and command in detail.

## If something goes wrong

Turn on Settings → Diagnostics log, do the thing again, and look in
`~/.config/backtrack/backtrack.log`. If nothing plays, check that VLC is installed.

## License

MIT.
