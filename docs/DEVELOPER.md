# Developer Guide

Architecture and development practices for Backtrack. See also **[README.md](../README.md)** for
user-facing docs and **[tag-etiquette.md](tag-etiquette.md)** / **[library-layout.md](library-layout.md)**
/ **[filesystem-etiquette.md](filesystem-etiquette.md)** for tag/library conventions.

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
│       │                       #   column/table layout, hint engine
│       ├── prompt.py           # The widget library built on prompt_core (see "Prompt widgets")
│       ├── tz_widget.py        # Full-screen world-map timezone picker (runnable standalone)
│       ├── terminal_input.py   # Non-blocking key reads for the playback loop
│       └── ui_utils.py         # ANSI helpers, terminal size, margins, breadcrumb status bar
└── docs/                       # This guide + user docs
```

### Data locations (outside the repo)

| Data | Path | Override |
|---|---|---|
| Config | `~/.config/backtrack/config.json` | `$BACKTRACK_CONFIG_DIR` |
| Library cache | `~/.cache/backtrack/library_cache.json` | `$BACKTRACK_CACHE_DIR` |
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

Scans the configured music dir, extracts metadata from ID3 (`_extract_id3_metadata`) and MP4
(`_extract_mp4_metadata`), and caches it. A background sync thread runs on an interval and
**reconciles against the filesystem** (a cheap `os.walk` diff), picking up external adds/removes and
renames/moves, with an unmount guard so a temporarily-missing drive doesn't wipe the cache. In-app
edits call `refresh_library_entry` to update the cache immediately.

### Search — `search.py`

A fuzzy matcher/ranker: tiered exact → prefix → word-boundary → substring → subsequence → bounded-
Levenshtein typo, scored by field weight × match geometry with recency/play-count boosts, returning
match spans. `prompt.live_select` re-runs it per keystroke and highlights matched characters.

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
  prefixes, article move, positional split) that offer ranked candidates. No name corpus — anything
  it gets wrong the user fixes by picking a candidate or "type custom".
- **`bulk_id3_manager.py`** — the bulk editor: a two-level menu, **TAGS** (add/set/rename/delete) and
  **Automation…** (derive, rename files, set album art, assign by range/schedule, apply sort orders,
  renumber tracks, copy from first track). Each automation op builds a preview then applies via the
  pure modules + `tag_writer`.

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
- `live_select` (incremental search), `text`, `path` (tab-completion), `confirm`, `list_edit`.
- Value editors: `calendar_select`, `datetime_edit` (+ `tz_widget.timezone_select`), `time_edit`,
  `fraction_edit`, `number_edit` (bounded int spinner), `rating_edit` (POPM stars + count + email),
  `rva2_edit` (dB meter), `equaliser_edit` (graphic EQ), `system_editor_edit`.
- **Raw↔widget toggle:** value editors return the `MODE_TOGGLE` sentinel on **Ctrl-T** so
  `prompt_for_value` can flip between the smart widget and a plain text field (#62).

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
