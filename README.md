# Backtrack

A terminal music player and tag editor for macOS and Linux. Backtrack plays your library with
VLC, draws album art as Unicode half-blocks in the terminal, shows synced and unsynced lyrics,
and has an ID3/MP4 tag editor with bulk operations for whole albums.

> **Status:** in active development. Core playback, browsing, search, lyrics, and the tag editor
> (including bulk operations) are working; expect rough edges and changing internals.

---

## Features

**Library & browsing**
- Browse by **artist**, **album**, **genre** and more, with an A-Z letter index for large collections.
- Track rows show a featured-artist marker and cached durations, with disc and work separators.
- A background sync keeps the library fresh: it re-scans on an interval and reconciles against the
  filesystem, picking up adds, deletes, renames and moves made outside the app.

**Search**
- Fuzzy live search that re-ranks on every keystroke and highlights the matched characters.

**Playback**
- VLC/libvlc-backed audio with transport controls and a live progress bar.
- Album art drawn in the terminal as half-blocks, or as a real image in iTerm2 (opt-in).
- A full-height **volume bar** beside the art, and a side panel for **lyrics**, the up-next
  **queue**, and cast/crew **credits**.
- **Equaliser**: 24 presets applied during playback via libvlc, stored per file as an `EQU2` tag.
- Several terminal windows can share one session (see [Several windows](#several-windows)).

**Lyrics**
- Shows **synced (`SYLT`)** and **unsynced (`USLT`)** lyrics, and markdown dialogue scripts for
  spoken-word tracks. A lyric editor times un-timed lyrics as the track plays.

**Tag editing (MP3)**
- Edit every ID3 frame through a widget suited to its type: date/time with a **world-map timezone
  picker**, track/disc **fractions**, **people/credit lists**, a **star-rating** editor (`POPM`),
  a **graphic equaliser** (`EQU2`) and **dB gain** meter (`RVA2`), **numeric spinners**, and
  enum/bool pickers (musical key, media type, a validated ISRC field, a compilation toggle).
- **Multi-value** frames (artists, composers, genres…), automatic **sort-order** generation, and a
  **plain-text** mode.

**Bulk operations** (with a preview before anything is written)
- Tag add / set / rename / delete across a selection, plus automations: derive tags from file
  names, rename files from tags, set album art, assign by range or schedule, sort orders,
  renumbering and more (see [Metadata editing](#metadata-editing)).
- Track/disc **number pairs** edit in bulk without collateral damage: whichever half the files
  already share is editable, and the half that differs shows as a greyed `──` and is left alone.

**Trimming** (needs ffmpeg)
- Cut the start or end off an MP3 losslessly, one track at a time (`t` in the tag editor) or
  across an album (bulk *Trim tracks…*). The original is backed up and a trim can be undone.
- Measure loudness and set ReplayGain across a selection.

**History & settings**
- Listening history with relative timestamps, and a sectioned settings screen.

---

## Installation

macOS, starting from nothing:
```bash
# Homebrew, if you don't have it yet (it asks for your password, and on Apple
# Silicon prints two lines to run afterwards: run them)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install pipx
brew install --cask vlc
pipx ensurepath             # then open a new terminal window
pipx install backpack-backtrack
backtrack
```

Ubuntu / Debian:
```bash
sudo apt install vlc pipx
pipx ensurepath             # then open a new terminal window
pipx install backpack-backtrack
backtrack
```

It needs Python 3.10 or later and VLC. macOS's own `python3` is too old (3.9), but pipx from
Homebrew brings a current one, so there's nothing to do about it. `pipx ensurepath` puts the
`backtrack` command on your PATH, which a new terminal window picks up. The first run asks for
your music folder.

`backtrack doctor` lists what it uses and how to install anything missing, including the optional
tools: **ffmpeg** for trimming and ReplayGain (without it those options are hidden), and on Linux
**wl-clipboard** or **xclip** for copying paths and tags. Upgrade later with
`pipx upgrade backpack-backtrack`.

### From a checkout, to work on it

Clone backbone beside backtrack and install both editable, backbone first, so edits to either take
effect with no reinstall:
```bash
python3 -m pip install -e ../backbone -e .
```

## Running

```bash
backtrack
```
or, without the command on your PATH, `python3 -m backtrack` from the checkout.

On first run, Backtrack asks for a music directory and builds a cached library for faster
startups after that.

**With arguments, `backtrack` is an ordinary command-line tool** instead: see
[Command line](#command-line) below. Everything the menus can do is reachable from there.

---

## Usage

### Main menu

Browse · Search · Listening History · Settings · Exit.

Navigation is the same everywhere: `↑↓` move, `→`/`Enter` confirm, `←`/`b`/`Esc` go back, and
`q` quits the app from anywhere (it never just closes a widget). In a field you type into, `q` is
typed as a letter; in the live search, Ctrl-C quits instead. Lists never wrap, keep the cursor on
the same item after a re-sort or an edit, restore it when you back out, and support mouse clicks;
`a` selects all in a multi-select list. A list in sections (Settings, Key bindings) that is much
taller than the window opens as its section titles: `Enter` opens one, `Esc` goes back to the
titles, and `/` switches to the whole list and back. The hint bar (pinned to the bottom of the screen) is
clickable too: click any highlighted key to trigger it. When audio is playing, the mini-player's
⏯/⏭ icons are clickable, and clicking anywhere else on it reopens the player. In the full player,
the ⏮/⏯/⏭ controls, the hint bar, and the vertical volume bar are all clickable (click the volume
bar at the height you want).

If a file has been moved or renamed since the library was scanned, Backtrack notices when you act
on it, re-syncs the library and says so. If a whole music folder has moved, the message asks you to
update it in Settings → Music directories.

### Browse

Explore by **Artist**, **Album**, **Genre** and more (composer, lyricist, people, year, decade,
grouping, work: choose which appear, and their order, in Settings → Browse menu), across
everything or within one music directory (Browse → Libraries, each under the name you give it in
Settings → Music directories). Drilling into a letter in the A-Z index and backing out returns you
to the index. Play all (`p`), shuffle (`x`), album shuffle (`X`) and edit all (`E`) are in the hint
bar at the bottom of each list; `e` edits just the highlighted row. `n` plays the highlighted row
next and `a` adds it to the queue (with nothing playing, either starts it). `o` lists everything
you can do with the highlighted row, each with its key: for a track, play it, edit its tags, play
from here, play next, play after the album that's playing, add it to the queue (shuffled or not),
add its whole album; for an album, artist or other group, the same for all its tracks. `O` lists
the same for the whole list: play, shuffle, edit, sort, and play next or queue everything listed.
Any of these can be given its own key in Settings → Key bindings. Shuffling, clearing and undoing
the queue itself are in the player's queue panel.

Albums and tracks follow one sort order, set with `s` in any list or in Settings → Sorting: a
chain of levels (album, album year, disc, track, title, date…), each ascending or descending, with
presets such as broadcast order. A music directory can have its own. Selecting a track plays it;
what follows is Settings → *After a picked track*: nothing, the rest of the list it was picked from
(the default), or the queue that was already playing. The queue is kept between runs: opening backtrack
again offers to **Resume** it at the track and moment you left, or start fresh.

### Search

Fuzzy search across title, artist, album, composer, lyricist, genre and people, and by disc
("disc 2"). Type to filter; results re-rank live with the matched characters highlighted. `^f`
cycles the scope (all / title / artist / album / composer / lyricist / genre / people) and `Tab`
jumps between result sections; `^e` edits the highlighted track and `^a` every result; `^k` lists
everything you can do with a result (as `o` does in a list); `Enter` opens a result or plays a track (what follows it is the
After a picked track setting); `Esc` backs out.

### Playback controls

These are the default keys. Every key in the app can be changed in Settings → Key bindings (screen
by screen, with more than one key per action if you like), and every hint bar shows the keys you set.

| Key | Action |
|-----|--------|
| `space` / `p` | Play / pause |
| `←` / `→` | Seek ∓5 s |
| `j` / `l` | Seek ∓1 s |
| `,` / `.` | Seek ∓30 s |
| `+` / `-` | Volume up / down |
| `m` | Show or hide the track details line (year · genre · disc/track …); remembered. Also Settings → Track details in player |
| `w` | Cycle the side panel: off → lyrics → queue → lyrics+credits. Views with nothing in them are skipped |
| `↑` / `↓` | With the queue panel showing: a cursor through the queue. `↵` plays the track at it, `J` / `K` move it up / down, `d` removes it, `x` shuffles what's coming, `c` clears what's coming, `u` undoes the last queue change |
| `?` | Show or hide the key hints, on every screen: they start hidden. Each screen's top line ends in `[?] help`; in a text field, where `?` is typed, it says `[^/] help` and the key is Ctrl+/ (which works everywhere). Clicking the key in the corner works too. In the player it stays in the hint bar. Also Settings → Key hints |
| `[` / `]` | Previous / next track |
| `e` | Jump to the last 35 s (only with Settings → Diagnostics log on) |
| `b` / `Esc` | Minimise: leave the player but keep the audio playing in the background (pinned while another window is attached) |
| `s` | Stop playback |
| `q` | Quit the application |

From any menu while audio is playing: **Ctrl-O** reopens the player, and **Ctrl-P** / **Ctrl-N** /
**Ctrl-B** control play-pause / next / previous.

### Several windows

Start a second `backtrack` while one is playing and it offers **Start a new session** (this window
plays its own audio) or **Join** the running one. A joined window browses and queues as normal and
controls the host's audio; its player shows what the host is playing. Only one window has the
player open at a time, and while another window is attached, `b` won't leave the player.

### Listening history

Recent tracks in aligned columns (title · artist · album · when · listened), with relative times
(`just now`, `40m ago`, `2w ago`). Replay any entry.

### Lyrics

Tracks with `SYLT`/`USLT` lyrics, or a transcript and markdown script, show them during playback.
To time or fix them, open the track's tag editor and choose the **Lyrics** row, which appears when
the track has lyrics or a transcript (and Settings → Lyrics editor is on). The lyric editor taps in
timings as the track plays, adds Music by / Words by credits (`c`), and for spoken-word tracks
checks the script against the transcript (`V`). An `.lrc` file can be imported from a `SYLT` or
`USLT` tag's actions. For writing dialogue scripts, see
[script etiquette](docs/script-etiquette.md).

### Metadata editing

From a track, choose **Edit tags** to open the single-track editor; from **Browse**, choose
*Edit tags* on an album (or press `e`) for the **bulk** editor. The bulk editor has the tag
operations (add, set, rename, delete) and an **Automation…** menu:

- **Derive from filename**: fill tags from file and folder names.
- **Rename files from tags**: the inverse, collision-safe.
- **Set album art from files**: embed per-track or per-disc/series covers found beside the tracks.
- **Assign by range / schedule**: including a per-range schedule where each disc/series carries
  its own start date and cadence, entered in a split date/time cell where you type only the digits.
- **Apply sort orders**.
- **Renumber tracks** (disc ↔ continuous).
- **Reflow disc numbering**: renumber discs to a dense 1…N after inserting a `1.5`, deleting a
  disc, or appending one, and fix the totals.
- **Remove single-disc numbering**: drop `1/1` disc numbers.
- **Strip stale length tags**: remove stale `TLEN` and non-zero `TDLY` frames.
- **Trim tracks…** and **Measure loudness / set ReplayGain…** (need ffmpeg).
- **Set picture type**: retype embedded art (for example to front cover) without touching the image.
- **Copy from first track**.

Every operation previews its changes and, by default, only fills blank tags. In the tidy-up
previews (renumber, reflow, remove single-disc numbering, strip length tags, set picture type),
rows that wouldn't change are greyed out.

Disc and track numbering is read from the files themselves rather than the library cache, so
renumbering and reflowing stay correct even right after you have hand-numbered a disc.

The guides under [Documentation](#documentation) cover tagging practice and what *Derive from
filename* recognises.

---

## Command line

`backtrack` with no arguments opens the app. With arguments it is a normal CLI: noun, then
verb.

```bash
backtrack library scan                    # rebuild the cache
backtrack track list --artist "Duran Duran"
backtrack search "hungry wolf" -n 5
backtrack tag read track.mp3 --tag TIT2
backtrack bulk stripdisc --album Rio
backtrack play --album Rio --repeat all
backtrack feed sync --name comedy
```

`backtrack --help` lists the groups; `backtrack <group> <verb> --help` documents one command
and shows a worked example. `backtrack schema` prints the whole tree (commands, flags, output
shapes and exit codes) as JSON. The parser, the schema and the shell completions are all generated
from the same command definitions, so they always agree.

### Command groups

| Group | Verbs |
|---|---|
| `library` | `scan` `list` `stat` `verify` `dirs` |
| `track` | `list` `show` |
| `tag` | `read` `write` `rename` `delete` `copy` |
| `bulk` | `derive` `rename` `art` `pictype` `renumber` `reflow` `stripdisc` `striplength` `sortorders` `assign` |
| `play` | (takes tracks or a filter) |
| `queue` | `show` `add` `next` |
| `session` | `list` `status` `pause` `next` `prev` `stop` `seek` `volume` |
| `lyrics` | `show` `import` `export` `verify` |
| `trim` | `detect` `cut` `list` `restore` |
| `feed` | `add` `list` `remove` `sync` `fetch` |
| `history` | `list` `clear` |
| `config` | `list` `get` `set` |
| `search`, `schema`, `completion` | |

Three things stay in the app, because they are "mark this by ear while it plays" and need a
person: **lyric tap-sync and audition**, the **trim marking screen**, and the **rendered
player view**. Their non-interactive halves all have commands: `lyrics import/export`,
`trim detect`, `trim cut --start --end`, and the `session` transport.

### Global flags

| Flag | What it does |
|---|---|
| `--json` | Structured output; NDJSON, one event per line, for long operations |
| `-y`, `--yes` | Accept every confirmation; never prompt |
| `--dry-run` | Print the plan in the same event shape a real run emits, change nothing |
| `-L`, `--library DIR` | Work in this music directory (repeatable) |
| `-o`, `--output DIR` | Where files this command writes should go |
| `-q`, `--quiet` | No human output; the exit code still reports |
| `--no-colour` | Never colour the output |

They work on either side of the verb: `backtrack --json library list` and
`backtrack library list --json` are the same.

### Output

Human by default: aligned columns, and colour **only** when stdout is a terminal. `NO_COLOR`
is honoured.

When stdout is **not** a terminal, a list command prints one path per line instead of a
table, so commands compose without a flag:

```bash
backtrack track list --artist Darude | backtrack tag read
backtrack search wolf | backtrack bulk stripdisc
```

`--json` overrides both. Every JSON object carries a `schema` version. Long operations emit
one event per line, flushed as it happens, so `backtrack bulk derive --json | jq` reports
each file as it is written rather than everything at the end.

Errors go to stderr; under `--json` they are `{"error": {"code", "message", "context"}}`.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | It worked |
| 1 | It didn't, for a reason with no more specific code |
| 2 | The arguments were wrong |
| 3 | The thing asked for isn't there |
| 4 | The thing asked for is there already |
| 5 | A required external tool (ffmpeg, VLC) is missing |

### Nothing blocks

`--yes` accepts every confirmation. When stdin is not a terminal, a confirmation takes its
default rather than waiting, so an agent with no human attached never hangs on a read that
will never come. `--dry-run` works on every command that writes.

### Shell completion

```bash
backtrack completion zsh  > ~/.zfunc/_backtrack
backtrack completion bash > /usr/local/etc/bash_completion.d/backtrack
backtrack completion fish > ~/.config/fish/completions/backtrack.fish
```

### Defaults in the config

A flag beats the config file; the config file beats the value compiled in. The keys are
`cli_output_dir`, `cli_rename_pattern`, `cli_art_strategy`, `cli_search_limit`,
`cli_history_limit` and `trim_scan_window_s`, plus `music_directories` for `--library`. A
falsy value means "no preference".

```bash
backtrack config set cli_rename_pattern "%artist% - %track% - %title%"
backtrack bulk rename --album Rio        # uses it
```

### Podcast feeds

```bash
backtrack feed add https://example.com/rss --name comedy --filter-title "News Quiz"
backtrack feed sync --name comedy --output ~/Music/Podcasts
```

`sync` downloads what is new, dedupes on GUID (falling back to the enclosure URL), and tags
each episode from its title (show, series, episode, title and date), keeping the raw title
verbatim in a comment. Re-running it downloads nothing and duplicates nothing.

`pubDate` is usually an upload time rather than a broadcast date, so a date found in the
title wins; where the title gives a day and month but no year, the year comes from `pubDate`
and each episode records which happened.

Downloads enter the library the way any other new file does: written into a music
directory and handed to the same refresh the app uses.

---

## Documentation

- **[Tag etiquette](docs/tag-etiquette.md)**: good ID3 practice, and how Backtrack reads each tag.
- **[Filesystem etiquette](docs/filesystem-etiquette.md)**: how to organise a library on disk.
- **[Library layout & naming](docs/library-layout.md)**: the exact folder/name patterns the
  *Derive from filename* parser recognises, including template and regex overrides.
- **[Script etiquette](docs/script-etiquette.md)**: writing a dialogue script a transcript can be
  matched to.
- **[Developer notes](docs/DEVELOPER.md)**: internals and architecture.

---

## Configuration

Settings are managed in-app under **Settings**, in seven sections:

- **Playback**: lyric lead-in, after a picked track, key hints, image album art (iTerm2), track
  details in player.
- **Appearance**: accent colour (colours from your terminal's palette, fixed colours, or a custom
  hex value).
- **Library**: music directories, the activity centre, the hidden file filter, the Browse menu.
- **Sorting**: the sort order, whether to use sort-order tags, and ignored leading words.
- **Editors**: metadata editor, lyrics editor, plain-text editing, tag name preferences, sort
  list delimiter.
- **Diagnostics**: the diagnostics log, which writes `~/.config/backtrack/backtrack.log`.
- **History**: listening history on or off, and clearing the log.

They are stored in a JSON config created on first run. `music_directories` is a list (add or remove
them under Settings → Music directories; the older single `music_directory` key is migrated
automatically and kept in step with the first entry), and `volume` is restored at launch and saved
whenever you change it. Prefer the Settings screen or `backtrack config set` over hand-editing the
file.

## Supported formats

- **Audio:** MP3, M4A, MP4, M4P and AAC. Raw `.aac` plays but can't be tagged.
- **Tags:** ID3v2 (MP3) and MP4 atoms (`.m4a`/`.mp4`/`.m4p`). The single-track editor is MP3 only.
  In bulk, derive, rename files, album art, renumber, reflow and remove single-disc numbering
  write both MP3 and MP4; the tag operations, assign, sort orders and the rest are MP3 only.
- **Lyrics:** `USLT` (unsynced) and `SYLT` (synced).
- **Album art:** embedded MP3 `APIC` and MP4 `covr` (JPEG/PNG), drawn in the terminal as half-blocks.

## Troubleshooting

**Playback fails**: check that VLC / libvlc is installed, the file is a supported format, and the
terminal can read your music directory.

**Album art doesn't render**: check the file actually has embedded art; very narrow terminals
shrink or omit the art.

**Lyrics don't appear**: not all files have embedded lyrics. Import an `.lrc` from the tag editor,
or time existing lyrics in the lyric editor.

**Tag editing says "MP3 only"**: the single-track editor edits ID3/MP3; use the bulk Automation
tools for MP4 tag changes.

**Something else went wrong**: turn on Settings → Diagnostics log, repeat what you did, and look
in `~/.config/backtrack/backtrack.log`.

## License

MIT.
