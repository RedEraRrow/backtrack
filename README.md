# Backtrack

A terminal music player and tag editor for macOS and Linux. Backtrack plays your library with
VLC, draws album art as Unicode half-blocks in the terminal, shows synced and unsynced lyrics,
and has a tag editor (ID3, MP4 and Vorbis comments) with bulk operations for whole albums.

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
- The **volume**, shown for a moment over the middle of the screen as it changes, and panels for the **lyrics**, the up-next
  **queue**, and the **people** credited.
- **Equaliser**: 24 presets applied during playback via libvlc, stored per file as an `EQU2` tag.
- Several terminal windows can share one session (see [Several windows](#several-windows)).

**Lyrics**
- Shows **synced (`SYLT`)** and **unsynced (`USLT`)** lyrics, and markdown dialogue scripts for
  spoken-word tracks. A lyric editor times un-timed lyrics as the track plays.

**Tag editing (MP3, WAV, AIFF; other formats as plain key/value tags)**
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
# Homebrew, if you don't have it yet (it asks for your password)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# put brew on your PATH, now and in new windows (Apple Silicon; does nothing on an Intel Mac)
echo 'eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null)"

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

### Tabs

Now playing · Browse · Directory · Search · Settings, along the top, on F1 to F5 (or a
click); `Tab` / `Shift-Tab` move between them too, except where you're typing. The tab showing is
outlined like a folder's tab, opening into the screen under it; that screen's title moves to the
right end of its border (the breadcrumb at the bottom says it too, when there's no room). The keys
show beside the names with the hints (`?`); in a narrow window the other tabs show just their keys. Each tab stays where
you left it. The lyrics and trim editors keep you until you leave them. Backing out of a tab's
first screen goes to the tab you came from. The app opens on Browse, offering first to resume what
the last run left. With nothing playing, Now playing shows the empty player; `r` there resumes the
last run's queue. In the player, `f` hides or shows the tab bar (also Settings → Tab bar in player).
The player sits in boxes while the tab bar or a panel shows, at any window size, and edge to edge
only with neither (`f` hides the bar). The
boxes fill the window with no gaps between them: the art and the details in a box as wide as the art
with the panels (`w`) in columns beside it wherever they fit without leaving the art much smaller
(the player's box gives up width for them; a column on each side can differ in width, as can the
panels stacked in one), else under it, as in a tall, narrow window; and along the
bottom the controls, the progress bar, the times and the speed or sleep timer. In a short, wide
window the art sits beside the details instead of over them, so it stays as big as it can be.
In a tiny window it's just the transport: boxed in 3 to 5 rows, bare in 1 or 2 (and the tab bar and
status line step aside under 6 rows). As it narrows the row gives things up in turn, never cutting
any short: the bar, then the length (the position alone), then the time, then previous, then next,
leaving play/pause, and last the box. Screens never show without their boxes: one in a window
too small for them (under 10 rows, or 9 columns) gives way to that same transport, alone, until the
window grows and it comes back as you left it. Meanwhile the transport's own keys work (play/pause,
next, previous, seeking, volume) and `:` opens the command line; nothing else reaches the screen.
The art is a square
that fills its space (a cover that isn't square is cropped about its centre), in the player,
Browse's preview and the tag editor's picture screens alike; an audiobook's booklet picture too.
The people panel
lists everyone, in two columns when one won't hold them, taking the rows it needs above the
lyrics. Settings → Exit quits, as `q` does.

Every list highlights its current row with a soft bar; a row that opens something has a `›` at its
right. Long titles on the highlighted row scroll, a column a beat, 100 beats a minute unless
Settings → Scrolling text speed says otherwise.

Navigation is the same everywhere: `↑↓` move, `→`/`Enter` confirm, `←`/`b`/`Esc` go back, and
`q` quits the app from anywhere (it never just closes a widget). In a field you type into, `q` is
typed as a letter; in the live search, Ctrl-C quits instead. Lists never wrap, keep the cursor on
the same item after a re-sort or an edit, restore it when you back out, and support mouse clicks;
`a` selects all in a multi-select list. A list in sections (Settings, Key bindings) that is much
taller than the window opens as its section titles: `Enter` opens one, `Esc` goes back to the
titles, and `/` switches to the whole list and back. The hint bar (pinned to the bottom of the screen) is
clickable too: click any highlighted key to trigger it. When audio is playing, the mini-player's
⏯/⏭ icons are clickable, and clicking anywhere else on it reopens the player. In the full player,
the ⏮/⏯/⏭ controls, the progress bar and the hint bar are all clickable.

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

In a wide enough window Browse is a column browser: one box holds the levels above as columns
(categories, then the artist, then the album) with the list you're in last, and a click on a row
of an earlier column goes back to that level, at that row. Beside it, the highlighted row's cover,
credits, length, format and copyright in one box over what opening it lists in another. The columns' widths
are worked out once for each list from everything in it, so they hold still as you move through
it and change when you go to another level.
The cover is a real image in iTerm2 with Settings → Image album art on, else drawn in text. An
artist (composer, person) shows their own picture: `artist`, `folder` or `poster` (.jpg, .png…)
in the folder their albums are in, else a placeholder. Genres show no picture. A narrower window
drops the oldest columns, then puts the details in a strip under the list, then shows the list
alone. `v` (or Settings → Browse in columns) turns the columns off and on. A long list opens as its
A–Z index (Settings → Long lists open by letter; `/` switches either way).

### Command line

`:` in any list or the player opens a box over the middle of the screen to type a command in, as
after `backtrack` in a shell (`track list --artist
Eagles`, `tag read`, `config set …`, `play --album …`, `--help`), on this window's library and
session: `play` and the queue and session commands act on what this window is playing. A
one-line answer shows in the status bar; more opens in a list to read.

### Directory

The music directories as folders on disk, and nothing more: folders first (each ending in `/`; one
with no audio anywhere beneath it isn't listed),
then their audio files, in name order, the folders above as columns to the left. No pictures or
details: it's the filesystem.
A folder plays, shuffles, queues and edits like an album (everything under it, in path order); a
file plays like a track, with the rest of its folder after it as Settings → After a picked track says.
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
| `w` | Choose the panels: a box over the player to tick Lyrics, People, Queue and Chapters (one this track has nothing for says so). Its **Arrange panels…** moves them: `Tab` from panel to panel (the one moving is outlined in the accent), `↑` / `↓` up or down its column, `←` / `→` to the other side of the player, `↵` done. A wide window and a tall one each keep their own arrangement: wide, the panels are columns beside the player (either side, or both); tall, they're under it, side by side when there's room. Remembered |
| `↑` / `↓` | With the queue panel showing: a cursor through the queue (the mouse wheel or a trackpad over the panel moves it too). `↵` plays the track at it, `J` / `K` move it up / down, `d` removes it, `x` shuffles what's coming, `c` clears what's coming, `u` undoes the last queue change |
| `?` | Show or hide the key hints, on every screen: they start hidden. Each screen's top line ends in `[?] help`; in a text field, where `?` is typed, it says `[^/] help` and the key is Ctrl+/ (macOS's Terminal sends nothing for that, so there it says `[^g] help` and the key is Ctrl+G; either works anywhere it arrives). Clicking the key in the corner works too. In the player it stays in the hint bar. Settings → [?] help toggle hides `[?] help` everywhere; `?` still works |
| `[` / `]` | Previous / next track |
| `e` | Edit the playing track's tags |
| `a` / `A` | Go to the playing track's album / artist in Browse (`Esc` from there goes up through Browse) |
| `E` | Jump to the last 35 s (only with Settings → Diagnostics log on) |
| `b` / `Esc` | Minimise: leave the player but keep the audio playing in the background (pinned while another window is attached) |
| `s` | Stop playback |
| `q` | Quit the application |

From any menu while audio is playing: **Ctrl-O** reopens the player (the Now playing tab), and **Ctrl-P** / **Ctrl-N** /
**Ctrl-B** control play-pause / next / previous.

### Several windows

Start a second `backtrack` while one is playing and it offers **Start a new session** (this window
plays its own audio) or **Join** the running one. A joined window browses and queues as normal and
controls the host's audio; its player shows what the host is playing. Only one window has the
player open at a time, and while another window is attached, `b` won't leave the player.

### Listening history

In Settings → History. Recent tracks in aligned columns (title · artist · album · when ·
listened), with relative times (`just now`, `40m ago`, `2w ago`). Replay any entry.

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

Adding or renaming a tag (or assigning one in bulk), its id is suggested as you type: frames by id,
then a description (`TXXX:Mood`, the file's own first) and an ISO 639-2
language (`COMM:Notes:eng`, or `COMM[eng]`); `↑` / `↓` pick one, `Tab` takes it. A key that can't
lead to a good id (a frame backtrack can't build from a typed value, a description or language on
a frame that has none, a language code that doesn't exist) does nothing, and a TXXX with no
description isn't taken till it has one. Vorbis and MP4 keys are checked and suggested the same way.

Text of several lines (comments, unsynced lyrics, a list typed as text) is edited in a box in the
app, its lines numbered down the left: `Enter` starts a new line, the arrows and a click move
through it, long lines wrap at word boundaries. `Ctrl-F` opens a find box at its foot: matches show
as you type (ignoring case unless you type a capital), `Enter` / `↓` goes to the next and `↑` the
one before. `Ctrl-R` adds a replacement: `Tab` moves between the two, `Enter` replaces the match the
caret is on and moves on, `Ctrl-A` replaces every match. In either, `Ctrl-T` switches between plain
text and a pattern (a regular expression; `\1` or `\g<name>` in the replacement puts back a
group). `Ctrl-S` saves; `Esc` closes the find box, or leaves, asking first if you changed anything;
`Ctrl-E` carries on in the system editor (`$EDITOR`) for anyone who prefers it.

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

- **Audio:** MP3, WAV, AIFF, M4A/M4B/MP4/M4P, FLAC, Ogg Vorbis (`.ogg`/`.oga`), Opus and AAC.
  Raw `.aac` plays but can't be tagged.
- **Tags:** ID3v2 (MP3, and the ID3 chunk in WAV/AIFF), MP4 atoms and Vorbis comments
  (FLAC/Ogg/Opus). The single-track editor edits ID3 frame by frame; MP4 and Vorbis files open in
  it as key/value tags. In bulk, derive, rename files, album art, renumber, reflow, remove
  single-disc numbering and ReplayGain write every format; the frame operations, assign and sort
  orders are ID3 only. Trimming is MP3 only.
- **Lyrics:** `USLT` (unsynced) and `SYLT` (synced); MP4 `©lyr` and Vorbis `LYRICS` are shown too,
  timed when they hold LRC text.
- **Album art:** embedded ID3 `APIC`, MP4 `covr` (JPEG/PNG) and FLAC/Ogg pictures, drawn in the
  terminal as images where it shows them (iTerm2 outside tmux, with Settings → Image album art on), else as
  half-blocks.

## Troubleshooting

**Playback fails**: check that VLC / libvlc is installed, the file is a supported format, and the
terminal can read your music directory.

**Album art doesn't render**: check the file actually has embedded art; very narrow terminals
shrink or omit the art.

**Lyrics don't appear**: not all files have embedded lyrics. Import an `.lrc` from the tag editor,
or time existing lyrics in the lyric editor.

**A tag operation skips some files**: the frame operations, assign and sort orders work on ID3
(MP3, WAV, AIFF); edit other formats in the single-track editor or with the cross-format Automation
tools.

**Something else went wrong**: turn on Settings → Diagnostics log, repeat what you did, and look
in `~/.config/backtrack/backtrack.log`.

## License

MIT.
