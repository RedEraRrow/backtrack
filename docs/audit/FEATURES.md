# Backtrack — Comprehensive Feature List

> **What this is.** A complete, chapter-organised catalogue of everything Backtrack does — every
> user-facing behaviour *and* every internal mechanism — derived from a line-by-line audit of all
> 29 Python modules (~21k lines) plus the docs and packaging. Each entry carries a `file:line`
> anchor so a feature can be traced straight to its implementation.
>
> Companion document: **[AUDIT.md](AUDIT.md)** — redundancies, inconsistencies, dead code, bugs,
> and consolidation opportunities found during the same pass. Where a feature is impaired by a
> defect, this document notes it inline as **⚠ see AUDIT** and the detail lives there.
>
> Audit date: 2026-08-12. Scope: `main.py`, all of `src/`, `docs/`, `pyproject.toml`,
> `requirements.txt`. Nothing in the codebase was modified to produce these documents.
>
> **Basis: the working tree, including uncommitted changes.** This reflects the current on-disk state,
> which at audit time carries in-flight work in `menus.py`, `music_library.py`, `playback.py`,
> `playback_ui.py`, `prompt_core.py`, and `ui_utils.py` (compilation-aware grouping, group-level queue
> shortcuts, a redesigned queue pane, and tiny-terminal guards — reconciled into the chapters below).
> `PR_NOTES.md` has been removed from the repo. Line anchors track this working tree and will drift as
> the code evolves.

---

## Table of contents

**Part I — Application shell**
1. [Overview & architecture](#1-overview--architecture)
2. [Entry point, lifecycle & shared state](#2-entry-point-lifecycle--shared-state)
3. [Configuration](#3-configuration)

**Part II — Library, search & navigation**
4. [Library: scan, extraction, cache & background sync](#4-library-scan-extraction-cache--background-sync)
5. [Fuzzy search & ranking](#5-fuzzy-search--ranking)
6. [Menus: browse, search, history, settings](#6-menus-browse-search-history-settings)
7. [Listening history](#7-listening-history)

**Part III — Playback**
8. [Playback engine & session model](#8-playback-engine--session-model)
9. [Multi-window / background playback (IPC)](#9-multi-window--background-playback-ipc)
10. [Playback screen renderer](#10-playback-screen-renderer)
11. [Album-art rendering](#11-album-art-rendering)

**Part IV — Lyrics**
12. [Lyrics parsing & display](#12-lyrics-parsing--display)
13. [Unified lyric editor](#13-unified-lyric-editor)

**Part V — Tag editing**
14. [Tag registry & handler (the tag brain)](#14-tag-registry--handler-the-tag-brain)
15. [Single-track tag editor & sort-order engine](#15-single-track-tag-editor--sort-order-engine)
16. [Bulk tag operations & automation](#16-bulk-tag-operations--automation)
17. [Pure tag modules: writer, filename parser, file namer, cover matcher, bulk pattern](#17-pure-tag-modules)

**Part VI — Terminal UI toolkit**
18. [Terminal primitives (prompt_core)](#18-terminal-primitives-prompt_core)
19. [Widget library (prompt)](#19-widget-library-prompt)
20. [UI utilities & terminal input](#20-ui-utilities--terminal-input)
21. [Timezone picker (tz_widget)](#21-timezone-picker-tz_widget)

**Part VII — Project**
22. [Supported formats, dependencies, packaging & docs](#22-supported-formats-dependencies-packaging--docs)

---

# Part I — Application shell

## 1. Overview & architecture

Backtrack is a **terminal-first music player and ID3/MP4 tag editor** for macOS and Linux. It plays a
local library with libVLC, renders album art in-terminal as Unicode half-blocks, shows synced/unsynced
lyrics, and includes a full single-track tag editor plus a bulk-automation suite.

**Architectural spine** (`docs/DEVELOPER.md`):

- **Pure core, thin UI.** Parsing/matching/writing live in small modules intended to be
  unit-testable headlessly (`filename_parser`, `file_namer`, `cover_matcher`, `bulk_pattern`,
  `search`, `tag_writer`); interactive screens are layered on top. (Adherence is uneven — see AUDIT.)
- **One catalogue for tags.** `tag_registry.TAG_REGISTRY` describes every ID3 frame; `ui_category` /
  `format_spec` / `single_only` drive widget dispatch and multi-value gating.
- **Cross-format writing.** Bulk writes route through `tag_writer`, which targets both MP3 (ID3) and
  the MP4 atom family and reports what it cannot represent.
- **Registered-callback decoupling.** The UI substrate (`prompt_core`/`ui_utils`) never imports the
  playback layer; playback registers providers/openers so the now-playing box, Ctrl-P player, and
  activity beacon work without a hard dependency (`src/main.py:140`).

**Module map** (lines audited): entry (`main.py` 8, `src/main.py` 202, `src/menus.py` 1251,
`src/state.py` 16), library/search (`music_library.py` 682, `search.py` 234, `bulk_pattern.py` 143,
`config.py` 64, `history.py` 67), tags (`tag_registry.py` 410, `id3_tag_handler.py` 1014,
`id3_browser.py` 865, `bulk_id3_manager.py` 2065, `tag_writer.py` 373, `filename_parser.py` 500,
`file_namer.py` 280, `cover_matcher.py` 530), lyrics (`lyrics.py` 1091, `lyrics_editor.py` 2364),
playback (`playback.py` 550, `session.py` 726, `ipc.py` 364, `playback_ui.py` 1063), art
(`album_art.py` 115), UI (`prompt_core.py` 975, `prompt.py` 3129, `tz_widget.py` 1534,
`ui_utils.py` 411, `terminal_input.py` 148).

**Data locations** (outside the repo): config `~/.config/backtrack/config.json` (`$BACKTRACK_CONFIG_DIR`),
cache `~/.cache/backtrack/library_cache.json` (`$BACKTRACK_CACHE_DIR`), history
`~/.config/backtrack/history.log`, IPC sockets/registry `$CONFIG_DIR/sessions/`.

## 2. Entry point, lifecycle & shared state

**Launch chain** — `python main.py` → `main.py:5` re-exports `src.main.main`; the console-script
`backtrack = main:main` (`pyproject.toml:37`) resolves to the same. `src/main.py:171` `main()`:

- Best-effort Windows ANSI enablement via `colorama.just_fix_windows_console()` (`src/main.py:174-178`).
  (A second, separate Windows colorama init runs at package import, `src/__init__.py:7-13` — ⚠ see AUDIT.)
- Loads config, wires the playback callbacks (`_wire_playback`), enters the alt-screen, runs `_run`,
  and on exit closes any client link and calls `SESSION.shutdown()` inside a broad guard, then exits
  the alt-screen (`src/main.py:182-201`).

**`_run(config)`** (`src/main.py:92-137`) — the core flow:
- `_maybe_join_session()` first, then seed first-run tag-name preferences, `save_config`, load cache.
- **Cached path:** show status, `start_background_sync`, run `main_menu`, `save_config` on return.
- **First-run path:** clear screen, prompt for a music directory (more can be added later), validate it is a dir (else a loading
  message + 1.5 s pause and return), persist it, `build_library`, save the cache synchronously, start
  background sync, run `main_menu`.

**First-run tag-preference seeding** — `_init_tag_preferences` (`src/main.py:16-23`) fills
`config['tag_name_preferences']` from `TAG_REGISTRY` (each tag → `info.name[0]`) the first time.

**Playback wiring** — `_wire_playback` (`src/main.py:140-168`) registers three hooks so the UI layer
stays decoupled from playback: the now-playing bar provider (`ui_utils.set_now_playing_provider`), a
**Ctrl-P** player opener (`prompt.set_player_opener`) that is role- and view-lock-aware, and the
status-bar **beacon → notification centre** opener (`prompt.set_notification_opener`).

**Shared navigation state** — `src/state.py`:
- `NAV_STACK` (`state.py:2`) — the breadcrumb path (`["Home"]` initially), pushed/popped by menus and
  read by `ui_utils`/`playback_ui` to draw the breadcrumb.
- `QUIT_REQUESTED` (`state.py:7`) — "save current edit, then quit" flag set by value editors and
  consumed by `prompt`/`prompt_core`.
- `QuitToTerminal(BaseException)` (`state.py:10`) — an unwind-to-shell sentinel; deliberately a
  `BaseException` subclass so editor `except Exception` blocks don't swallow it.

## 3. Configuration

`src/config.py` — a JSON config in a platform config dir with an env override.

- **Platform-aware location** (`config.py:27-41`): Windows `%APPDATA%/Backtrack`, else
  `$XDG_CONFIG_HOME/backtrack` or `~/.config/backtrack`; `$BACKTRACK_CONFIG_DIR` overrides. Resolved
  once at import.
- **`DEFAULT_CONFIG`** (`config.py:6-25`) — every default key: `theme` (ANSI codes), `history_enabled`,
  `search_weights`, `lyric_lead_in` (2.0), `art_width` (80), `music_directories` (list; the legacy
  `music_directory` is migrated on load by `load_config` and mirrored back for older builds, with
  `music_dirs()`/`set_music_dirs()` the accessors), `player_view`,
  `ignore_hidden_files`, `show_metadata_editor`, `show_lyrics_editor`, `tag_name_preferences`,
  `sort_list_delimiter` ("/"), `plain_text_editing`, `autoplay_on_select`. (Several keys are never
  read — ⚠ see AUDIT.)
- **`load_config`** (`config.py:43-58`) — creates the dir, writes defaults on first run, else loads and
  `setdefault`s any keys added since (forward-compatible). **`save_config`** (`config.py:60-64`) dumps
  indented JSON.
- **Env-override design** makes isolated live testing possible: point `$BACKTRACK_CONFIG_DIR` and
  `$BACKTRACK_CACHE_DIR` at a temp dir to run the real app against a throwaway config/cache.

---

# Part II — Library, search & navigation

## 4. Library: scan, extraction, cache & background sync

`src/music_library.py` — scans every configured music directory, extracts ID3/MP4 metadata, caches it, and keeps it
fresh on a background thread.

**Scan & extraction**
- Supported extensions `.mp3/.m4a/.mp4/.m4p/.aac` (`music_library.py:18`).
- `build_library(directory, ignore_hidden)` (`:424-446`) — `abspath`+`expanduser`, `os.walk`, full
  tag parse per file via `get_metadata`, skipping `grouping == 'HIDDEN'` files when asked.
- `get_metadata(file_path)` (`:223-263`) — starts from a full default dict, stamps `cached_mtime`,
  dispatches MP3 → `_extract_id3_metadata`, MP4-family → `_extract_mp4_metadata`, then re-opens with
  `mutagen.File` to cache `duration`.
- `_extract_id3_metadata` (`:266-340`) — maps text frames (TIT2/TPE1/TPE2/TALB/TCON/TDRC/TIT3/TBPM),
  **joins multi-value frames with `"; "`**, loads the TSO\* sort frames, splits TRCK/TPOS/MVIN on `/` (with `⁄`→`/`) into
  number+total, reads MVNM/TSST/TIT1, resolves a `work` fallback chain (TXXX:WORK → grouping), and
  builds people as `"Name (Role)"` from TMCL/TIPL (so roles are searchable/displayable).
- `_extract_mp4_metadata` (`:343-421`) — the MP4-atom equivalent (`©nam/©ART/aART/©alb/©gen/©day/©wrk`,
  `trkn`/`disk` tuples, `©mvi/©mvn` movement, `©grp` grouping with work fallback).
- `get_song_duration(file_path)` (`:210-220`) — MP3-only duration probe used for playback timing
  (⚠ returns 0 for non-MP3; see AUDIT).
- `_get_default_metadata` (`:182-207`) — the canonical track-dict shape with "Unknown *" placeholders,
  `play_count`, `duration`, `cached_mtime`, `people`.

**Cache**
- Platform-aware cache dir with `$BACKTRACK_CACHE_DIR` override (`:22-37`); dir created at import.
- `save_library_cache(library, _async)` (`:449-478`) — **atomic** temp-file write + `os.replace`;
  updates `_cache_mtime`; wakes the sync worker; optional async thread.
- `load_library_cache` (`:481-497`) — tolerant of missing/corrupt cache (returns `[]`), drops a legacy
  `performers` key per track.
- `refresh_library_entry(library, file_path)` (`:500-520`) — re-reads one file, updates the in-memory
  list in place, and **persists the cache synchronously** (used everywhere after an edit/rename).

**Background sync** (the "keeps the library fresh" feature)
- `start_background_sync(library)` (`:61-78`) — idempotent; stores the shared list and spawns/pokes a
  daemon `_sync_worker` on a 30 s interval (`SYNC_INTERVAL_SECONDS`, `:19`).
- `_sync_worker` (`:122-179`) — each cycle: shows a live "Checking library…" status, loads music-dir
  config, reconciles the filesystem, mtime-refreshes changed tracks, adopts an externally-changed
  cache, then waits on a trigger event (interruptible).
- `_reconcile_library` (`:81-119`) — a cheap `os.walk` (no tag parse) diffed against known paths:
  **adds** new files, **drops** vanished ones (so external renames/moves are picked up as
  remove+add), with an **unmount guard** — if the scan is empty but the cache holds entries under the
  that root, nothing is removed (a temporarily-missing drive can't wipe the cache). The guard is
  **per root**, and removal is judged against every live root at once, so nesting roots is safe and
  one absent drive affects neither its own tracks nor anyone else's. `build_library` and
  `_reconcile_library` both take one directory or a list (`as_dir_list`), and `build_library`
  de-duplicates so overlapping roots don't scan a file twice.

**Grouping & sorting**
- `get_grouped_data(library, category)` — groups by artist/album/genre/grouping. The
  artist axis prefers album-artist and falls back to `derive_album_credit`. Skips "Unknown"
  categories and normalises classical multi-name artists (splits on `,`/`&`).
- `derive_album_credit(songs)` — the **anchor rule** for an album with no album-artist tag. Each
  track's artist tag is one *cast* (a multi-value credit `A; B` is a duo, not two artists); the album
  is filed under the artists credited on **every** track. So a duet survives a guest on one track,
  a solo album survives a featured guest, and a panel show files under its recurring host — while a
  line-up with nobody throughout collapses to **"Various Artists"**. `menus._album_artist_of` calls
  the same function for the displayed credit, so label and grouping cannot disagree.
- **Derived names are never written** (`is_placeholder_name` / `strip_placeholder_names`,
  `id3_tag_handler`): "Various Artists", "VA", "Unknown Artist" and friends are filtered out of
  TPE1/TPE2/TPE3/TPE4/TCOM and their sort tags by `create_frame` — the choke point for every ID3
  write — and by `tag_writer.write_fields` (reported as `skipped_placeholder`). `filename_parser`
  sets `compilation` on a Compilations folder instead of writing the name, and `_sort_value` returns
  None for one. The name still *appears* everywhere it should: `derive_album_credit` infers it per
  album from the track casts.
- **No title sort order**: TSOT is gone from `_SORT_SRC`, `_SORT_BASE`, `tag_writer._SORT_MAP`,
  `id3_browser._SORT_SOURCES` and the *Apply sort orders* field list. A sort tag equal to its source
  is never written either — `_sort_candidates` excludes the raw value, so `_sort_value` returns None.
- **Stored with `'; '`, displayed with `', '`** (`format_tag_values`): the joined string is the
  storage/round-trip form (cache, editor seeds, clipboard, `summarize_tag_value`'s default `sep`);
  screens render a comma-separated list instead. Applied at the 12 display sites for
  artist/album-artist/genre only — never a single-title field, or an album named `Songs; Ohia` would
  read as two. A value containing its own comma keeps the semicolon separator so the list stays
  unambiguous. `playback_ui._txts` renders every value of a list-like frame (`_txt` returned only
  `text[0]`, hiding all but the first artist/genre in the now-playing panel); the bulk-edit summary
  comma-joins too (it used to concatenate with no separator, summarising a two-genre frame as
  `PopRock`).
- **Multi-value genres fan out** (`split_tag_values` / `group_values`): the `'; '`-joined values of a
  list-like field each become their own group, so a `Pop`+`Rock` album is listed under **both**
  genres — each entry leading to the same album. Single-title fields (album, grouping) are never
  split, so an album called `Songs; Ohia` stays intact.
- **Artist credits stay whole**: two names on one album are a joint *billing*, so the artist axis
  keys on the entire credit rendered for display — `Herbert von Karajan, Berlin Philharmonic &
  Friends` is one group, not two. (Classical normalisation — first name before `,`/`&` — therefore
  applies only to a *single-value* credit that jams several names into one string.) Compilation
  detection compares distinct **casts** (the set of split names) rather than raw strings, so `A; B`
  and `B; A` are one duet, not a compilation (see `derive_album_credit` above). A joint group takes
  its **first** artist's sort name when a paired sort frame exists.

  Deliberately **not** split: casing variants (`Rock` / `rock` stay two groups, so inconsistent tags
  stay visible), and other taggers' in-value delimiters (`Rock/Pop`, `Pop, Rock` are one genre —
  splitting them would mangle `R&B/Soul` and `AC/DC`). A single value that *contains* `;` is
  indistinguishable from two and splits anyway; the joined-string cache format is kept as-is.
- `year_of(value)` / `album_year(songs)` — the year an album sorts under. `year_of` matches the first
  four-digit run, so a TDRC **timestamp** (`2005-09-15 18:30:00`) yields 2005; `int(str(value))`
  raised on those, and every dated file counted as year-less — the bug that dumped 14 dated albums
  into the year-0 bucket to be ordered alphabetically, in both the album sort and
  `sort_library_logic`. `album_year` then picks the **earliest corroborated** year: the oldest that
  two or more tracks share, else the oldest of all. So one stray track can't drag an album backwards,
  while a series whose episodes all differ still reads as the year it began. (Previously: whichever
  track happened to sort first.) A placeholder date such as `1899-01-01` is taken at face value —
  the list stays a faithful view of the tags.
- `get_group_sort_key` and `sort_library_logic` — multi-key ordering
  (artist-sortable, −year, album, disc, track, movement) with a leading-"The " strip.
  The **explicit sort-order tag now takes priority** (it used to be dead code): `_extract_id3_metadata`
  loads TSOT/TSOP/TSO2/TSOA/TSOC — and `_extract_mp4_metadata` the sonm/soar/soaa/soal/soco atoms —
  under the friendly names these keys look up, so `DJ Wren` + `TSO2 = "Wren, DJ"` files under **W**.
  `_tagged_sort_key` pairs a multi-value credit with its multi-value sort frame **by position**
  (`TPE2 = "Cee Dot"/"Dee Ray"` ↔ `TSO2 = "Dot, Cee"/"Ray, Dee"`), so a split group can never inherit
  a co-credited artist's sort name; mismatched counts and derived group names ("Various Artists", a
  classical-normalised surname) fall through to the display name.
- `sort_album_tracks` (`:618-630`) — within-album (disc, track, movement) ordering.
- `to_num` (`:50-58`) — leading-number parse handling `'1/12'` fractions.

## 5. Fuzzy search & ranking

`src/search.py` — a pure, tiered fuzzy matcher/ranker (re-run per keystroke by the live search UI).

- **Tier cascade** per token×field (`match_token`, `:105-131`): exact → prefix → word-boundary →
  substring → subsequence (fzf-style, `_subseq` `:68-89`) → bounded-Levenshtein typo (`_typo`/`_lev`,
  `:47-102`; typo only for tokens ≥3 chars). Base quality per tier is `_TIER` (`:15-18`).
- **Ranking** (`search`, `:167-234`): AND semantics (every query token must match some field); per
  token pick the best field by `score × weight`; accumulate a `fine` score; count "solid" tiers
  (exact/prefix/word/substring) and add a large `_SOLID_BAND` (1000) so **exact substrings always
  outrank fuzzy**; apply recency (×1.15) and play-count (`min(pc,20)×0.05`) boosts; prune the weak
  tail below `min_ratio` (0.15) of the best; final score `solid×1000 + fine`; sort desc; apply limit.
- **Field weights** `DEFAULT_WEIGHTS` (`:10-12`): title 10, artist 7, album 5, genre 3, people 2.
- **Match spans** for highlighting: `_merge_spans`/`highlight_spans` (`:138-159`) return merged
  character spans; `tokenize` (`:162-164`) lowercases and splits the query.

## 6. Menus: browse, search, history, settings

`src/menus.py` — the whole top-level TUI hierarchy.

**Main menu** (`main_menu`, `:1222-1252`): Browse Library · Search · Listening History · Settings ·
Exit; dispatches and unwinds on `QUIT_ALL`.

**Browse** (`handle_browse` `:1161-1178` → `browse_menu` `:706-1158`) — a 5-level loop:
- Level 2 (groups): grouping via `get_grouped_data`, an **A–Z letter index** that auto-enables on
  overflow and toggles with `/`, sort options (`s`, `_GROUP_SORTS`/`_ALBUM_SORTS` `:70-76`), play-all
  (`p`), and bulk-edit (`e`). Backing out of a drilled-in letter returns to the letter index with the
  cursor restored.
- Level 3 (albums): album sublist for Artists/Genres with single-album handling.
- Level 4 (tracks): rich track rows via structured columns — bright title (truncates), full featured
  artist in a dimmed column, duration pinned right; **disc headers** (`▌ Disc 1`), **disc subtitles**,
  and classical **work headers** with **roman movement numbers**; single-option disc/work headers
  auto-play. `_track_columns`/`_album_columns` (`:31-39`), `_disc_track_cell` (`:178-187`).
- Level 5 (per-track actions): Play / Edit Lyrics (only when lyrics exist) / Edit Metadata, honouring
  the auto-play and single-option skips.
- Restores the cursor on back-out at every level via `_idx_of` (`:41-47`); manages `NAV_STACK`.

**Search** (`handle_search`, `:229-322`) — the live fuzzy screen over `prompt.live_select`: results
re-rank per keystroke with **matched characters accented** in each field (`_hl_segments`
`:158-175`, `_search_result_cells` `:208-226`); columns are title · artist · album · **people**
(who/role matched) · disc/track · duration; **Tab cycles scope** (all → title → artist → album →
genre → people, `_SCOPE_CYCLE` `:138-139`); an "Edit tags — all N results" row opens the bulk editor;
per-result actions Play / Edit Lyrics / Edit Metadata, honouring auto-play.

**Listening history** (`handle_history`, `:386-449`) — the last 30 plays in aligned columns (title ·
artist · album · when · listened) with **relative timestamps** (`_relative_time` `:342-365`:
`just now`/`40m ago`/`2w ago`, date fallback) and readable durations (`_nice_dur` `:368-383`);
`Unknown Artist/Album` blanked; per-entry Play / Edit Lyrics. (No Edit Metadata here — ⚠ see AUDIT.)

**Queue building** — `play_queue` (`:626-646`) starts the shared session with a queue and repeat mode.
Row-level shortcuts **`n` = play next / `a` = add to queue** (`_queue_shortcut_kwargs` `:678-721`,
`_handle_queue_action` `:725`) now act not only on an individual track row but on **group, album, and
disc/work-header rows** — pressing `n`/`a` on an artist, album, disc, or classical-work row queues the
whole collection at once (`_queue_titles_for_paths` `:673`). Shown only when a session is
active/joined, auto-starting an idle session. `_queue_action_choices` (`:649-654`) provides the
menu-based equivalents for the search path.

**Settings** (`handle_settings`, `:494-623`) — sectioned via `separator()` into PLAYBACK / LIBRARY /
EDITORS / HISTORY. Toggles/actions: Activity Centre, Toggle Listening History, Clear History Log
(confirm + count, no-op when empty), Toggle Auto-play on Select, Adjust Lyric Lead-in (validated,
clamped ≥0), Toggle Metadata Editor, Toggle Lyrics Editor, Toggle Plain-text Editing, Tag Name
Preferences, Sort List Delimiter, Update Music Directory (rescan + in-place library swap), Toggle
Hidden Files. Persists each loop; restores the cursor to the acted row.

**Tag Name Preferences editor** (`_handle_tag_name_preferences`, `:452-491`) — a `list_edit` grid of
every registry tag with a locked TAG-ID column and an editable preferred-name column (barrel selector
cycling registry aliases, or free text); saves per-tag with a changed-count toast.

**Activity / notification centre** (`notification_centre`, `:1181-1219`) — a live full-screen panel
listing running `BACKGROUND_TASKS` with pulsing status dots and an "N running" count; opens from
Settings and from clicking the status-bar ● beacon; closes on Esc/b/q.

## 7. Listening history

`src/history.py` — an append-only log at `CONFIG_DIR/history.log` (`timestamp | duration | path`).

- `log_listening_history(path, start, end)` (`:10-19`) — appends `"{ts} | {dur}s | {path}"`; swallows
  `OSError`. (Gating on `history_enabled` lives in the caller — ⚠ see AUDIT.)
- `get_recent_paths()` (`:22-37`) — distinct recently-played paths (feeds search recency boost).
- `get_history(limit=30)` (`:50-67`) — newest-first `(timestamp, duration, path)` tuples.
- `clear_history()` (`:40-47`) — deletes the log (True on success/absent).

---

# Part III — Playback

## 8. Playback engine & session model

Audio and transport live in **`src/playback/session.py`** (the process-wide engine); **`playback.py`**
is the on-screen *view* over it.

**`PlaybackSession`** (singleton `SESSION`, `session.py:117-533`) — owns one libVLC instance/player,
the queue (paths/titles/index/mode), the track lifecycle, a background tick, and history logging.
- `start(...)` (`:170-193`) — sets queue/titles/index/mode, loads the current track, advertises the
  session over IPC.
- `_load(path)` (`:195-233`) — logs the previous track, reads ID3 (empty ID3 on failure), gets
  duration, creates media/player, `play()`, settles, backfills duration, **applies the stored
  equaliser**, bumps a `generation` counter.
- Transport: `pause_toggle`, `seek`/`seek_to`/`_handle_seek` (clamps to `[0, duration-0.5]`),
  `set_volume`/`get_volume`, `next`/`prev` (repeat-aware), `enqueue`, `play_next`,
  `elapsed`/`latest_at`.
- **Volume is session state, not a VLC read-back.** `libvlc_audio_get_volume` returns **-1** while no
  audio output is open (before playback starts, after a stop), which is how the player view could come
  up reading "-1". The session holds `_volume` (default 100), `set_volume` remembers it even with no
  player yet — a level chosen before playback used to be dropped — `play` re-applies it to each new
  track, and `get_volume` adopts VLC's reading only before the user has set a level and only when it
  is in 0–100. `clamp_volume()` guards every display path (the snapshot published to other windows,
  the remote proxy's `get_volume`, and the remote render call), and the bar's percentage label is now
  printed from the same clamped value the fill is drawn from. The level **persists**: `config.volume`
  is restored in `__init__` (and counts as explicit, so VLC's default can't replace it) and written
  back by `_persist_volume` on every change — best-effort, so an unwritable config can't interrupt
  playback. `bind_config` adopts the app's live config dict as well, so a dict held since startup
  being saved on quit can't write a stale level back over the current one.
- **Repeat modes** off/one/all (`REPEAT_*`, `:42-44`).
- `tick()` (`:433-451`) — end-of-track detection + auto-advance; `start_background_tick`/`_tick_loop`
  (`:453-477`) is a daemon that advances the queue and **logs history even with no view attached**, and
  pulses the now-playing box.
- `now_playing()` (`:481-515`) — the snapshot dict broadcast to the UI and to joined windows.
- History: `_log_history` (`:303-310`) → `history.log_listening_history`, guarded once per track.
- `shutdown()` (`:327-343`) — stops the tick, the IPC server, and audio; restores stderr.

**Equaliser application** — `_apply_equalizer(mp, audio)` (`session.py:65-94`) reads `EQU2` frames,
builds a `vlc.AudioEqualizer`, snaps each point to the nearest libVLC band, clamps gain to ±20 dB, and
applies it on load; `playback.py:306-308` shows a 2.5 s "♫ Equaliser applied" toast when the file has
an `EQU2` tag. (The **24 EQ presets** the docs advertise are authored in the tag editor and stored as
`EQU2`; this layer only *applies* whatever bands the tag holds — see Ch. 19 for the preset table.)

**Foreground view** (`playback.py`):
- `music_player(...)` (`:79-103`) — entry point; if joined, forwards the play to the host; otherwise
  starts the session and opens the player view.
- `open_player_view` (`:106-122`) — claims the cross-window **view lock**, runs the host loop, releases
  on detach (never stops audio on detach).
- `_player_view_loop` (`:234-543`) — per-track prepare (`_prepare` `:272-308`), full redraw, and the
  input+sync loop: an early `SESSION.is_active()` guard (exits when the session stops), resize debounce,
  toast expiry, `SESSION.tick()` auto-advance **plus a `(generation, file_path)` track-change detector**
  that re-prepares the view when a track changes out from under it (e.g. a remote/host-driven advance),
  live queue-pane refresh (queue paths threaded through `set_queue_context`), progress bar, and the
  lyric/dialogue sync (including SYLT→USLT tail handoff).

**Playback key map** (host loop, `playback.py:400-482`; README table):

| Key | Action |
|---|---|
| `space` / `p` / `P` | Play / pause (`:404`) |
| `←` / `→` | Seek ∓5 s (`:409`) |
| `j` / `l` | Seek ∓1 s (`:435`) |
| `,` / `.` | Seek ∓30 s (`:423`) |
| `+`=`/` `-`=`_` | Volume ±5 (`:460`) |
| `↑` / `↓` | Nudge USLT lyric line (4 s auto-revert) (`:417`) |
| `n` | Next track (`:443`) |
| `b` / `B` / `Esc` | Minimise & keep playing (pinned if another window is attached) (`:447`) |
| `s` | Stop (`:455`) |
| `q` | Quit app (`:458`) |
| `i` | Toggle help/hints (`:470`) |
| `w` | Cycle right pane: off → lyrics → queue → lyrics+credits (`:474`) |
| `m` | Toggle extended metadata (`:478`) |
| `e` | **(TEMP/debug)** jump to last 35 s (`:431`) — ⚠ see AUDIT |

(README still describes `b` as "stop and return" — that changed with multi-window playback; `s`, `p`,
`e` are undocumented in the README table — ⚠ see AUDIT.)

## 9. Multi-window / background playback (IPC)

The headline feature of PR #14: a process-wide audio session decoupled from the view, keeping audio
alive when the player is minimised and **shared across multiple terminal windows** over a local socket.

**Transport** — `src/playback/ipc.py` (pure, playback-agnostic, headlessly testable):
- Per-session `AF_UNIX` socket + a JSON registry under `$CONFIG_DIR/sessions/`; newline-delimited JSON
  wire protocol (`_send`/`_iter_messages`, `:37-61`).
- **Discovery** `list_sessions()` (`:106-138`) — reads every registry JSON, probes socket liveness,
  returns live sessions newest-first, and cleans up crashed/stale entries.
- **`SessionServer`** (`:145-275`) — binds the socket (removing a stale one), writes the registry,
  accepts clients, **broadcasts now-playing snapshots ~4 Hz**, forwards client commands to a handler,
  exposes `peer_count`.
- **`SessionClient`** (`:282-364`) — connects, mirrors snapshots (fires `on_snapshot`/`on_disconnect`),
  sends commands, `latest`/`latest_at` for interpolation.

**Coordination** — `session.py`:
- **Advertising**: `_ensure_advertised` (`:244-262`) starts a server on first play unless this window
  is itself a client; `_handle_remote_command` (`:264-288`) dispatches client commands
  (play/pause/next/prev/seek/set_volume/stop/enqueue/play_next/acquire_view/release_view).
- **Remote control**: `RemoteSession` (`:547-606`) presents the same control surface as
  `PlaybackSession` but forwards each action to the host; `active_session()` returns the local session
  when hosting or the proxy when joined, so menus/queue-actions/player drive playback identically.
- **Single player view across windows**: a per-process token (`my_token()`), a `view_holder` broadcast
  in the snapshot, and `acquire_view`/`release_view` (`:519-532`) enforce one player at a time.
- **Host handoff on crash/quit**: the snapshot carries full session state; on host disconnect a joined
  window runs `attempt_handoff` (`:659`) — an **`O_EXCL` lock-file election**; the single winner
  re-hosts on the same socket id, reconstructs the queue, and resumes the current track near its last
  position (`_become_host_from`); losers reconnect (`_rejoin_after_handoff`).

**Launch/join wiring** — `src/main.py`: `_maybe_join_session` (`:51-89`) shows a **New / Join ⟨label —
▶ now-playing⟩ chooser** when other live sessions exist; on join it mirrors the host read-only and can
auto-grab the free player (`_take_player_if_free` `:26-48`). The **ambient now-playing box** appears in
every menu/browse screen (drawn by the UI substrate, not per-widget), with a **`^P player`** hint and a
progress-bar bottom border; it hides session-wide while a full player view is open.

## 10. Playback screen renderer

`src/playback/playback_ui.py` — assembles the whole playback screen into a **frame buffer** flushed in
one write.

- **Frame-buffer model** (`_render_frame_buffer`, `:34-49`) — "flow" lines are positioned by an
  incrementing row counter while **absolute-positioned** items (volume bar, controls, lyrics,
  credits/queue — matched by `_ABS_ROW_RE` requiring a trailing `H`, `:20`) pass through and consume no
  row slot. This two-track model is the fix for the historic metadata-displacement bug.
- **Three layout modes** by width (`_layout_mode`, `:92-98`): wide (≥120), standard (≥60), minimal.
  `_draw_default_ui` (`:715-1063`) sizes the art to leave room for metadata and the variable-height
  hint block, vertically centres it, and branches per mode (wide no-pane/split; standard single-column
  with queue/credits; minimal art-or-meta).
- **Art fitting** — `_get_art_cached` (`:101-117`, mtime-keyed with stale eviction) and
  `_art_width_for_height` (`:150-166`, aspect-aware down-fit to available height).
## Rendering: the persistent screen model

`prompt_core` keeps one dict of **what is currently on each screen row**, shared by every writer —
widget frames, the now-playing box, the status bar, and the full player view. Painting a frame writes
only the rows whose content actually changed, absolutely positioned, in one buffered write:

- `screen_row_paint(row, text, extra)` — the escape string for one row, **empty when it already reads
  that way**. `extra` is the absolute-overlay layer (the volume bar writes a few columns of the album
  art's own rows): both layers form the row's identity, so a row repaints when *either* changes, and a
  row whose overlay went away is erased instead of keeping stale glyphs. **Blank is content** — a row
  left out of a frame is erased, not left showing the previous screen. `screen_row_segment(row, text)`
  is the plain-text case. `screen_paint(rows, …)` paints a mapping in one flush; `screen_invalidate()` forgets
  everything (registered as `ui_utils.set_screen_invalidator`, so every existing `clear_screen()`
  keeps meaning "the screen is blank now"); `screen_forget_rows` releases rows another writer owns.
- **No erase-to-end-of-screen, no newlines.** The old frame was `ESC[J` followed by every row with a
  trailing `\n` — on a tty `sys.stdout` is line-buffered, so each newline flushed and the terminal
  painted the frame in ~40 pieces, and the wipe took the miniplayer and status bar with it every
  keystroke. Frames now contain no newlines at all, so one frame is one flush.
- `screen_takeover_next()` — a new widget's first frame overwrites what it needs and blanks the rest
  of the previous screen *in the same paint*, so moving between screens no longer flashes blank. The
  15 widget-entry `clear_screen()` calls became takeovers; a resize still clears (the terminal
  reflowed, so nothing on screen can be trusted). `_Widget.refresh()` is the same idea for a
  focus-in repaint.
- The **now-playing box** and **status bar** are diffed writers too: the 8 Hz idle tick writes
  **0 bytes** when nothing changed, and `ui_utils.now_playing_lines` keeps its last good rows when the
  provider raises instead of blinking the box out and back.
- The **player view** (`_render_frame_buffer`) drops its per-frame clear and diffs as well, so a
  progress tick repaints the one row that moved instead of the album art. Overlays are layered over
  their row rather than replacing it, and every row the frame stops using — including one whose
  overlay vanished — is erased. The player **owns the whole screen**, so a frame also blanks any row
  the painter still remembers from the screen before it (the menu's miniplayer box, a taller list):
  the view clears only on *exit*, and without its old per-frame clear a shorter first frame left the
  previous screen's bottom rows showing. Writers that paint outside a frame (`update_progress_ui`,
  `draw_volume_bar`, the four lyric-pane drawers) call `screen_forget_rows` for their band, so the
  model never claims to know a row something else painted.
- `ui_utils.get_terminal_size` is memoised (invalidated by SIGWINCH; a 0.25 s TTL where SIGWINCH
  doesn't exist) — it was an ioctl per *line* on the clipping path.

**Cursor visibility.** Hidden by default, everywhere: `clear_screen()` and `enter_alt_screen()` end
with `?25l` (a bare clear parks the cursor at home, where it blinks in the top-left until some later
frame happens to hide it — visible during a library build or any slow step), `_Widget.clear()` no
longer hands back a shown cursor at teardown, and the two caret widgets (`text`, `path`) hide on exit
instead of showing. Returning from `$EDITOR` invalidates the screen model and re-hides. The only two
places that show it are a text caret (`screen_paint(cursor=…)`) and `exit_alt_screen()`, where the app
hands the terminal back.

Measured on a 30-row album list at 100×40: a cursor move went from ~994 bytes and 35 partial-frame
flushes to **76 bytes, one flush, two rows**; an unchanged frame from a full repaint to **nothing**;
a player progress tick from 1346 bytes and a screen clear to **36 bytes**.

- **Settings screen** — every row carries its current state in a **left-aligned column just beside
  the labels** (`✔`/`✘` for the on/off settings, the value itself for the rest), so a setting can be
  read without toggling it — pinned to the far right, the state was too far from its label to scan;
  rows dispatch on a stable value rather than their label, and labels/separators are sentence case.
- **`^t` hint coverage** — `_with_toggle_hint()` in `prompt.py` appends the Ctrl-T pair to a widget's
  hint bar while the per-edit raw-text toggle is live. Every widget that *answers* `^t`
  (`text`, `list_edit` — including its fixed-rows variant, which never showed it —
  `calendar_select`, `datetime_edit`, `fraction_edit`, `time_edit`, `number_edit`) now advertises it,
  and the hint is clickable: `"^t"` synthesises the real control character via `_hint_key_tokens`.
- **Full-height volume bar** (`_volume_bar_cells`) — vertical bar right of the art, clamped
  so its bottom lines up with the clipped art bottom, `█`/`░` fill, `♪` cap, right-aligned percentage
  taken from the clamped fill percentage (never a raw, possibly-negative reading); live redraw via
  `draw_volume_bar`.
- **Progress** — `update_progress_ui` (`:169-190`) draws the horizontal progress bar + `mm:ss / mm:ss`
  aligned to the art.
- **Settings → Music Directories** (`_music_dirs_menu`, `menus.py`) — add a directory, remove one
  (confirmed; removing the last one warns that the library will empty), or *Re-scan now*; each change
  rebuilds the cache over all roots via `_rescan_library` and restarts the background sync. Rows for
  a root that isn't currently mounted are marked "(not available)".
- **Metadata line** — `_meta_left_lines`: **TIT2** / **TPE1**(→TPE2)—**TALB**, then behind the `m`
  toggle: **year only** (TDRC→TYER→TDOR→TORY, 4-digit match so a full date shows just the year),
  **TCON**, **TSST or else TPOS**, **TRCK** — degrading to filename then path. Artist and genre render
  every value of a multi-value frame via `_txts`/`format_value_list` (`_txt` returned `text[0]`, so a
  duet showed one name); TRCK/TPOS render in display form ("Track 3 of 12", "Disc 1 of 2"). **TIT1**
  (work) and **MVIN** (movement, as a Roman numeral via `_movement_roman` → `utils.numbering`) sit
  alongside the disc fields, but a movement **suppresses the track number** — it already says where
  the track sits in the work — so a classical track reads
  `1963 · Classical · Symphony No. 5 · Movement III` while its unnumbered sibling reads
  `1963 · Classical · Track 1 of 4`. The whole panel is fed an `ID3` object, so it stays blank for
  non-MP3 playback.
- **Panes** — toggleable metadata/credits/lyrics/help/queue flags with a single-key right-pane cycle
  (`cycle_right_pane`, `:420-437`, skipping empty states); a redesigned **UP NEXT** queue pane
  (`_build_queue_lines`, `:483`) — a **columnar** list (title + a **context-aware meta column**)
  showing the upcoming tracks (current + following, backfilled with previous), laid out with the shared
  `_table_widths` engine. The meta column only appears when it adds information: it shows the track
  artist when it differs from the album artist (featured artists / compilations,
  `_queue_should_show_artist` `:579`, `_queue_meta_value` `:596`) and the album when the queue spans
  several albums; per-track metadata is loaded via `_queue_metadata` (`:450`, a `get_metadata` read per
  queued path). **Cast/crew credits** render in two columns (`_build_cast_lines` `:209`,
  `_build_crew_lines` `:686` — crew cast-ordering is impaired by a case bug, ⚠ see AUDIT).
- **Ambient now-playing box** — `format_now_playing_bar` (`:325-384`) whose bottom border is the
  progress bar; hidden while a full player view holds the session.

## 11. Album-art rendering

`src/art/album_art.py` — in-project half-block rendering (no external viewer).

- `render_native_half_block(img_bytes, width)` (`:10-39`) — `cv2.imdecode` → `cv2.resize` (INTER_AREA)
  → per-cell `\033[38;2…m\033[48;2…m▀` (U+2580) pairing top/bottom pixel rows, black for the missing
  bottom row on odd heights. Requires a 24-bit-truecolor terminal.
- `render_album_art(source, width, is_bytes)` (`:41-59`) — bytes-or-path dispatch.
- `_select_apic_frame` (`:67-87`) — pick APIC by description, then picture type, else first;
  `get_art_from_mp3` (`:90-103`) loads ID3 and renders; `get_art` (`:106-115`) dispatches by extension.
- `ART_MAX_WIDTH = 200` caps render width. (Legacy "viu" naming survives only in comments here and in
  `id3_browser.py` — ⚠ see AUDIT.)

---

# Part IV — Lyrics

## 12. Lyrics parsing & display

`src/lyrics/lyrics.py` — SYLT/USLT parsing, timing estimation, markdown-dialogue support, and the
three-line context windows drawn during playback.

**Frame parse/write**
- `_parse_sylt` (`:743-749`) — concatenate every SYLT frame's `(text, ts_ms)`, sort by timestamp.
- `_parse_uslt` (`:772-781`) — first USLT frame, normalise newlines, per-line list dropping blanks.
- `save_sylt_entries` (`:752-769`) — replace all SYLT with one UTF-8 `eng` frame (format=2 absolute-ms,
  type=1), saved via `id3_tag_handler.save_id3`.
- `normalize_lyric_newlines` (`:14-18`) — CRLF/CR → `\n`.

**Untimed (USLT) timing model**
- `build_uslt_line_times` (`:157-187`) — estimate `(start,end)` per untimed line from a track-wide
  words-per-second (fallback 2.2). (⚠ strips text before the first colon — see AUDIT.)
- `expand_uslt_lines` (`:115-154`) — split over-tall lines with proportional sub-timing (cached).
- `find_current_uslt_line` (`:190-194`) — `bisect_right` over end-times.
- SYLT→USLT handoff: `estimate_sylt_last_line_end` (`:784-791`) and `find_uslt_handoff_index`
  (`:794-806`) find where to resume in USLT after SYLT ends.

**Sentence/clause wrapping** — `_sentence_split` (`:29-79`) and `_sub_split_sentence` (`:82-108`) break
long lines at sentence/clause boundaries for readable display, masking abbreviation dots via
`_ABBREV_RE` (`:21-26`).

**Markdown dialogue** (audiobook/radio-drama support)
- `_parse_markdown_dialogue` (`:197-241`) — speakers, stage directions `*(…)*`, and continuations into
  `DialogueLine`s (`:275-295`).
- `_apply_markdown_formatting` (`:243-262`) — inline `**bold**`/`*em*`/`[label](url)` → ANSI,
  base-style-preserving; `_strip_markdown` (`:596-598`) for plain display.
- `expand_dialogue_into_sentences` (`:307-458`) — the dialogue timing model: per-sentence timing from
  word timings (cursor-based match) or proportional fallback, stage-direction gap windows, and
  synthetic "air"/silence chunks past `_AIR_THRESHOLD` (2.0 s).
- WhisperX-style JSON word timings: `_parse_word_timings_json` (`:882-901`), `_matchable` (`:948-958`,
  NFKD, letters/numbers only), `_match_md_to_timings` (`:961-999`), enriched-transcript discovery and
  dead-air merge (`_find_enriched_transcript` `:904-930`, `_merge_air_beats` `:1047-1064`), sidecar
  discovery for md/srt/json (`:808-867`).
- `DialoguePlaybackState` (`:1002-1073`) — the playback-consumed state machine that finds files,
  parses, matches timings, expands to chunks, and updates `current_idx`.

**Rendering** — three-line context windows with dimmed neighbours: `draw_lyric_window` (SYLT,
`:601-645`), `draw_uslt_window` (`:648-707`, with manual scroll + `↕ scroll` hint),
`draw_dialogue_window` (`:461-586`, two-column speaker│text with `⋯` air indicators),
`draw_lyric_initial` (`:710-740`, one-line pre-play preview).

## 13. Unified lyric editor

`src/lyrics/lyrics_editor.py` — a full terminal lyric/transcript sync editor. It auto-detects one of
three sources (transcript JSON with word timing → SYLT line timing → USLT untimed) and offers several
modes.

- **Source detection & load** — `find_lyrics` (`:350-360`, public: which source exists), `_load`
  (`:363-423`, resume from a `.sync.json` sidecar with external-edit **drift detection** via a
  size+md5 fingerprint `_file_fp` `:270-279`, else bootstrap from raw JSON/SYLT/USLT).
- **SEG mode** (segment/line editing) — navigate ↑↓; adjust timing ←→ ±0.25 s, `,`/`.` ±0.1 s,
  `[`/`]` ±1 s; multi-select `spc` + batch shift; `e` segmented timestamp editor; `j` join; `J`/`K`
  move; `a` insert dead air; `d` delete dead-air/stage-dir; `l` relabel; `k` dead-air↔stage-dir;
  `r` fill gaps / auto-time stage dirs; `m` import/remove MD overlay; `M` commit stage dirs; **`c`
  append Music-by/Words-by credits** from TCOM/TEXT; `w` word view; `t` tap-sync; `b` audition;
  `p` preview; `s` save; `V` verify; `S` split by MD line; `W` write transcript.json+SRT; `u` undo.
- **WORD mode** — per-word timing adjust, `x` split a segment at a word.
- **EDIT mode** — a segmented `MM:SS.mmm` start+end field editor (tab/arrows/spin/digits).
- **TAP mode** (VLC) — audio plays, `spc`/`↵` marks the current line start with a configurable
  **lead-in** offset (`lyric_lead_in`), auto-advancing; nudge the previous line.
- **AUDITION mode** (VLC) — plays the whole line on land; move the line by ear, hear start/end
  boundaries, inline duration editor.
- **Mouse** — scroll to navigate, click to position the cursor, click a dialogue row to open the word
  view, click a timed dead-air to recategorise.
- **MD overlay & verification** — `_build_md_overlay` (`:911-1121`, difflib word alignment JSON↔MD,
  speaker banners, `✦` stage-direction anchoring, `md` quality flags), `_verify_matchup` (`:1201-1291`,
  EXTRA/MISSING/CHANGED report written to `.verify.txt` and paged), `do_speaker_split` (`:1648`).
- **Persistence** — `do_save` (`:1531-1575`): transcript source → `.sync.json`; else write a **SYLT
  tag** via `save_sylt_entries`. `do_commit` (`:1577-1600`): Whisper-schema `transcript.json` + `.srt`.
  Full **undo** stack (`do_undo` `:1490-1529`) across shifts/splits/joins/deletes/taps.
  (Round-trip has losses and the USLT→SYLT conversion leaves the original USLT — ⚠ see AUDIT.)

---

# Part V — Tag editing

## 14. Tag registry & handler (the tag brain)

**`src/id3/tag_registry.py`** — the single frame catalogue.
- `TagInfo` dataclass (`:26-41`): `tag_id`, `name[]` (friendly aliases; first is the default label),
  `frame_type`, `format_spec`, `official_category`, `ui_category`, `single_only`, `mutagen_class`.
- `TAG_REGISTRY` (`:44-338`) — ~90 frames grouped Text / Timestamp / Legacy-v2.3 / Fractional /
  List-people / URL / Binary, each mapped to its mutagen class. This drives widget dispatch, multi-value
  gating, friendly names, and rename compatibility.
- Lookups: `parse_composite_tag_id` (`:342-361`, splits `TXXX:desc:lang` / `COMM[eng]`), `get_tag_info`
  (`:364-370`), `get_tag_category` (`:379-382`), `get_preferred_tag_name` (`:403-411`, config-driven).

**`src/id3/id3_tag_handler.py`** — frame construction, value prompting, summaries, and saving.
- **`save_id3`** (`:192-202`) — the version-selection policy: **v2.4 iff any frame is multi-value
  (`_has_multivalue`), else v2.3** (max player compatibility; avoids `/`-collapse corruption).
- **`create_frame(tag_id, value)`** (`:205-356`) — builds the right mutagen frame: structured binary by
  base id (EQU2/RVA2/POPM/PCNT/RBUF), `ui_category=='image'` → APIC (MIME-sniffed), then text/ISO8601/
  fractional/numeric/list/date/year/time branches.
- **`prompt_for_value`** (`:604-811`) — the master widget dispatcher: structured binary by base id;
  enum/bool text by base id (TKEY/TMED/TSRC/TCMP); else generic by `ui_category`+`format_spec`
  (image cover picker, multiline → system editor, people → list editor, date/year/time/fraction/int
  widgets). Handles the **multi-value default** (single field, Ctrl-T to a list editor) and the
  **MODE_TOGGLE** raw↔widget loop carrying a half-typed value.
- **Structured editors & pickers** (`:420-601`): EQU2 graphic EQ, RVA2 gain, POPM rating, PCNT count,
  TKEY musical-key, TMED media-type, TSRC ISRC validation (`CC-RRR-YY-NNNNN` shape-check), TCMP yes/no,
  RBUF buffer.
- **POPM 0–5★ ↔ 0–255** WMP-scale mapping (`_POPM_STAR_BYTES = (0,1,64,128,196,255)`, boundaries at
  32/96/160/224, `:441-466`).
- **Album-art picker** — `pick_nearby_cover` (`:81-164`) ranks nearby covers (via `cover_matcher`) with
  "Type a path…"/"No art" escapes and picture-type/description metadata.
- **Display/summary** — `display_tag_id` (`:814-817`, strips empty-descriptor colon), `summarize_tag_value`
  (`:820-880`, per-category one-liners: people count, image `[MIME] (N KB)`, POPM stars, TCMP Yes/No,
  EQ bands/gain, generic capped text).
- **Bulk primitives** — `collect_tag_data` (`:883-917`, presence/value tallies), `apply_bulk_edit`
  (`:920-964`, set/rename/delete on one ID3), `apply_bulk_operation_to_files` (`:967-1014`).

(Several frame kinds are prompt-editable but not creatable — URL and most binary frames — and
`rename_frame` is broken by a pop-before-search caller contract. These are significant ⚠ AUDIT items.)

## 15. Single-track tag editor & sort-order engine

`src/id3/id3_browser.py` — the MP3-only single-track editor UI and the pure sort-order heuristic engine
(the single source of truth for sort generation, reused by the bulk manager).

**Editor** (`inspect_tag_loop`, `:550-866`):
- Rejects non-`.mp3` up front with a friendly message (`:559-561`).
- Boxed header with live title/artist, `[EXT]`, duration, size, degrading tags→cache→filename and
  filtering "Unknown" placeholders (`_main_header` `:591-656`).
- Tag list in structured columns with friendly names (`:670-699`) plus a synthetic read-only **File
  path** row (with "copy to clipboard").
- **Add Tag** (`a` shortcut, `:718-741`) — prompt an id (composite TXXX/COMM/APIC parsed), route through
  `prompt_for_value` (sort-tags prefilled by the engine), create + save.
- Per-tag actions (`:788-865`): Copy / Paste (confirm + recreate) / Edit / Rename / Delete (confirm),
  plus **Import LRC** for USLT/SYLT and **Manage** for images. People frames render a ROLE/NAME table.
- **Cover art editor** (`_edit_apic_tag`) — one screen: the art with a facts line above it
  (`{w}×{h} px · mime · KB · colour mode`), the picture type and description, then the actions —
  *Open in image viewer*, *Replace image…*, *Type and description…*, *Save a copy…*, *Remove image*,
  each with a dim note in a right-hand column. Notes on the rework:
  - The old *View art* / *View info* rows (mutually exclusive, appearing and disappearing from the
    list) are gone: the facts fit alongside the picture.
  - **Replace** uses `pick_nearby_cover` — the same ranked picker as the tag editor's image field —
    instead of demanding a typed path, and **keeps the picture type and description**, which it used
    to reset to "Cover (front)"/blank on every replace.
  - **Type and description** reuses `_prompt_for_image_metadata` (one screen for both), and rebuilds
    the frame so mutagen re-keys it: editing `.desc` in place left the frame under its *old*
    `APIC:desc` key. The editor then **tracks that key** — deleting by the key it was *called* with
    matched nothing once the description had changed, so a second edit (setting a description then
    reverting it) added a frame instead of replacing one and left two near-identical images behind.
    Replace and Remove use the tracked key too.
  - *Save a copy* and *Remove image* are new — there was no way to export or delete an image.
  - Art is cached per (image bytes, width) in `_ART_CACHE` (shared with the single-tag header): a
    redraw was re-decoding the JPEG every keystroke — 7 ms → 0.006 ms.
  - The header is **one rounded-box line** — `APIC · Cover (back) · “desc”` bold-left, the facts
    dim-right — matching this file's track header and the bulk-edit header. Facts shed from the right
    (colour mode, then format, then weight) rather than wrapping.
  - **Sizing** (`_art_width` / `_art_lines_boxed`) is driven by the rows left after the screen's
    chrome, capped at `_ART_MAX_WIDTH` (64 — a comfortable thumbnail, not wallpaper), less
    `_ART_BREATHING_ROWS`, re-rendered narrower for a portrait image, and drawn **centred** in a
    rounded box. Below `_MIN_ART_ROWS` (6) the art becomes a dim "— art hidden (window too short) —"
    line, and below ~14 visible rows the facts and description lines drop too, so the screen degrades
    instead of overflowing. The old `min(height × 1.5, width)` overflowed any tall window (60 cells
    wide → 30 art rows plus 14 of chrome on a 37-row screen).
  - Rows carry **no invented glyphs**: the app's established prefixes are `▸` (play) and `＋` (add),
    and the neighbouring tag-action rows (Copy / Paste / Edit / Rename / Delete) have none.
  - Saving is left to the caller's `_save`, which also refreshes the library entry; the editor
    returns whether anything actually changed, so an unmodified visit writes nothing.
- **LRC import** (`_parse_lrc_file` `:394-416`, `_import_from_lrc` `:419-454`) — `[mm:ss.fff]` timed →
  SYLT, untimed → USLT.

**Sort-order engine** (pure heuristics, **no name corpus** — verified accurate):
- `_sort_single_name` (`:181-254`) — the name-inversion pipeline: merge space-separated initials
  (`J. S.`→`J.S.`, `_merge_initials` `:137-153`), merge Celtic joining prefixes (`O'`/`Mc`/`Mac`,
  `_merge_joining_prefixes` `:156-178`, case-guarded so hip-hop MC stays separate), move leading
  articles to the tail (multi-language `_ARTICLE_MAP` `:59-74`), strip leading honorifics to the tail
  (`_HONORIFICS` `:90-95`), retain trailing generational/credential suffixes (`_NAME_SUFFIXES`
  `:99-102`), handle spacing surname prefixes (`von`/`van`/`de`, `_SPACING_PREFIXES` `:78-82`), and
  offer positional right-splits (last-word-as-surname first).
- `_sort_candidates` (`:257-299`) — top-level generator: splits multi-artist strings on
  `&/feat./and/with/|/+` (only *possessive* backing-band phrases protected — "and his/her/their" is
  not a delimiter, but "& The …" is, so `Jeff Goldblum & The Mildred Snitzer Orchestra` becomes
  `Goldblum, Jeff/Mildred Snitzer Orchestra, The` rather than being inverted whole into
  `Orchestra, Jeff Goldblum & The Mildred Snitzer`; `_LIST_SPLIT_RE`), sorts each and
  rejoins with the config delimiter; article-only move for title/album tags; adds ordinal-padded
  variants (`Series 1`→`Series 01`).
- `_prompt_sort_order` (`:302-333`) — prefill for a simple name; a ranked `select` picker with "type
  custom" for ambiguous ones. `_SORT_SOURCES` (`:47-53`) maps each sort frame to its source frame.

## 16. Bulk tag operations & automation

`src/id3/bulk_id3_manager.py` — the two-level bulk editor (entry `bulk_id3_manager()`,
`:1409-1869`): a **TAGS** menu and an **Automation…** submenu. Every op previews before writing and,
by default, fills only blank tags.

**TAGS** (raw-ID3, MP3-only):
- **Add New Tag** (`:1626-1641`) — type-aware `prompt_for_value` for known ids, composite ids parsed.
- **Set Common Value** (`:1664-1726`) — three value sources: literal, **find & replace (regex)** over
  each existing value, and **from file name / folder (regex)** expanding a template; people tags route
  to the people editor.
- **Rename Tags** (`:1660-1663`) — change the 4-letter frame id (category-interlocked).
- **Delete Tags** — multi-select delete.
- **Copy From First Track** (`:1727-1729`) — deep-copy source frames from track 1 to all others.
- **People-list editor** (`bulk_people_editor`, `:245-366`) — aggregates distinct role→name entries
  across files with N/total coverage; add/edit/remove an entry across every file that has it, order
  preserved; import from CSV/TSV/columns.
- **APIC bulk** — replace image / edit description / edit picture type / skip.

**Automation** (cross-format MP3+MP4 via `tag_writer`, except where noted):
- **Derive from filename** (`derive_from_filename`, `:414-600`) — field multi-select (Title pre-ticked),
  fill-blanks/overwrite, auto/template/regex detection via `filename_parser.derive_all`, a preview that
  lifts uniform fields into the header and shows only varying fields as columns, `d` per-file detail
  view, per-row deselect, and sort-order augmentation. Writes MP3+MP4.
- **Rename files from tags** (`rename_files_op`, `:726-842`) — `%token%` preset/custom pattern (smart
  default folds `%artist%` only for compilations), preview, **collision-safe two-phase rename**
  (mkstemp → os.replace, per-file rollback), library-path sync. MP3+MP4.
- **Set album art from files** (`set_album_art_op`, `:901-1136`) — 4 pairing modes + grouped sub-modes
  via `cover_matcher`, per-directory planning, live-count header, `d` ranked per-file picker with
  "apply to all". Writes MP3 APIC + MP4 `covr` (JPEG/PNG).
- **Assign by range/schedule** (`assign_by_pattern`, `:1238-1406`) — assign one tag by ranges / every-N
  / date schedule (start + interval + granularity + per-group times) via `bulk_pattern`. Raw-ID3,
  MP3-only.
- **Apply sort orders** (`apply_sort_orders`, `:1139-1235`) — (re)generate TSOP/TSO2/TSOA/TSOT from
  existing tags via the #15 engine. Raw-ID3, MP3-only.
- **Renumber tracks (disc ↔ continuous)** (`renumber_tracks_op`, `:634-694`) — convert numbering via
  `bulk_pattern.renumber_tracks`. Writes MP3+MP4.

Every write path refreshes the in-memory library entry. (The raw-ID3 ops re-implement the same apply
scaffolding 4×, diverge on MP3-only vs cross-format, and there is dead legacy code at the tail — ⚠ see
AUDIT.)

## 17. Pure tag modules

**`tag_writer.py`** — the format-agnostic writer.
- `write_fields(path, values, apply_fields, overwrite)` (`:171-241`) — MP3 (fresh ID3 header for a
  blank file) or MP4 atoms; fill-blanks default; carries the compilation flag and sort frames with a
  written base field; returns a `WriteResult` (`written`/`skipped_existing`/`skipped_format`/`error`/
  `unsupported`, `.changed`).
- `write_cover(path, data, mime, …)` (`:323-373`) — MP3 APIC (replace) / MP4 `covr` (JPEG/PNG only,
  else `skipped_format`).
- `present_fields`/`has_cover`/`writable_fields`/`format_kind` (`:59-168`, `:301-320`) back the
  fill-blanks previews; MP4 `disc_subtitle` is dropped (no atom).

**`filename_parser.py`** (genuinely pure — string ops only) — path → tag fields:
- `_match_numbering` (`:94-116`) — `SxxExx`, `2x04`, `1-05` disc-track, vinyl `A1`, plain `\d{1,3}`,
  `Track N` (4-digit years guarded out).
- Folder rules — `CD/Disc/Disk/Series/Season` folders collapse into the album and set the disc/subtitle
  (`_folder_disc_sub` `:124-140`); album/album-artist from the folder chain; Various-Artists detection;
  year from `Album (1997)` / `1997 - Album` / a filename date; `Artist - Album (Year)` fallback.
- `parse_one`/`derive_all` (`:191-338`) orchestrate per-file derivation and fill totals per (album,disc)
  group; `%token%` templates (`compile_template` `:455-491`) and named-group **regex** over the file
  name or library-relative folder path (`compile_regex`/`_regex_target` `:410-442`).

**`file_namer.py`** — tags → `%token%` names (~32 tokens `:20-53`): reads tag values, zero-pads
numbers, sanitises illegal chars, and `plan_renames` (`:240-280`) computes collision-safe renames with
` (2)`/` (3)` disambiguation.

**`cover_matcher.py`** — pair tracks ↔ cover files: discovery in the album folder + `artwork/covers/
scans/` subfolders + the parent album folder above a disc dir (`find_images` `:81-118`); scoring
(exact stem 1000, track-number, title-word overlap, generic names ranked low; `score_match` `:177-215`);
and five bulk strategies `plan_auto/basename/positional/grouped/best/template` (`:274-504`) with
confidence labels.

- **Set picture type** (`set_picture_type_op`) — retypes art that's already embedded, without
  touching the image: rippers routinely tag a front cover as "Other" (type 0), which anything looking
  specifically for a front cover then misses. Picks the type with the existing
  `_prompt_for_picture_type` (the header shows what's currently in the selection, e.g.
  `Other ×495 · Cover (front) ×411`), previews `Other → Cover (front)` per file with already-correct
  rows unticked, and writes via `tag_writer.retype_cover` — which rebuilds each APIC under its own
  key, skips frames already of that type, and reports MP4 as unsupported (`covr` has no type field).
- **Remove single-disc numbering** (`strip_single_disc_op`) — strips `TPOS`/`disk` from tracks that
  are **disc 1 of 1**, where the tag is noise (it shows up as a disc header in browse lists and in
  names derived from tags). A bare `1` with no total counts too, but only when nothing else in the
  selection sits on another disc — on a real multi-disc album an untotalled "1" is meaningful. Same
  preview-with-ticks flow as the other disc operations; rows say `disc 1/1 → —` or `keeps disc 2/3`
  so the reasoning is visible. Clearing goes through the new `tag_writer.clear_fields` (MP3 + MP4),
  since `write_fields` can only *set* a value — it skips anything empty.

**`bulk_pattern.py`** (pure) — positional assignment: `order_tracks`, `assign_ranges`
(`{n}`/`{r}`/`{en}` = range index), `assign_periodic` (every-N), `assign_dates`
(start + interval + per-group times → ISO timestamps), and `renumber_tracks`
(continuous↔per-disc).

**`utils/numbering.py`** (pure) — the three number styles shared by every patterning tool:
**arabic** (`{n}`, `{n:02d}`), **roman** (`{r}` → III, `{r:l}` → iii) and **written out**
(`{en}` → Three, plus `:l`/`:u`/`:t` cases). `render` writes them, and `parse`/`from_roman`/
`from_words` read them back (Roman must be canonical — `IIII` is text, not 4). `ui_utils.roman`
delegates here, so one implementation owns the conversion. Reaches:
- `bulk_pattern.fmt_value` — `Series {n}` · `Act {r}` · `Series {en}` in range and every-N values.
- `file_namer.render` — a style suffix on any numeric token: `%track:r%` → `IV`, `%disc:en%` → `Two`,
  `%movementno:r:l%` → `iv`; `unknown_tokens` reports a bad style as `track:zz`.
- `filename_parser.compile_template` — the same suffix on the *parse* side (`Act %track:r%`,
  `Series %disc:en%`), and `_fields_from_groups` reads a numeric capture in any style, so a raw
  regex capturing `III` or `three` yields 3.

---

# Part VI — Terminal UI toolkit

## 18. Terminal primitives (prompt_core)

`src/utils/prompt_core.py` — the raw-terminal foundation every screen builds on.

- **Raw-mode switching** — `_get_term_attrs`/`_set_raw`/`_restore_term_attrs` (`:47-61`, Windows-aware).
- **Key decoder** — `_read_key_raw` (`:790-871`): arrows/HOME/END/PGUP/PGDN/TAB/ENTER/BACKSPACE/SPACE/
  CTRL_C, **SGR mouse** (click/release/scroll with row/col, `:816-835`), UTF-8 multi-byte assembly, and
  **focus in/out** events (FOCUS_IN triggers a now-playing repaint). `_read_key` (`:782-787`) discards
  focus events.
- **Anchored widget renderer** — `_Widget` (`:907-975`): anchors a line list to an absolute row,
  clears ghost lines from a taller prior render, and **atomically restamps the status bar + now-playing
  box in the same flush** (so callers must not redraw those separately). `anchor_reset` forces a full
  redraw on resize.
- **Adaptive hint engine** — `_hint` (`:208-343`): a five-layer responsive cascade (centred line →
  upside-down pyramid → uniform grid → aligned stack → split key/value) for the bottom hint bar.
- **Table layout engine** — `_table_widths` (`:460-570`, content-driven widths with `max_frac`/
  `max_width`/`min_width` caps, flex fill, right-pin, and **priority-based column dropping** on narrow
  terminals) + `_render_table_row` (`:573-621`) + styled-segment cells (`_render_cell_segments`
  `:429-457`). Data model `Choice`/`Column`/`separator`/`_norm` (`:356-402`, `:758-779`).
- **Now-playing box + wake pipe** (#14) — a self-pipe (`os.pipe`, `:74-92`) lets background threads
  repaint the box without a keystroke; `_wait_for_keypress` (`:147-188`) selects on `[stdin, wake]`,
  drains the pipe, and throttles the idle repaint (~4–8 Hz); `now_playing_box_segment`/
  `invalidate_now_playing_box` support atomic + focus-in repaints.
- **String-grammar column parsing** (`_split_columns`/`_render_select_columns`, `:629-742`) — legacy
  parse-a-title path retained for older select/checkbox rows.
- `_visible_rows`/`_cols`/`_rows`/`_clip_ansi` (`:873-887`, `:202-204`, `:666-695`) — sizing and
  ANSI-safe truncation helpers.

## 19. Widget library (prompt)

`src/utils/prompt.py` — ~15 widgets built on `prompt_core`.

- **`select`** (`:81-493`) — the one list widget: single/multi, structured `columns`, `interlock`
  (one-category-checkable), `on_inspect`+`inspect_key` (`d` detail view), `row_actions` (arbitrary
  per-row keys), toggle-all (`a`) in multi, **Ctrl-P** player, **beacon-click** notifications,
  mouse click/scroll (two-step + double-click), FOCUS_IN repaint, `shortcuts` map, vi keys.
- **`live_select`** (`:496-683`) — incremental search box: `provider(query)` re-ranks per keystroke,
  `Tab` scope cycle (`on_cycle`), PgUp/PgDn jumps, caret editing.
- **`confirm`** (`:686-720`) — yes/no with a default.
- **`text`** (`:723-818`) — wrapped free-text line editor with a bordered frame and Ctrl-T raw toggle.
- **`path`** (`:821-1004`) — path editor with **Tab-cycling autocomplete** and a 5-item completion
  tooltip.
- **`list_edit`** (`:1241-1632`) — editable table: add/edit-in-place/delete/reorder (`K`/`J`),
  barrel-mode value cycling, import from text (`i`) / from file (`f`, via `_parse_import_rows`
  auto-detecting CSV/TSV/`;`/`|`/`:`/columns), `locked_cols`, `fixed_rows`, Ctrl-T raw toggle.
- **Value editors**: `calendar_select` (`:1706`, month/year/day nav, manual entry), `datetime_edit`
  (`:1921`, calendar + `HH:MM:SS.ms`, TZ strip), `time_edit` (`:2312`, validated), `fraction_edit`
  (`:2176`, current/total with tag-aware labels), `number_edit` (`:2741`, bounded spinner),
  `rating_edit` (`:2834`, POPM stars/plays/email), `rva2_edit` (`:2673`, ±12 dB meter),
  `equaliser_edit` (`:2950`, 10-band graphic EQ + **24 named presets** `_EQ_PRESETS` `:2490-2515`,
  add/delete bands, mouse band-select), `system_editor_edit` (`:3113`, `$EDITOR`/fallback external
  editor).
- **MODE_TOGGLE** (`:43-50`) — the Ctrl-T raw↔widget sentinel protocol driven by
  `id3_tag_handler.prompt_for_value`, carrying a half-typed value across the flip.
- **Registered hooks** — `set_player_opener` / `set_notification_opener` (`:60`, `:69`) keep the widget
  layer decoupled from playback and the notification centre.

(Two confirmed widget bugs — `list_edit` inverted discard, `datetime_edit` TAB dead-end — and uneven
MODE_TOGGLE/cancel conventions are ⚠ AUDIT items.)

## 20. UI utilities & terminal input

**`src/utils/ui_utils.py`** — the widely-shared UI substrate:
- **Resize** — SIGWINCH handler + `consume_resize` (`:14-41`) reporting terminal resize *or*
  now-playing box height change. (Registered unconditionally — crashes on Windows; ⚠ see AUDIT.)
- **Colours** (`:48-65`) — ANSI constants including `PRIMARY` (bold white) and a distinct `WHITE`
  (normal white, `:50`); **alt-screen** enter/exit with focus reporting (`:67-81`), **screen clear**.
- **Now-playing subsystem** (`:88-168`) — `BACKGROUND_TASKS` registry, the provider/signature/waker
  registration hooks (dependency inversion so the substrate needn't import playback), `now_playing_lines/
  active/height`, and `pulse_now_playing`.
- **Status/toast bar** — `get_status_line` (`:212-246`) assembles breadcrumb (left) + pulsing tasks/
  toast (right) with a `_pulse_circle` 256-colour cyan ramp, returning `""` on a zero-width terminal and
  **clipping overflow to the terminal width** (`clip_ansi`); `show_status`/`show_loading`/`set_status`.
  The now-playing box, status bar, and breadcrumb now guard against tiny terminals (`rows/cols ≤ 1`) so
  they don't draw at invalid rows.
- **Text/geometry** — `get_terminal_size/width/height`, `truncate_text`, `strip_ansi`/`visual_len`/
  `clip_ansi`, `divider`, `format_time`, `_get_breadcrumb_str`, `roman`, `get_progress_bar`
  (pip/rich-style with a smooth tip).

**`src/utils/terminal_input.py`** — non-blocking key reads for the playback loop:
- `raw_mode(file)` (`:32-49`) context manager; `get_key_non_blocking()` (`:59-140`) assembles escape
  sequences across calls (per-byte 0.02 s wait, 0.1 s stale-flush), emits a lone `Esc` immediately,
  translates `\x1b[I/O` → `FOCUS_IN/OUT`, and returns raw strings; `is_arrow_key` (`:143-148`).

## 21. Timezone picker (tz_widget)

`src/utils/tz_widget.py` — a full-screen, braille-rendered world-map **timezone picker**:
- A 365-entry timezone table (`_TIMEZONES`, `:23-428`: offset, lat/lon, IANA name, abbr, city, capital
  flag) and two embedded zlib+base64 rasters — a 2880×1440 coastline bitmap and a 1440×720 timezone
  raster.
- `_render_world_map` (`:1131-1271`) — equirectangular projection with 2×4 **braille sub-pixel**
  coastline glyphs, live timezone-region highlighting keyed to the selected offset, selected-city
  (`◉`) and same-offset-capital (`•`) markers, and an equator/prime-meridian overlay.
- `timezone_select(initial_offset)` (`:1274-1522`) — offset navigator strip, per-offset zone list,
  type-to-search across all entries; returns ISO offsets (`"Z"`, `"+05:30"`, `"-08:00"`). Runnable
  standalone (`__main__`, `:1525`).

> **Status note (important):** despite being a complete, polished feature, `timezone_select` has **no
> caller in the app** — `datetime_edit` strips and discards timezone info. It ships effectively as a
> standalone toy. It also does no DST computation. See AUDIT for the full analysis.

---

# Part VII — Project

## 22. Supported formats, dependencies, packaging & docs

**Supported formats** (README): audio MP3/M4A/MP4/M4P/AAC; tags ID3v2 (MP3) and MP4 atoms — single-track
tag *editing* is MP3-only, bulk ops write both; lyrics USLT/SYLT; album art embedded MP3 `APIC` and MP4
`covr` (JPEG/PNG), rendered in-terminal as half-blocks.

**Dependencies** (`requirements.txt`, `pyproject.toml`): `mutagen` (tags), `python-vlc` (playback),
`opencv-python` + `numpy` (in-project art), `pyperclip` (clipboard), `colorama` (Windows ANSI). No
external image viewer — art is computed in-project.

**Packaging** (`pyproject.toml`): setuptools build, `backtrack` console-script → `main:main`,
`packages=["src"]`, `py-modules=["main"]`. `requires-python >=3.8`. (Classifiers cap at 3.11 while the
project runs on 3.13/3.14 — ⚠ see AUDIT.)

**Windows package init** (`src/__init__.py`) initialises colorama on `os.name == "nt"`.

**Documentation** — README (user guide + controls table), `docs/DEVELOPER.md` (architecture, data
locations, design principles, adding-features recipes), `docs/tag-etiquette.md` (general ID3 practice +
how Backtrack reads each tag), `docs/filesystem-etiquette.md` (on-disk library organisation),
`docs/library-layout.md` (exact parser reference for *Derive from filename*, with `%token%`/regex
overrides), and `TODO.md` (a detailed, ID-stable changelog/backlog).

---

*End of feature list. See [AUDIT.md](AUDIT.md) for the companion findings — dead code, duplication,
bugs, inconsistencies, concurrency concerns, doc/packaging drift, and consolidation opportunities.*
