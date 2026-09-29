# Developer Guide

Architecture and development practices for Backtrack. See also **[README.md](../README.md)** for
user-facing docs and **[tag-etiquette.md](tag-etiquette.md)** / **[library-layout.md](library-layout.md)**
/ **[filesystem-etiquette.md](filesystem-etiquette.md)** for tag/library conventions.

## UI style rules

One rule per axis, so similar screens read the same way.

| Axis | Rule |
|---|---|
| Separator | Interpunct `·`: hint bars, headers, player details, multi-value fields. Never `⋅`. |
| Truncation | `…`, one column, never `...`. `truncate_text`'s default. |
| Hierarchy | `>` in the breadcrumb only: it means descent, not separation. |
| Column gap | `prompt.core.COL_GAP` (3) for every list; never a per-list override. Narrow terminals are handled by column `priority` and the per-render pin gap. |
| Case | Sentence case for every label, menu item, prompt title and separator heading. |
| Tick / cross | One pair, heavy: `✔` U+2714 and `✘` U+2718. The tick already marks the current sort, a checked multi-select row and "Save changes", so state uses the same one. Never `✓` U+2713 (lighter, doesn't match) or `✗` U+2717 (drawn brush-style in most fonts). |
| On/off state | Shown in the row itself, in a left-aligned column right beside the labels. Pinned right, a value is too far from its label to scan. |
| Status message | Either a state readout (`Metadata editor ✔`) or a sentence ending in `.`. A colon or comma joins two clauses; never an em or en dash. Failures read "Could not …". No trailing stop when the message ends in interpolated text. |
| Action row | Sentence case, `…` when it opens a further prompt, a scope suffix (after a colon) only when the row acts on something narrower than the header. **Glyphs are not decoration**: only `▸` (play) and `＋` (add) prefix a row, two spaces after; everything else (Copy, Rename, Replace image…) is plain text, like the tag-action screen. |
| Long lists | The viewport never leaves blank space: `viewport = max(0, min(viewport, n - vis))` after following the cursor, so growing the window (or deleting rows) un-scrolls the list instead of stranding it. A list rebuilt each time round passes the same `prompt.ListPlace` to every `select(place=...)`, so the cursor stays on the same item after a re-sort or an edit. |
| Boxes | Rounded (`╭─╮ │ ╰─╯`), dim, indented by `MARGIN_H`, with a blank row after. Header boxes are one line: styled title left, dim facts right, facts shed from the right rather than wrapping. |
| Embedded art | Sized from the rows left after the screen's chrome (`_art_width`), less breathing rows, capped so it stays a thumbnail; centred in a rounded box; **hidden** below `_MIN_ART_ROWS`, where the facts line says more than six rows of mush. Never a fixed `height × 1.5`, which overflows tall windows. |
| Prompt title | Says what confirming *does* (`↵ applies`); the keys belong in the hint bar, not the title. |
| Shift+Tab | Always Tab in reverse, on every screen where Tab does something; advertised as one hint, `[tab/⇧tab]`, both halves clickable. |
| Preview rows | A preview lists every file. A row that wouldn't change is greyed out and can't be ticked (`bulk_common.preview_and_apply` does this). |

### Key vocabulary

One word per concept, so the same key reads the same way on every screen.

| Key | Label | Meaning |
|---|---|---|
| `esc` | **back** | Leaves the screen. Never "cancel": every editor already discards on leave. |
| `↵` | **save** | In an editor: writes the value. |
| `↵` | **confirm** | In a chooser (`select`, `live_select`, list_edit's barrel mode): picks the highlighted item. |
| `tab/⇧tab` | **field** · **column** · **complete** | Names the destination, forwards and back. |
| `tab` | **month/day** | The calendar's two modes, with no reverse to advertise. |
| `q` | **quit app** | It leaves *Backtrack*, not the screen. |
| `d` | **delete** | Never "del". |
| `a` | **add** in an editor · **all** in a multi-select | Different actions, so different words. |


## Architecture

Backtrack is a terminal-first music player and tag editor. The guiding split is **pure logic vs.
UI**: parsing, matching, and writing live in small pure, unit-testable modules; the interactive
screens are thin layers over them. This is what lets most behaviour be tested headlessly.

### Project layout

The terminal layer every back* tool shares (colours, prompt widgets, the painter, dates, logging,
CLI output) lives in [backbone](https://github.com/RedEraRrow/backbone), not here. Change it there.

```
backtrack/
├── pyproject.toml                # Dependencies and the `backtrack` command
├── backtrack/
│   ├── __main__.py               # `python3 -m backtrack`
│   ├── main.py                   # `backtrack` itself. Startup: no args → the app (new session or join), any arg → the CLI
│   ├── cli.py                    # The CLI: argparse, schema and completion built from the command tree
│   ├── cli_commands.py           # The command tree (TREE) and its handlers, thin wrappers over the app's own functions
│   ├── config.py                 # DEFAULT_CONFIG, CONFIG_DIR, load_config, setting, update_config
│   ├── feed.py                   # PURE (bar two network calls): RSS → parsed episodes; dedupe; download
│   ├── history.py                # Listening-history log
│   ├── music_library.py          # Library scan, ID3/MP4 extraction, background sync + reconcile, cache
│   ├── search.py                 # PURE fuzzy matcher/ranker (tiered exact→prefix→word→substring→typo)
│   ├── bulk_pattern.py           # PURE range/every-N/date-schedule assignment + track renumbering
│   ├── tuning.py                 # Every timing, threshold and weight the app is tuned on
│   ├── album_art.py              # Half-block art rendering (OpenCV → ANSI); APIC extraction
│   ├── menus/
│   │   ├── __init__.py           # Main menu
│   │   ├── common.py             # Headers, list columns, settings-row glyphs, Browse categories
│   │   ├── browse.py             # Browse: category menu, each category's list, Libraries
│   │   ├── sorting.py            # Name orders, sort-chain presets, the `s` picker, level editor
│   │   ├── play.py               # Play/queue actions a list offers (`n`/`a`), handing tracks to the player
│   │   ├── search.py             # Search screen and grouped results
│   │   ├── history.py            # Listening history screen
│   │   ├── activity.py           # Activity (notification) centre
│   │   └── settings.py           # Settings screen and the editors it opens
│   ├── id3/
│   │   ├── tag_registry.py       # Single source of truth: TagInfo per frame (drives widget dispatch)
│   │   ├── tag_handler.py        # create_frame, prompt_for_value dispatch, summarize, load_id3/save_id3
│   │   ├── browser.py            # Single-track editor UI + the sort-order engine (pure heuristics)
│   │   ├── bulk_menu.py          # Bulk menu (tag ops / Automation…) + renumber, reflow, strip ops
│   │   ├── bulk_common.py        # Shared by the bulk ops: the step walker (_walk), preview_and_apply
│   │   ├── bulk_names.py         # Derive from filename, Rename files from tags
│   │   ├── bulk_art.py           # Set album art from files, Set picture type
│   │   ├── bulk_sort.py          # Apply sort orders: the split and people review screens
│   │   ├── bulk_assign.py        # Assign by range/schedule, bulk people and fraction editors
│   │   ├── bulk_ops.py           # The plan/apply core the bulk menu and the CLI both drive
│   │   ├── filename_parser.py    # PURE: file/folder names → tag fields (Derive from filename)
│   │   ├── file_namer.py         # PURE: tags → %token% file names (Rename files from tags)
│   │   ├── cover_matcher.py      # PURE: pair tracks ↔ cover-image files (Set album art from files)
│   │   └── tag_writer.py         # Format-agnostic writer: MP3 (ID3) + MP4 atoms; write_fields/write_cover
│   ├── lyrics/
│   │   ├── text.py               # PURE: normalising, stage-dir stripping, script↔transcript alignment
│   │   ├── md_overlay.py         # PURE: the one MD→segment overlay both editor and player render from
│   │   ├── formats.py            # SYLT/USLT/markdown parsing, save_sylt_entries, LRC import
│   │   ├── lyric_pane.py         # The player's lyric pane: what is on screen at a moment of a track
│   │   ├── editor.py             # The lyric editor session (edit, tap-sync, review, save)
│   │   ├── editor_keys.py        # The editor's keys per mode, mouse, review walkthroughs
│   │   ├── editor_view.py        # The editor's screen: pure rendering
│   │   ├── time_fields.py        # Segmented mm:ss.mmm fields, shared with the trim editor
│   │   ├── sync_doc.py           # Finding/loading a track's timed lyrics, the working copy (.sync.json)
│   │   └── verify.py             # Script vs transcript: word streams, split candidates, match-up report
│   ├── playback/
│   │   ├── session.py            # The shared PlaybackSession: one VLC player, queue, background tick
│   │   ├── ipc.py                # Multi-window sessions: registry under CONFIG_DIR/sessions/, client
│   │   ├── player.py             # The player view over the session: key handling, host and joined
│   │   ├── player_ui.py          # Player renderer (frame buffer, layout modes, panes, volume bar)
│   │   ├── queue_pane.py         # The player's queue pane
│   │   ├── now_playing_box.py    # The mini-player above the status bar on every menu screen
│   │   ├── player_art.py         # The player's art: half-blocks, or the real image on iTerm2
│   │   └── player_geom.py        # Where the last frame put things (art, bars, panes) for clicks and redraws
│   └── trim/
│       ├── engine.py             # Lossless MP3 trim engine: ffmpeg stream copy, tag copy, backups
│       ├── editor.py             # Single-track trim screen (`t` in the tag editor)
│       └── bulk.py               # Bulk trim (detection, sting seeding, the conveyor), ReplayGain
├── tests/                        # test_*.py, each runnable on its own
├── tools/
│   ├── align_script.py           # Time a markdown script against its audio → transcript JSON
│   └── check_alignment.py        # Sanity-check a transcript align_script.py produced
└── docs/                         # This guide + user docs
```

### Data locations (outside the repo)

`CONFIG_DIR` is `$XDG_CONFIG_HOME/backtrack` (default `~/.config/backtrack`), and the cache dir is
`$XDG_CACHE_HOME/backtrack` (default `~/.cache/backtrack`). On Windows both are
`%APPDATA%\Backtrack`.

| Data | Path | Override |
|---|---|---|
| Config | `CONFIG_DIR/config.json` | `$BACKTRACK_CONFIG_DIR` |
| Library cache | `<cache dir>/library_cache.json` | `$BACKTRACK_CACHE_DIR` |
| Keyboard layout | detected from the OS (typo scoring) | `$BACKTRACK_KEYBOARD` (`qwerty`/`qwertz`/`azerty`/`dvorak`/`colemak`) |
| History | `CONFIG_DIR/history.log` (`timestamp \| duration \| path`) | (follows `CONFIG_DIR`) |
| Feeds | `CONFIG_DIR/feeds.json` (url, filter, seen keys) | (follows `CONFIG_DIR`) |
| Diagnostics log | `CONFIG_DIR/backtrack.log` (rotated at 1 MB, two kept) | (follows `CONFIG_DIR`) |
| Key hints shown | `CONFIG_DIR/hints_on` (present = on) | (follows `CONFIG_DIR`) |
| Trim backups | `CONFIG_DIR/trim-backups/` | `trim_backup_dir` in config |
| Sessions | `CONFIG_DIR/sessions/` (one registry entry and socket per running session) | (follows `CONFIG_DIR`) |

The env overrides make **isolated live testing** possible: point them at a temp dir to run the real
app against a throwaway config/cache without touching your own.

## Design principles

- **Pure core, thin UI.** `filename_parser`, `file_namer`, `cover_matcher`, `bulk_pattern`,
  `search` and `tag_writer` are pure and unit-tested, and `bulk_ops` plans and applies without any UI; the `bulk_*` screens,
  `id3.browser` and `menus/` own the prompts, previews, and apply loops. New logic should be added
  to (or as) a pure module and driven from the UI, not baked into a prompt.
- **One source of truth for tags.** `tag_registry.TagInfo` describes every frame (friendly names,
  `frame_type`, `format_spec`, `ui_category`, `single_only`, mutagen class). `ui_category` /
  `format_spec` drive which editor widget a frame gets; `single_only` gates multi-value.
- **Cross-format writing.** Anything that writes tags in bulk goes through `tag_writer`, which
  handles MP3 (ID3) and the MP4 atom family and reports what it couldn't do (e.g. an MP4 cover that
  isn't JPEG/PNG) rather than silently dropping it.
- **Shared helpers.** New code uses these rather than its own version:

  | Need | Use |
  |---|---|
  | Read a setting | `config.setting(cfg, key)`. Defaults live only in `DEFAULT_CONFIG`; never `cfg.get(key, default)` |
  | Save settings | `config.update_config(changes)` with just what changed, never the whole dict back |
  | Note what happened | `log.debug(...)` etc., after `from backbone.log import log` |
  | Carry on past an error on purpose | `with quietly():` (`from backbone.log import quietly`); it logs the error when Diagnostics is on |
  | Show a time | `timefmt.clock(t)` (mm:ss.mmm), `timefmt.srt(t)` |
  | A track's name on screen | `music_library.track_title(path, song)` |
  | A text frame's first value | `music_library.first_text(frame)` |
  | Act on paths that may have moved | `music_library.drop_moved(paths)`: re-syncs and tells the user |
  | Read / write ID3 | `id3.tag_handler.load_id3(path)` / `save_id3(audio, path)` |
  | Keep a rebuilt list's cursor | `prompt.ListPlace`, passed as `select(place=...)` |
  | A line-editing key in a text field | `prompt.core.edit_line(buf, pos, key)` |
  | Preview a bulk plan and apply it | `bulk_common.preview_and_apply` |
  | "3 tracks" / "1 track" | `backbone.ui.plural(n, "track")` |

- **Test headlessly, then live.** Pure logic + writes are checked with `pyright backtrack` (kept at
  **0/0**) and small headless scripts (create→save→read round-trips). Interactive widgets get a
  final live-terminal pass, since focus/mouse/layout can't be exercised headlessly.
- **Docstrings.** Every module-level function and class method carries a concise docstring;
  trivial nested redraw/clamp closures are left undocumented, since a docstring there is noise.
  Match the surrounding voice; say *what/why*, don't restate the signature.
- **Type hints.** Python 3.10+ union syntax (`X | Y`, `X | None`); 3.10 is the minimum.
- **Terminal-first UX.** Keyboard-driven, lists never wrap, symmetric margins, works at narrow widths.

## Key systems

### Library & metadata: `music_library.py`

Scans every configured music directory (`config.music_dirs()`; roots may nest and are de-duplicated),
extracts metadata from ID3 (`_extract_id3_metadata`) and MP4
(`_extract_mp4_metadata`), and caches it. A background sync thread runs on an interval and
**reconciles against the filesystem** (a cheap `os.walk` diff), picking up external adds/removes and
renames/moves, with an unmount guard so a temporarily-missing drive doesn't wipe the cache. In-app
edits call `refresh_library_entry` to update the cache immediately.

A screen about to act on files passes them through `drop_moved` first. Any that have gone were
moved or renamed since the scan: the library re-syncs on the spot and one message says so (or,
when a whole music directory has gone, asks the user to update Settings → Music directories).

### Search: `search.py`

A fuzzy matcher/ranker: tiered exact → prefix → word-boundary → substring → subsequence →
bounded-Levenshtein typo, scored by field weight × match geometry with recency/play-count boosts,
returning match spans. `prompt.live_select` re-runs it per keystroke and highlights matched
characters. The searched fields are title, artist, album, composer, lyricist, genre, people and a
computed `disc_label`; "disc 2" / "cd 2" is an exact disc lookup.

The typo tier is keyboard-aware: at equal edit distance, a slip onto a neighbouring key
("radiohesd") outranks the same distance reached with an unrelated letter ("radiohepd").
`backbone.keyboard` detects the layout family at startup (macOS via the HIToolbox plist, Linux via
`setxkbmap`/`localectl`/`/etc/default/keyboard`, Windows via `GetKeyboardLayout`) and hands the key
rows to `search.use_layout()`, keeping `search.py` itself free of I/O. Only letter positions matter,
so British/US/Canadian/ABC are all one QWERTY; the families that differ are QWERTZ, AZERTY, Dvorak
and Colemak. Anything unrecognised stays QWERTY, and `$BACKTRACK_KEYBOARD` overrides detection:
the only thing that can be right over SSH, where the keyboard is on the *other* machine.

### Tag editing: `id3/`

- **`tag_registry.py`**: `TAG_REGISTRY`, the frame catalogue, and `SORT_TAGS` (the sort frames and
  their sources).
- **`id3/tag_handler.py`**: `create_frame(tag_id, value)` builds the right mutagen frame;
  `prompt_for_value` dispatches a frame to its editor widget (by `base_id` for structured binary
  frames like EQU2/RVA2/POPM/PCNT/RBUF and for enum/bool text frames TKEY/TMED/TSRC/TCMP, else by
  `ui_category`/`format_spec`); `summarize_tag_value` renders a one-line summary. `load_id3` reads
  a tag (or a fresh one for an untagged MP3) and `save_id3` picks **v2.4 iff any frame is
  multi-value, else v2.3**, and refuses anything that isn't an MP3. `is_placeholder_name` is what
  keeps "Various Artists" and friends out of name fields. Multi-value (#60) support and the POPM
  0-5★ ↔ 0-255 (WMP-scale) mapping live here.
- **`id3/browser.py`**: the single-track editor UI (MP3 only; its Lyrics row opens the lyric
  editor, `t` the trim editor), and the **sort-order engine** (`_sort_single_name` /
  `_sort_candidates`): pure heuristics (initials/Celtic merges, honorific & suffix strip, spacing
  prefixes, article move, positional split, ensembles as-is, commas read from context) that offer
  ranked candidates. No name corpus: anything it gets wrong the user fixes by picking a candidate or
  "type custom".
- **`bulk_sort.py`**: bulk **Apply sort orders** runs in two passes over the same engine:
  `_verify_splits` settles how each value divides into names (`split_options` offers the engine's
  reading, the value whole, and the maximal split), then `_review_sort_people` lists the resulting
  individuals flat, one row per person across `TSOP`/`TSO2`/`TSOC`, so each is decided once and the
  decision reaches every value they appear in. `_SortPlan` holds both the values and the decisions.
- **`id3/bulk_menu.py`**: the bulk editor's menu, the tag operations (add/set/rename/delete),
  **Automation…**, and the renumber, reflow, remove-single-disc and strip-length ops. The other
  automations live in `bulk_names`, `bulk_art`, `bulk_sort`, `bulk_assign` and `trim/trim_bulk`.
  Each builds a plan with `bulk_ops` and applies it via `tag_writer`; the tidy-ups show it through
  `bulk_common.preview_and_apply`.
- **Walkable screens (`bulk_common._walk`).** The multi-screen automations (derive, rename files,
  album art, assign by pattern, renumber, apply sort orders) hand `_walk` a list of steps, each a
  callable returning True to advance, False to go back one screen, or `_SKIP` when the answers so
  far make it irrelevant (the template question after choosing regex detection). Back leaves the
  operation only from the first screen; anywhere else it returns to the screen before, which still
  holds what was decided there. Every answer lives in a `state` dict so a reopened screen is
  re-seeded: typed patterns, `list_edit` rows, hand-picked covers, and the sort-order flow's
  per-person decisions (carried across a rescan, being keyed by person and value rather than by
  row). A step that only does work (sort orders' file scan) returns `_SKIP` once its output matches
  the answers, so it is transparent in both directions. Validation failures re-ask on the spot
  rather than reporting a back, which on the first screen would end the operation over a typo.

### Dates and times: `backbone.datetime_parse`, `backbone.timefmt`

Every hand-typed date in the app goes through `parse_datetime`. It takes year-first dates with any
of `-` `/` `.` (or spaces) between the parts, zero-padding optional, the compact `20080702` form,
and an optional time after a `T` or a space; it reports the **precision** it was given (`year` …
`second`) so a caller that needs a real day can insist rather than silently scheduling from an
invented 1 January, and returns a short human reason on failure instead of a bare `None`.

`02/07/2008` is ambiguous and is refused unless the caller passes `dayfirst`; a part over 12
settles the order on its own. `prompt.dates._parse_date` (calendar + date/time widgets) and
`bulk_pattern.parse_start` / `norm_time` are thin wrappers over it. Add new date reading here
rather than growing a fourth set of rules.

Showing a time goes the other way, through `timefmt.clock` (mm:ss.mmm) or `timefmt.srt`. Both
round to whole milliseconds first, so 59.9996 s reads `01:00.000`, never `00:60.000`.

### Format-agnostic writing: `id3/tag_writer.py`

`write_fields(path, values, apply_fields, overwrite)` and `write_cover(path, data, mime, …)` write
to MP3 (ID3, fresh header for a blank file) or MP4 atoms (`.m4a`/`.mp4`/`.m4p`), returning a
`WriteResult` (`written`/`skipped_existing`/`skipped_format`/`error`/`unsupported`). Raw `.aac` has
no atoms and is skipped. `has_cover`/`present_fields` back the fill-blanks previews.

### Prompt widgets: `backbone.prompt` (over `backbone.prompt.core`)

`prompt.core` provides the raw-terminal primitives (mode switching, key decode, the anchored
`_Widget` renderer, the structured column/table layout, the adaptive hint engine, and `edit_line`,
the one line editor every text field uses). The `prompt` package builds the widgets and re-exports
them, so callers write `prompt.select(...)`:

- `select` (`lists.py`): the one list widget (single-select; `multi=True` for checkboxes, where `a`
  toggles all; `columns=` for structured rows; `on_inspect`/`inspect_key` for a `d`-style detail
  view; `place=` a `ListPlace` to keep the cursor on the same item across rebuilds).
  `row_edit`/`row_edit_commit` add an **in-place cell edit**: the key (default `e`) cycles the row
  through the values the callback offers, one press per option, and one step past the last is a text
  field seeded from where the cycle left off. While it is live the edit owns every key (`q` and the
  cycle key included, since a name may contain either), so ↑↓/↵/Esc are the only ways out. The cell
  is handed back as styled segments, never raw ANSI: the table measures a cell by its text length
  and would count escape codes as visible.
- `live_select` (incremental search), `confirm` (`lists.py`); `text`, `path` (tab-completion),
  `system_editor_edit` (`text.py`); `list_edit` (`list_edit.py`).
- `list_edit` cell types: a column can be plain text (default), a **barrel** field (`col_hints`
  supplies candidate values to cycle through), or a **timestamp** field (`col_types={i: 'timestamp'}`):
  a split `YYYY-MM-DD HH:MM:SS` mask where you type only the digits and the left/right arrows run
  past the end of one part into the next, with Tab still moving between *columns*. Widths come from
  `col_ratios` with `col_mins` honoured first, so a fixed-shape cell stays readable as the terminal
  narrows and its neighbours give way instead.
- Value editors: `calendar_select`, `datetime_edit` (+ `prompt.timezone.timezone_select`) in `dates.py`;
  `time_edit`, `fraction_edit`, `number_edit` (bounded int spinner), `rating_edit` (POPM stars +
  count + email) in `values.py`; `rva2_edit` (dB meter), `equaliser_edit` (graphic EQ) in `audio.py`.
- **One caret, drawn not borrowed:** every typable field marks its position with
  `prompt.core.block_cursor()`: reverse video *on* the character (a white block at the end of the
  text, where there is nothing left to move). A bar drawn between two characters costs a column, so
  the line slides sideways on every keystroke; `block_cursor_width()` gives callers the padding
  arithmetic. `text`/`path` draw it too rather than positioning the terminal's own cursor, which is
  a thin bar or invisible depending on the terminal.
- **Raw↔widget toggle:** value editors return the `MODE_TOGGLE` sentinel on **Ctrl-T** so
  `prompt_for_value` can flip between the smart widget and a plain text field (#62).
- **Key convention:** `q` **quits the application**: it raises `QuitToTerminal` (a `BaseException`,
  so the editors' broad `except Exception` handlers can't swallow it) on the spot. It is never a way
  to leave a widget and carry on. Backing out is **Esc** everywhere, plus **←/b** in the list
  widgets. There is deliberately no vim-style `hjkl` navigation: those are ordinary letters the
  user may want to type. In widgets whose input is free text (the live search, `text`, `path`, the
  timezone search) `q` is a literal character. **Ctrl-C** quits from the live search; in `text`,
  `path` and the timezone search it leaves the field like Esc.
- **Shared screen chrome (`chrome.py`):** every screen owes the user the same four things: a hint
  bar pinned above the miniplayer + status bar so its keys never move, those keys clickable, the
  background transport keys listed whenever the miniplayer is up, and clicks on the miniplayer box
  itself doing something. That contract lives in one place:
  `chrome_hint_pairs` / `chrome_hint_lines` (build the pairs, adding `^P`/`^N`/`^B`/`^O` only when
  a handler is actually installed), `append_chrome` (pin to `_hint_pin_target`, render, register
  the click cells), and `consume_chrome` (call it right after `_read_key`; it returns
  `CHROME_HANDLED`, `CHROME_REDRAW`, a replacement key when a hint was clicked, or `None`).
  `enable_mouse`/`disable_mouse` turn click reporting on for widgets that want it.
  **A new widget wires these two call sites and gets all four behaviours.**
  A widget that sizes content to the terminal must budget against
  `_hint_pin_target() - len(chrome_hint_lines(pairs))`, not the raw terminal height, or it draws
  over the miniplayer.


### Command line: `cli.py`, `cli_commands.py`, `backbone.output`

`backtrack/main.py:main` sends any invocation with arguments to `cli.main`; bare `backtrack` opens
the app.

**The command tree is data.** `TREE` (in `cli_commands.py`) is a list of `Cmd`s, each with
`Flag`s, `Arg`s and children. Three things read it: `build_parser` makes the argparse parser,
`schema_tree` makes `backtrack schema`, and `completion` makes the bash/zsh/fish scripts. A
flag therefore cannot exist in the parser and be missing from the schema or the completions,
which is the usual way a hand-written completion script rots.

**Handlers are thin.** A handler resolves its arguments, calls the function the menus call, and
hands the result to `output`. If a handler starts deciding *what an operation does*, that
decision belongs in a shared module: `id3/bulk_ops.py` is the worked example.

**One output path.** `backbone.output` has `table` / `record` / `event` / `note` / `fail`, and
the format decision lives only there. Human tables go through `prompt.core._table_widths` and
`_render_table_row`, the same engine every list in the app uses. Colour is switched process-wide
by `backbone.ui.set_colour`, so existing render paths lose colour on a pipe without knowing about
it. A list command prints its table on a terminal and one path per line when it is not, which
is what makes `backtrack track list | backtrack tag read` compose with no flag.

**Two argparse traps**, worth knowing before adding a flag:

- A flag declared on both the top-level parser and the subparsers (so it can be written either
  side of the verb) gets its value *overwritten* by the subparser's default. Global flags and
  any flag with a `config_key` therefore declare `default=argparse.SUPPRESS` and are filled in
  afterwards by `_apply_global_defaults` / `_resolve_defaults`.
- `--help` raises `SystemExit` from inside `parse_args`; it must be caught or it escapes as a
  traceback.

**Defaults precedence** is `flag > config > built-in`, implemented once in `_resolve_defaults`.
Give a `Flag` a `config_key` and it takes part. A falsy config value means "no preference".

**Nothing blocks on stdin.** `Ctx.confirm` returns its default when stdin is not a terminal, and
`--yes` accepts everything. `Ctx.targets(allow_stdin=False)` exists because reading stdin is a
blocking call: a command with another source of targets (`--album`, say) must consult that
*before* stdin, or it hangs on any stdin that stays open.

### Adding a command

1. Write the logic where both callers can reach it: a pure module, or `bulk_ops`-style
   plan/apply. Never in the handler.
2. Add a handler in `cli_commands.py` returning an exit code from `output`.
3. Add a `Cmd` to `TREE` with a **worked example**: a test asserts every leaf has one.
4. Support `--dry-run` if it writes, and emit through `output.event` so `--json` streams.
5. Test it in both output modes and assert its exit codes.

### Playback: `playback/`

`session.py` owns the audio: one process-wide `PlaybackSession` with the VLC player, the current
track and the queue, and a background tick that advances the queue and logs history, whatever
screen is showing. `playback.py` is the player *view* over it: it handles keys
(seek/volume/panes/help) for the host and for a joined window, and leaving the view (`b`) keeps the
audio playing. `player_ui.py` renders the screen into a **frame buffer** flushed in one write:
flow lines are positioned by a row counter while absolute-positioned items (volume bar, controls,
lyrics) pass through. It has three layout modes (wide / standard / minimal) that size the art to
leave room for the metadata and the (variable-height) hint block, a full-height volume bar clamped
to the art bottom, and the side panel that `w` cycles (`cycle_right_pane`: off → lyrics → queue →
lyrics+credits, skipping views with nothing in them). `lyric_pane.py` draws the lyrics,
`queue_pane.py` the queue, `player_art.py` the art, and `player_geom.py` records where the last
frame put everything so clicks and partial redraws can find it.

**Several windows.** `ipc.py` registers each running session under `CONFIG_DIR/sessions/`. A second
`backtrack` lists them at startup (`backtrack/main.py`) and offers a new session or Join; a joined window
drives the host through a `SessionClient`. Only one window holds the player view at a time
(`acquire_view`), and `b` is refused while another window is attached (`has_other_windows`).

### Dialogue scripts & lyrics: `lyrics/`

A spoken-word track has a timed transcript (word-level JSON) and a markdown **script**
next to the audio. The transcript owns the timing; the script owns the speakers,
punctuation, emphasis and stage directions. `lyrics/text.py` is the pure layer: what
counts as the same word, what counts as spoken, and `align_tokens`, which matches the two
word streams and reconciles the conventions they differ on (`take-off`/`takeoff`,
`twenty-five`/`25`, `'cause`/`because`). `md_overlay.build_md_overlay` is the **single**
join of script to segments: the editor and the player both render from it, so they cannot
disagree about who is speaking or where a direction sits.

The lyric editor is `lyrics/editor.py` (the session and its state), `editor_keys.py` (what each
key does in each mode), `editor_view.py` (drawing) and `time_fields.py` (the mm:ss.mmm fields,
also used by the trim editor). `sync_doc.py` loads the lyrics and keeps edits in the working copy
(`.sync.json`) beside the transcript until they are written back; `verify.py` builds the
script-vs-transcript report behind `V`, `backtrack lyrics verify` and `tools/check_alignment.py`.

The script format, and the rules a script has to follow to be matchable, are
**[script-etiquette.md](script-etiquette.md)**.

### Album art: `art/album_art.py`, `playback/player_art.py`

Rendered in-project: `render_native_half_block` downsamples an image with OpenCV/NumPy into ANSI
half-block (`▀`) cells. `player_art.py` sizes it for the player and, on iTerm2 with Settings → Image
album art on, draws the real image over those cells. (Some helper names like `_convert_apic_to_viu`
are legacy from the old `viu` dependency; the binary is gone.)

## Adding features

- **A bulk operation:** write the pure transform in a new/existing pure module (mirror
  `filename_parser`/`cover_matcher`, or add a planner to `bulk_ops`), unit-test it headlessly, then
  add a `*_op(paths, library, header)` in the `bulk_*` module it belongs with (preview with
  `bulk_common.preview_and_apply` or `_walk`, write via `tag_writer`) and register it in
  `bulk_id3_manager`'s `Automation…` list, `op_map`, and dispatch.
- **A tag widget:** add the widget to the fitting `backbone.prompt` module and export it from
  `prompt/__init__.py` (model an existing one; support `MODE_TOGGLE` if it has a plain-text form),
  route it in `prompt_for_value` (by `base_id` for binary frames, else by
  `format_spec`/`ui_category`), and add matching `create_frame` + `summarize_tag_value` branches.
- **A menu option:** add a handler in the fitting `menus/` module, insert it in the choice list,
  return cleanly.
- **A config value:** add it to `config.DEFAULT_CONFIG`, read it with `config.setting`, save it with
  `config.update_config`, and expose it under Settings.

## Testing

```bash
python3 -m unittest discover -s tests -t tests   # the suite
pyright backtrack                                # type check
python3 -m compileall -q backtrack               # syntax/import sanity
BACKTRACK_CONFIG_DIR=/tmp/bt BACKTRACK_CACHE_DIR=/tmp/bt python3 -m backtrack   # isolated live run
```

**Isolating a test that writes.** `config.CONFIG_FILE`, `music_library.CACHE_PATH` and
`history.HISTORY_FILE` are all bound at import, and `history.HISTORY_FILE` captures
`config.CONFIG_DIR` at *its* import, so patching `CONFIG_DIR` alone does not reach it. Patch
each one, and assert they all point inside the temp directory before running anything
destructive. `tests/test_cli.py:_assert_isolated` does this, so a `history clear` test can't
delete a real log.

Prefer a small headless script for pure logic and tag writes (build fixtures with `mutagen`,
round-trip create→save→read). Reserve live runs for the interactive widgets and playback rendering.

## Debugging

- **The diagnostics log.** Turn on Settings → Diagnostics log and Backtrack writes
  `CONFIG_DIR/backtrack.log`. Log through it (`from backbone.log import log`; `log.debug(...)`)
  at points where a failure would be hard to work out afterwards, rather than adding ad hoc prints
  or files. Where an error is swallowed on purpose, use `with quietly():` so it still leaves a line
  in the log. Diagnostics also turns on the player's `e` (jump to the last 35 s) and shows which
  files a track's lyrics came from.
- **Config:** `python3 -c "from backtrack.config import load_config; print(load_config())"`, or
  `backtrack config list`.
- **Library cache:** inspect `library_cache.json` in the cache dir (or `$BACKTRACK_CACHE_DIR`).
- **Playback:** the frame buffer makes it easy to dump the assembled screen before it's written.

## Contributing

- Keep the pure-core / thin-UI split; unit-test pure logic.
- Use the shared helpers (see Design principles) rather than a local copy.
- Document functions concisely (see Design principles); keep `pyright backtrack` at 0/0.
- Preserve terminal UX (no wrapping, symmetric margins, consistent nav keys).
- Update this file and README.md when behaviour or structure changes.
