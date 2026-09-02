# Developer Guide

Architecture and development practices for Backtrack. See also **[README.md](../README.md)** for
user-facing docs and **[tag-etiquette.md](tag-etiquette.md)** / **[library-layout.md](library-layout.md)**
/ **[filesystem-etiquette.md](filesystem-etiquette.md)** for tag/library conventions.

## UI style rules

One rule per axis, so similar screens read the same way.

| Axis | Rule |
|---|---|
| Separator | Interpunct `·` — hint bars, headers, player details, multi-value fields. Never `⋅`. |
| Truncation | `…`, one column, never `...`. `truncate_text`'s default. |
| Hierarchy | `>` in the breadcrumb only — it means descent, not separation. |
| Column gap | `prompt_core.COL_GAP` (3) for every list; never a per-list override. Narrow terminals are handled by column `priority` and the per-render pin gap. |
| Case | Sentence case for every label, menu item, prompt title and separator heading. |
| Tick / cross | One pair, heavy: `✔` U+2714 and `✘` U+2718. The tick already marks the current sort, a checked multi-select row and "Save changes", so state uses the same one. Never `✓` U+2713 (lighter, doesn't match) or `✗` U+2717 (drawn brush-style in most fonts). |
| On/off state | Shown in the row itself, in a left-aligned column right beside the labels — pinned right, a value was too far from its label to scan. |
| Status message | Either a state readout (`Metadata editor ✔`) or a sentence ending in `.`. An em dash introduces a clause; never a hyphen. Failures read "Could not …". No trailing stop when the message ends in interpolated text. |
| Action row | Sentence case, `…` when it opens a further prompt, a `— scope` suffix only when the row acts on something narrower than the header. **Glyphs are not decoration**: only `▸` (play) and `＋` (add) prefix a row, two spaces after; everything else (Copy, Rename, Replace image…) is plain text, like the tag-action screen. |
| Long lists | The viewport never leaves blank space: `viewport = max(0, min(viewport, n - vis))` after following the cursor, so growing the window (or deleting rows) un-scrolls the list instead of stranding it. |
| Boxes | Rounded — `╭─╮ │ ╰─╯` — dim, indented by `MARGIN_H`, with a blank row after. Header boxes are one line: styled title left, dim facts right, facts shed from the right rather than wrapping. |
| Embedded art | Sized from the rows left after the screen's chrome (`_art_width`), less breathing rows, capped so it stays a thumbnail; centred in a rounded box; **hidden** below `_MIN_ART_ROWS`, where the facts line says more than six rows of mush. Never a fixed `height × 1.5`, which overflowed tall windows. |
| Prompt title | Says what confirming *does* (`Preview — ↵ applies:`); the keys belong in the hint bar, not the title. |
| Shift+Tab | Always Tab in reverse, on every screen where Tab does something; advertised as one hint, `[tab/⇧tab]`, both halves clickable. |

### Key vocabulary

One word per concept, so the same key reads the same way on every screen.

| Key | Label | Meaning |
|---|---|---|
| `esc` | **back** | Leaves the screen. Never "cancel" — every editor already discards on leave. |
| `↵` | **save** | In an editor: writes the value. |
| `↵` | **confirm** | In a chooser (`select`, `live_select`, list_edit's barrel mode): picks the highlighted item. |
| `tab/⇧tab` | **field** · **column** · **complete** | Names the destination, forwards and back. |
| `tab` | **month/day** | The calendar's two modes — no reverse to advertise. |
| `q` | **quit app** | It leaves *Backtrack*, not the screen. This surprised people. |
| `d` | **delete** | Never "del". |
| `a` | **add** in an editor · **all** in a multi-select | Genuinely different actions, so different words. |


## Architecture

Backtrack is a terminal-first music player and tag editor. The guiding split is **pure logic vs.
UI**: parsing, matching, and writing live in small pure, unit-testable modules; the interactive
screens are thin layers over them. This is what lets most behaviour be verified headlessly.

### Project layout

```
backtrack/
├── main.py                     # Root launcher → src.main:main (console-script: `backtrack`)
├── src/
│   ├── main.py                 # Startup: load config/library, launch the main menu
│   ├── menus.py                # Main menu, browse, search, history, settings handlers
│   ├── music_library.py        # Library scan, ID3/MP4 extraction, background sync + reconcile, cache
│   ├── search.py               # Fuzzy matcher/ranker (tiered exact→prefix→word→substring→typo)
│   ├── history.py              # Listening-history log
│   ├── config.py               # Config load/save + CONFIG_DIR
│   ├── bulk_pattern.py         # Pure range/every-N/date-schedule assignment + track renumbering
│   ├── state.py                # Shared nav state (NAV_STACK), QuitToTerminal
│   ├── art/
│   │   └── album_art.py        # In-project half-block art rendering (OpenCV → ANSI); APIC extraction
│   ├── id3/
│   │   ├── tag_registry.py     # Single source of truth: TagInfo per frame (drives widget dispatch)
│   │   ├── id3_tag_handler.py  # create_frame, prompt_for_value dispatch, summarize, save_id3, POPM/PCNT
│   │   ├── id3_browser.py      # Single-track editor UI + the smart sort-order engine (pure heuristics)
│   │   ├── bulk_id3_manager.py # Two-level bulk menu (TAGS / Automation…) and its operations
│   │   ├── filename_parser.py  # PURE: file/folder names → tag fields (Derive from filename)
│   │   ├── file_namer.py       # PURE: tags → %token% file names (Rename files from tags)
│   │   │                        #       number styles: %track:r% (roman), %disc:en% (words)
│   │   ├── cover_matcher.py    # PURE: pair tracks ↔ cover-image files (Set album art from files)
│   │   └── tag_writer.py       # Format-agnostic writer: MP3 (ID3) + MP4 atoms; write_fields/write_cover
│   ├── lyrics/
│   │   ├── lyrics.py           # SYLT/USLT parsing + display; save_sylt_entries; markdown/dialogue
│   │   └── lyrics_editor.py    # Unified lyric editor (edit, sync/tap-in, verify)
│   ├── playback/
│   │   ├── playback.py         # VLC engine, key handling, seek/volume, lyric/dialogue sync loop
│   │   └── playback_ui.py      # Playback screen renderer (frame buffer, layout modes, panes, volume bar)
│   └── utils/
│       ├── prompt_core.py      # Low-level terminal primitives: raw mode, key decode, anchored _Widget,
│   │                            #       + the persistent screen model (diffed row painting)
│       │                       #   column/table layout, hint engine
│       ├── prompt.py           # The widget library built on prompt_core (see "Prompt widgets")
│       ├── tz_widget.py        # Full-screen world-map timezone picker (runnable standalone)
│       ├── terminal_input.py   # Non-blocking key reads for the playback loop
│       ├── datetime_parse.py   # the one date/time parser (precision-aware, human errors)
│       ├── keyboard.py         # Detects the keyboard layout family (typo scoring in search.py)
│       └── ui_utils.py         # ANSI helpers, terminal size, margins, breadcrumb status bar
└── docs/                       # This guide + user docs
```

### Data locations (outside the repo)

| Data | Path | Override |
|---|---|---|
| Config | `~/.config/backtrack/config.json` | `$BACKTRACK_CONFIG_DIR` |
| Library cache | `~/.cache/backtrack/library_cache.json` | `$BACKTRACK_CACHE_DIR` |
| Keyboard layout | detected from the OS (typo scoring) | `$BACKTRACK_KEYBOARD` (`qwerty`/`qwertz`/`azerty`/`dvorak`/`colemak`) |
| History | `~/.config/backtrack/history.log` (`timestamp | duration | path`) | (follows `CONFIG_DIR`) |

The env overrides make **isolated live testing** possible — point them at a temp dir to run the real
app against a throwaway config/cache without touching your own.

## Design principles

- **Pure core, thin UI.** `filename_parser`, `file_namer`, `cover_matcher`, `bulk_pattern`,
  `search`, and `tag_writer` are pure and unit-tested; `bulk_id3_manager` / `id3_browser` / `menus`
  own the prompts, previews, and apply loops. New logic should be added to (or as) a pure module and
  driven from the UI, not baked into a prompt.
- **One source of truth for tags.** `tag_registry.TagInfo` describes every frame (friendly names,
  `frame_type`, `format_spec`, `ui_category`, `single_only`, mutagen class). `ui_category` /
  `format_spec` drive which editor widget a frame gets; `single_only` gates multi-value.
- **Cross-format writing.** Anything that writes tags in bulk goes through `tag_writer`, which
  handles MP3 (ID3) and the MP4 atom family and reports what it couldn't do (e.g. an MP4 cover that
  isn't JPEG/PNG) rather than silently dropping it.
- **Verify headlessly, then live.** Pure logic + writes are checked with `pyright src` (kept at
  **0/0**) and small headless scripts (create→save→read round-trips). Interactive widgets get a
  final live-terminal pass, since focus/mouse/layout can't be exercised headlessly.
- **Docstrings.** Every module-level function and class method carries a concise docstring (present
  coverage ~94%); trivial nested redraw/clamp closures are intentionally left undocumented — a
  docstring there is noise. Match the surrounding voice; say *what/why*, don't restate the signature.
- **Type hints.** Python 3.10+ union syntax (`X | Y`, `X | None`).
- **Terminal-first UX.** Keyboard-driven, lists never wrap, symmetric margins, works at narrow widths.

## Key systems

### Library & metadata — `music_library.py`

Scans every configured music directory (`config.music_dirs()`; roots may nest and are de-duplicated),
extracts metadata from ID3 (`_extract_id3_metadata`) and MP4
(`_extract_mp4_metadata`), and caches it. A background sync thread runs on an interval and
**reconciles against the filesystem** (a cheap `os.walk` diff), picking up external adds/removes and
renames/moves, with an unmount guard so a temporarily-missing drive doesn't wipe the cache. In-app
edits call `refresh_library_entry` to update the cache immediately.

### Search — `search.py`

A fuzzy matcher/ranker: tiered exact → prefix → word-boundary → substring → subsequence → bounded-
Levenshtein typo, scored by field weight × match geometry with recency/play-count boosts, returning
match spans. `prompt.live_select` re-runs it per keystroke and highlights matched characters.

The typo tier is keyboard-aware: at equal edit distance, a slip onto a neighbouring key
("radiohesd") outranks the same distance reached with an unrelated letter ("radiohepd").
`utils/keyboard.py` detects the layout family at startup — macOS via the HIToolbox plist, Linux via
`setxkbmap`/`localectl`/`/etc/default/keyboard`, Windows via `GetKeyboardLayout` — and hands the key
rows to `search.use_layout()`, keeping `search.py` itself free of I/O. Only letter positions matter,
so British/US/Canadian/ABC are all one QWERTY; the families that differ are QWERTZ, AZERTY, Dvorak
and Colemak. Anything unrecognised stays QWERTY, and `$BACKTRACK_KEYBOARD` overrides detection —
the only thing that can be right over SSH, where the keyboard is on the *other* machine.

### Tag editing — `id3/`

- **`tag_registry.py`** — `TAG_REGISTRY`: the frame catalogue.
- **`id3_tag_handler.py`** — `create_frame(tag_id, value)` builds the right mutagen frame;
  `prompt_for_value` dispatches a frame to its editor widget (by `base_id` for structured binary
  frames like EQU2/RVA2/POPM/PCNT/RBUF and for enum/bool text frames TKEY/TMED/TSRC/TCMP, else by
  `ui_category`/`format_spec`); `summarize_tag_value`
  renders a one-line summary; `save_id3` picks **v2.4 iff any frame is multi-value, else v2.3**.
  Multi-value (#60) support and the POPM 0–5★ ↔ 0–255 (WMP-scale) mapping live here.
- **`id3_browser.py`** — the single-track editor UI, and the **sort-order engine** (`_sort_single_name`
  / `_sort_candidates`): pure heuristics (initials/Celtic merges, honorific & suffix strip, spacing
  prefixes, article move, positional split, ensembles as-is, commas read from context) that offer
  ranked candidates. No name corpus — anything it gets wrong the user fixes by picking a candidate or
  "type custom". Bulk **Apply sort orders** runs in two passes over the same engine: `_verify_splits`
  confirms how each value divides into names (`split_options` offers the engine's reading, the value
  whole, and the maximal split), then `_review_sort_people` lists the resulting individuals flat —
  one row per person across `TSOP`/`TSO2`/`TSOC`, so each is decided once and the decision reaches
  every value they appear in. `_SortPlan` holds both the values and the decisions.
- **`bulk_id3_manager.py`** — the bulk editor: a two-level menu, **TAGS** (add/set/rename/delete) and
  **Automation…** (derive, rename files, set album art, assign by range/schedule, apply sort orders,
  renumber tracks, copy from first track). Each automation op builds a preview then applies via the
  pure modules + `tag_writer`.
- **Walkable screens (`_walk`).** The six multi-screen automations — derive, rename files, album art,
  assign by pattern, renumber, apply sort orders — hand `_walk` a list of steps, each a callable
  returning True to advance, False to go back one screen, or `_SKIP` when the answers so far make it
  irrelevant (the template question after choosing regex detection). Back leaves the operation only
  from the first screen; anywhere else it returns to the screen before, which still holds what was
  decided there. Every answer lives in a `state` dict so a reopened screen is re-seeded — typed
  patterns, `list_edit` rows, hand-picked covers, and the sort-order flow's per-person decisions
  (carried across a rescan, being keyed by person and value rather than by row). A step that only
  does work — sort orders' file scan — returns `_SKIP` once its output matches the answers, so it is
  transparent in both directions. Validation failures re-ask on the spot rather than reporting a
  back, which on the first screen would end the operation over a typo.

### Dates and times — `utils/datetime_parse.py`

Every hand-typed date in the app goes through `parse_datetime`. It takes year-first dates with any
of `-` `/` `.` (or spaces) between the parts, zero-padding optional, the compact `20080702` form,
and an optional time after a `T` or a space; it reports the **precision** it was given (`year` …
`second`) so a caller that needs a real day can insist rather than silently scheduling from an
invented 1 January, and returns a short human reason on failure instead of a bare `None`.

`02/07/2008` is ambiguous and is refused unless the caller passes `dayfirst`; a part over 12
settles the order on its own. `prompt._parse_date` (calendar + date/time widgets) and
`bulk_pattern.parse_start` / `norm_time` are thin wrappers over it — add new date reading here
rather than growing a fourth set of rules.

### Format-agnostic writing — `id3/tag_writer.py`

`write_fields(path, values, apply_fields, overwrite)` and `write_cover(path, data, mime, …)` write
to MP3 (ID3, fresh header for a blank file) or MP4 atoms, returning a `WriteResult`
(`written`/`skipped_existing`/`skipped_format`/`error`/`unsupported`). `has_cover`/`present_fields`
back the fill-blanks previews.

### Prompt widgets — `utils/prompt.py` (over `prompt_core.py`)

`prompt_core` provides the raw-terminal primitives (mode switching, key decode, the anchored
`_Widget` renderer, the structured column/table layout, and the adaptive hint engine). `prompt`
builds the widgets:

- `select` — the one list widget (single-select; `multi=True` is the old checkbox; `columns=` for
  structured rows; `on_inspect`/`inspect_key` for a `d`-style detail view; `a` toggles all in multi).
  `row_edit`/`row_edit_commit` add an **in-place cell edit**: the key (default `e`) cycles the row
  through the values the callback offers, one press per option, and one step past the last is a text
  field seeded from where the cycle left off. While it is live the edit owns every key — `q` and the
  cycle key included, since a name may contain either — so ↑↓/↵/Esc are the only ways out. The cell
  is handed back as styled segments, never raw ANSI: the table measures a cell by its text length
  and would count escape codes as visible.
- `live_select` (incremental search), `text`, `path` (tab-completion), `confirm`, `list_edit`.
- `list_edit` cell types: a column can be plain text (default), a **barrel** field (`col_hints`
  supplies candidate values to cycle through), or a **timestamp** field (`col_types={i: 'timestamp'}`)
  — a split `YYYY-MM-DD HH:MM:SS` mask where you type only the digits and the left/right arrows run
  past the end of one part into the next, with Tab still moving between *columns*. Widths come from
  `col_ratios` with `col_mins` honoured first, so a fixed-shape cell stays readable as the terminal
  narrows and its neighbours give way instead.
- Value editors: `calendar_select`, `datetime_edit` (+ `tz_widget.timezone_select`), `time_edit`,
  `fraction_edit`, `number_edit` (bounded int spinner), `rating_edit` (POPM stars + count + email),
  `rva2_edit` (dB meter), `equaliser_edit` (graphic EQ), `system_editor_edit`.
- **One caret, drawn not borrowed:** every typable field marks its position with
  `prompt_core.block_cursor()` — reverse video *on* the character (a white block at the end of the
  text, where there is nothing left to move). A bar drawn between two characters costs a column, so
  the line slides sideways on every keystroke; `block_cursor_width()` gives callers the padding
  arithmetic. `text`/`path` draw it too rather than positioning the terminal's own cursor, which is
  a thin bar or invisible depending on the terminal.
- **Raw↔widget toggle:** value editors return the `MODE_TOGGLE` sentinel on **Ctrl-T** so
  `prompt_for_value` can flip between the smart widget and a plain text field (#62).
- **Key convention:** `q` **quits the application** — it raises `QuitToTerminal` (a `BaseException`,
  so the editors' broad `except Exception` handlers can't swallow it) on the spot. It is never a way
  to leave a widget and carry on. Backing out is **Esc** everywhere, plus **←/b** in the list
  widgets. There is deliberately no vim-style `hjkl` navigation: those are ordinary letters the
  user may want to type, and binding them to movement made `h` back out of a list without ever
  being advertised in the hint bar. In widgets whose input is free text — the live
  search, `text`, `path`, the timezone search — `q` is a literal character and **Ctrl-C** is the
  quit key. (There used to be a deferred `state.QUIT_REQUESTED` "save the edit, quit at the next
  menu" flag; it committed a half-finished edit as a side effect of quitting and only fired if a
  `select` happened to run next, so it has been removed.)
- **Shared screen chrome:** every screen owes the user the same four things — a hint bar pinned
  above the miniplayer + status bar so its keys never move, those keys clickable, the background
  transport keys listed whenever the miniplayer is up, and clicks on the miniplayer box itself
  doing something. That contract lives in one place:
  `chrome_hint_pairs` / `chrome_hint_lines` (build the pairs, adding `^P`/`^N`/`^B`/`^O` only when
  a handler is actually installed), `append_chrome` (pin to `_hint_pin_target`, render, register
  the click cells), and `consume_chrome` (call it right after `_read_key`; it returns
  `CHROME_HANDLED`, `CHROME_REDRAW`, a replacement key when a hint was clicked, or `None`).
  `enable_mouse`/`disable_mouse` turn click reporting on for widgets that want it.
  **A new widget wires these two call sites and gets all four behaviours** — previously each
  widget grew its own subset and they had drifted badly apart.
  A widget that sizes content to the terminal must budget against
  `_hint_pin_target() - len(chrome_hint_lines(pairs))`, not the raw terminal height, or it draws
  over the miniplayer (both the EQ and the RVA2 meter did).

### Playback — `playback/`

`playback.py` runs VLC, handles keys (seek/volume/panes/help), and drives the lyric/dialogue sync
loop. `playback_ui.py` renders the screen into a **frame buffer** flushed in one write: flow lines
are positioned by a row counter while absolute-positioned items (volume bar, controls, lyrics) pass
through. It has three layout modes (wide / standard / minimal) that size the art to leave room for
the metadata and the (variable-height) hint block, a full-height volume bar clamped to the art
bottom, and toggleable lyrics/queue/credits panes.

### Album art — `art/album_art.py`

Rendered **in-project**: `render_native_half_block` downsamples an image with OpenCV/NumPy into ANSI
half-block (`▀`) cells — no external viewer. Also extracts APIC bytes for playback/editing. (Some
helper names like `_convert_apic_to_viu` are legacy from the old `viu` dependency; the binary is gone.)

## Adding features

- **A bulk operation:** write the pure transform in a new/existing pure module (mirror
  `filename_parser`/`cover_matcher`), unit-test it headlessly, then add a `*_op(paths, library,
  header)` in `bulk_id3_manager` (preview + apply via `tag_writer`) and register it in the
  `Automation…` menu, `op_map`, and dispatch.
- **A tag widget:** add the widget to `prompt.py` (model an existing one; support `MODE_TOGGLE` if it
  has a plain-text form), route it in `prompt_for_value` (by `base_id` for binary frames, else by
  `format_spec`/`ui_category`), and add matching `create_frame` + `summarize_tag_value` branches.
- **A menu option:** add a handler in `menus.py`, insert it in the choice list, return cleanly.
- **A config value:** add it to `config.DEFAULT_CONFIG` and expose it under Settings.

## Testing

```bash
pyright src                                   # type check — keep at 0 errors / 0 warnings
python3 -m compileall -q src                  # syntax/import sanity
BACKTRACK_CONFIG_DIR=/tmp/bt BACKTRACK_CACHE_DIR=/tmp/bt python3 main.py   # isolated live run
```

Prefer a small headless script for pure logic and tag writes (build fixtures with `mutagen`,
round-trip create→save→read). Reserve live runs for the interactive widgets and playback rendering.

## Debugging

- **Config:** `python3 -c "from src.config import load_config; print(load_config())"`.
- **Library cache:** inspect `~/.cache/backtrack/library_cache.json` (or `$BACKTRACK_CACHE_DIR`).
- **Playback:** add targeted logging in `playback.py`/`playback_ui.py`; the frame buffer makes it
  easy to dump the assembled screen before it's written.

## Contributing

- Keep the pure-core / thin-UI split; unit-test pure logic.
- Document functions concisely (see Design principles); keep `pyright src` at 0/0.
- Preserve terminal UX (no wrapping, symmetric margins, consistent nav keys).
- Update this file and README.md when behaviour or structure changes.
