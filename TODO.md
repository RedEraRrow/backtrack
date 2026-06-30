# Backtrack — TODO

**Status:** ✅ done · 🟡 partial · ⬜ open
IDs (`#N`) are stable so cross-references keep working; items are grouped by area, not by number.

---

## Session handoff — read me first

**Architecture you'll be working with (all in `src/utils/prompt.py` unless noted):**
- **Unified list widget.** `select(message, choices, *, columns=None, multiple=False, interlock_category_callback=None, index=0, shortcuts=None, header=None, extra_hints=None)` is the ONE list widget. `checkbox(...)` is now just a wrapper calling `select(multiple=True)`. Single-select returns the chosen value (or None); multi-select returns a list of checked values (or None on cancel).
- **Structured columns (no string parsing anywhere).** Pass `columns=[Column(...), ...]` and give each `Choice(cells=[...])`. A `Column` has `style` (`'primary'`/`'static-dim'`/`'dynamic-dim'`/`'accent'`/`'normal'`), `align`, `flex` (absorbs leftover, truncates), `pin` (laid against the right edge), `min_width`/`max_width`/`max_frac`, `gap`. A **cell** is a `str`, a `(text, style)` tuple, or a **list of segments** (so one column mixes styles — e.g. column 1 = `[(TAG,'primary'), (' (friendly)','static-dim')]`). Helpers: `_table_widths`, `_render_table_row`, `_render_cell`, `_cell_segments`. The old `_split_columns`/title-parsers were deleted.
- **Never-wrap guarantee:** `_clip_ansi(line, width)` clips every emitted line (ANSI-aware) so lists never wrap. `_COLUMNS_MAX_WIDTH` (140) caps layout width on ultrawide terminals; `_EDGE_MARGIN` (4) keeps pinned columns off the edge.
- **Quit/back:** `q`/`Q` raise `QuitToTerminal` (caught in `main()`); value editors set `_state.QUIT_REQUESTED` to commit-then-quit. Back = `←`/`b`/`h`/`Esc`.
- **Focus-aware cursor:** focus reporting (`\033[?1004h`) is enabled in `ui_utils.enter_alt_screen`; `_read_key` emits `FOCUS_IN`/`FOCUS_OUT` and sets `_FOCUSED`; `_block_caret()`/`_char_caret()` draw hollow when blurred.
- **Tag display:** `id3_tag_handler.display_tag_id()` strips a trailing `:` from empty-descriptor keys (`APIC:` → `APIC`). `create_frame` dispatches album art on `ui_category == 'image'` (NOT `frame_type`); image payload is a dict `{'__image__', 'data', 'type', 'desc'}`. EQU2/RVA2 dispatch by base id.
- **Playback render:** `playback_ui._draw_default_ui` builds a frame buffer; `_render_frame_buffer` now only advances the row counter for FLOW lines (absolute-positioned items like the volume bar pass through without consuming a row — this was the metadata-displacement bug). `_meta_left_lines` does the title/artist/album + toggled extras.
- **Lyrics:** `save_sylt_entries` lives in `src/lyrics/lyrics.py` now (`lyric_timer.py` deleted).

**Verify before coding:** `pyright src` (currently 0/0) and a quick `python3 -c "import src.main, src.menus, src.playback.playback, ..."`. Python does NOT hot-reload — the app must be relaunched to see code changes (this bit us on #20/#82 below).

**Highest-value next steps:** confirm the live-pass items (bottom of file), especially **#82** (metadata-on-screen) after a fresh relaunch; then continue the chosen batches (Tag power-user #43/#62/#63, Search+EQ #8/#59, or remaining hygiene #48/#70).

---

## Navigation & input

- ✅ **#1 — Additional nav keys.** `←`/`b`/`h`/`Esc` = back, `→`/`l` = confirm, `↑↓`/`jk` = move, `q` = quit.
- ✅ **#10 — `q` = global save-and-quit to terminal.** Menus quit immediately; value editors commit the in-progress edit first, then quit on the next menu. `q` stays literal only in free-text fields.
- ✅ **#11 — Universal back button.** `←`/`b`/`h`/`Esc` returns from any `select()`/`checkbox` menu, including editors.
- ✅ **#22 — Logical, consistent hints.** Unified back/quit wording across widgets; `space` not `spc`; the right column is one `panel` hint.
- ✅ **#24 — Mouse clicks only register over text/buttons,** not the empty space in a row (`select()` + `checkbox()`).
- ✅ **#78 — Shared `select()` capabilities** underpinning the above: `separator()`/disabled rows (#7), `index=` to restore cursor (#5), `shortcuts=` (#27), `columns=` mode (#75).
- 🟡 **#23 — Vim-like shortcut system.** `hjkl` navigation + `select(shortcuts=)` done; a fuller scheme is still open.
- ✅ **#49 — Don't show hints for unavailable actions.** Conditional throughout: the `w`/panel hint only shows with content; multi-select's `space toggle` only in multiple mode; the value editors only list available keys.
- ✅ **#41 — `b` in playback returns to the previous page.** New `STOP` status: `b` stops playback and drops back to the browse list / action menu you came from (distinct from `n` next-track and `q` quit-app); shown in the help hints.
- ✅ **#52 — Esc backs out of text fields.** `Esc` cancels (→ None) in `text()` and `path()` (where `←`/`b` are literal/cursor); `Enter` still confirms.
- ✅ **#55 — Restore cursor on back.** `select(index=)` threaded through main menu, browse-by, and the group/album/track levels via `_idx_of`, so backing out lands on the item you came from. (Search/history results still one-shot.)
- ✅ **#58 — Two-step mouse select.** First click moves the cursor, second click confirms. Column rows act on any click; plain rows only act over the text itself.

## Browse & library

- ✅ **#9 — Always show an album list** in artist/genre browse, even for a single album (no more single-album auto-skip).
- ✅ **#12 — Hide "Play all"/"Edit tags"** when a group/album has only one track (track + album level).
- ✅ **#25 — Richer track rows.** Featured-artist marker (when track artist ≠ album artist) + duration; duration is now cached.
- ✅ **#26 — Prefer tag data over filenames.** The metadata editor shows an explicit, clearly-labelled "File path" row marked as not ID3.
- ✅ **#28 — Absolute library paths.** `build_library` + config resolve via `abspath`/`expanduser`.
- ✅ **#33 — Prettier browse metadata.** Built on structured columns (#53). Track rows: bright title (truncates), the **full** featured artist in a dimmed left-aligned column, and a dimmed duration pinned further right with an edge margin. Album rows: album name + dimmed album artist. The album artist also shows in the header subtitle.
- ✅ **#34 — Cleaner disc/work separators.** Left-bar section markers (`▌ Disc 1`, indented `▏ Work`) instead of dashed rules.
- ✅ **#44 — Nicer listening history.** Shows `title — artist` (filename only as fallback), a readable duration (`3m 5s`) and a trimmed timestamp.
- ⬜ **#68 — Revisit XML / m4p logic** (noted as not working in recent memory and skipped — verify).

## Search

- ⬜ **#8 — Clearer search results.** Make matches obvious by showing what was matched, in a pretty way.

## Playback

- ✅ **#13 — Queue view.** A single `w` cycles the right column: off → lyrics → queue → lyrics+credits; unavailable views are skipped.
- ✅ **#19 — Audio-general language** (not radio-specific). Player credits: CAST → PERFORMERS / PRODUCTION TEAM; `c` hint = credits.
- ✅ **#20 — Album & artist always display;** the `m` toggle governs only the extras: year, genre, and one of {work + movement | disc-or-subtitle + track | just track}. Single-disc (`x/1`) shows no disc; disc subtitle replaces "Disc N" when present.
- ✅ **#21 — Visual volume indicator.** Full-height vertical bar right of the album art (incl. split view); dim fill with a hollow "tube" for the unused section; 5% steps; live updates. Progress bar is white.
- ✅ **#31 — `w` does nothing when there's no queue/lyrics/credits** (the cycle only includes available views).
- ✅ **#51 — Metadata show/hide fixed.** Artist shows even with no album (and vice-versa); extras built robustly from frame text so they appear whenever present and the toggle is on.
- ⬜ **#14 — Playback pop-out** so you can keep browsing/editing. *PARKED — needs design discussion; leaning towards in-app background audio + a "now playing" bar.*
- ✅ **#47 — Hide the "Edit Lyrics" action** for tracks with no lyrics at all (gated on `find_lyrics()` in search, history, and browse).
- ⬜ **#50 — Low volume is silent.** ≤10% is inaudible; should be quiet-but-audible except at 0%. *Deferred — needs a live audio test; likely a perceptual/log mapping for `audio_set_volume`.*
- ✅ **#57 — Centre/balance the queue view** (the block is now horizontally centred within the pane).
- ✅ **#80 — Hints must never be cut off.** `ctrl_row` clamped to `rows - len(shortcut_lines) - 1` in all four playback rendering branches so hints always stay within the terminal.
- ✅ **#81 — Volume bar placement + stability.** Fixed.
- ✅ **#82 — Metadata reliably on-screen in playback.** Fixed.

## Metadata & tag editor

- ✅ **#15 — Bulk uses the specific value prompts.** Bulk "Add New Tag" reuses `prompt_for_value` like single-track editing.
- ✅ **#17 — Bulk header box spans the full width.**
- ✅ **#27 — Add Tag is a shortcut** (`a`), not a list button.
- ✅ **#54 — Friendly names fill the column then truncate** (single-track). Removed the hardcoded 22-char cap; the column sizes to the longest name (bounded to the window) and truncates per row keeping the `)`.
- ✅ **#75 — Single-track editor mirrors the bulk layout.** 4 columns: `TAG (friendly name) · type | value`, with friendly name + type dimmed; full-width rounded box header.
- ✅ **#76 — Editor header uses real tags,** not the filename: `title · artist` + duration + `[format]`/size.
- 🟡 **#16 — Audit every tag's input method.** Graphic-EQ widget for EQU2 + dB editor for RVA2 done; a broader numeric/rating-input audit is still open.
- ✅ **#53 — Structured columns + merged select/checkbox (no parsing).** `select(columns=[Column(...)], multiple=False)` + each `Choice(cells=[...])`; the renderer lays out/styles per-column with zero string parsing. `checkbox()` is now a thin wrapper over `select(multiple=True)` (one widget, shared layout/keys/clip/focus). Tag keys like `APIC:Front Cover` and values like `3/12` can never be misparsed; the bulk picker uses cells too (old title parser deleted). A cell can be a string, a `(text, style)` tuple, or a **list of segments** so one column can mix styles — the tag/bulk column 1 holds `TAG` (bright) + ` (friendly)` (dim) together.
- ✅ **#36 — File-path row styling.** "⌁ File path" is white inactive, white+bold active; the `(filesystem)` hint is dimmed (via the columnar friendly-name styling).
- ✅ **#37 — In active rows, bold and dim the friendly name** but not the tag type (both column renderers).
- ✅ **#38 — "Import list from text"** now pre-fills a format template in the editor; `f` key imports directly from a file path.
- ✅ **#39 — Hollow cursor when the window is unfocused.** Focus reporting (`\033[?1004h`) enabled app-wide; the in-place field editors (list/fraction/time/date-time) draw a solid `█` when focused, a hollow `▢` when not. *Wants a live check — focus events can't be tested headlessly.*
- ✅ **#40 — System editor.** `_find_editor()` tries `$EDITOR` first, then probes micro/nano/vim/vi/emacs via `shutil.which`; clear error if nothing found.
- ✅ **#42 — Smart sort-order default.** When adding a sort tag (TSOT/TSOA/TSOP/TSO2/TSOC), prefills from the matching source tag and generates ranked candidates via a multi-pass heuristic engine: merges space-separated initials (J. S. → J.S.), merges Celtic joining prefixes (Mc/Mac/O'), strips leading honorifics (Dr./Prof./Sir/MC/DJ/Lady/Lord/…) and trailing suffixes (Jr./III), checks a known compound-surname corpus, uses a given-name corpus to weight the most likely split, detects spacing surname prefixes (von/van/de/…), and offers all right-splits as fallback. Extends article stripping to French/Spanish/German/Italian/Dutch. Pads single-digit ordinals (Series 1 → Series 01). Multi-artist strings (split on &/feat./and/with/|/+/… — but not "and his/her/their" which describes a backing ensemble) sort each entity and rejoin with a configurable delimiter (Settings → EDITORS → Sort List Delimiter, default /). Multiple candidates shown via select; single candidate prefills silently; "type custom" escape always available. Known single-name and multi-word stage-name artists (Björk, Aphex Twin, Massive Attack, etc.) are detected as mononyms and returned unchanged. **Corpus (JSON-backed, config-selectable full/minimal):** full corpus now has 9,066 given names across 40+ naming traditions (classical, jazz, blues, hip-hop, electronic, reggae, Latin, African, Middle Eastern, Indian, East Asian, Celtic, Basque, Catalan, Baltic, Balkan, Hungarian, Romanian, and more), 570 compound surnames, 371 mononyms, 65 honorifics, 28 ordinal suffixes. **MusicBrainz validation pipeline** (`scripts/mb_validate.py` + `scripts/mb_mine.py` + `scripts/mb_artists.tsv`): streams the MB full dump (2.9M artists), validates sort candidates against MB's authoritative sort names, reports accuracy with exact/normalised/close/mismatch breakdown, and mines mismatches for corpus improvements. Current accuracy: **94–95% against 1.35M Latin-script artists**. Sort engine bug fixes applied this session: `MC X`/`DJ X`/`Dr. X` now sort as `X, MC`/`X, DJ`/`X, Dr.`; `MC` no longer mis-merged as Celtic prefix; `Artist and His Band` no longer splits on `and his/her/their`.
- ⬜ **#60 — Multi-value tags.** Many text frames support multiple values (à la Picard); offer that here.
- ✅ **#61 — Copy tag across tracks.** "Copy from first track" bulk operation reads source frames from track 1 and deep-copies them to all other tracks.
- ✅ **#63 — Logical default friendly-name order.** Reordered: `TOPE`/`TOLY` drop `(s)` suffix first; `TKEY`→"Key"; `TLAN`→"Language"; `TDRC`→"Year"; date timestamps say "date" not "time"; `TSSE`→"Encoder"; `WOAR`→"Artist URL".

## Dates & equaliser editors

- ✅ **#74 — Apply stored EQ during playback** via libvlc's equaliser (auto-applied on load; brief toast; each EQU2 point snaps to the nearest libvlc band so untouched bands stay flat).
- ✅ **#79 — RVA2 dB editor** alongside the EQU2 graphic EQ (part of #16).
- ✅ **#56 — Robust ISO8601 dates + timezones.** `datetime_edit` now has a third Timezone section (TAB cycles date→time→tz); opens `timezone_select()` — a full-screen world map picker rendered in Unicode half-blocks (▀▄█) at 72×18 display resolution from a 72×36 land/sea bitmap, with equator (`─`) and prime meridian (`│`) overlaid. 95-entry timezone table covering UTC-12 to UTC+14 (all fractional offsets); left/right arrows cycle offsets, up/down cycle zones within offset, type to search. Returns ISO 8601 offset (`+05:30`, `-08:00`, `Z`).
- ✅ **#59 — More EQ presets.** Expanded from 6 to 24: Rock, Pop, Jazz, Classical, Electronic, Hip-hop, R&B, Acoustic, Dance, Country, Metal, Folk, Latin, Speech, Bass cut, Treble cut, Piano, Night mode.

## Lyrics

- ✅ **#35 — Lyric editor: mouse + duplication.** Mouse enabled (scroll navigates, click positions the cursor on a row); every line is clipped to width so long lines no longer wrap and ghost each frame. *Click-to-row is best-effort by layout — wants a live check.*
- ✅ **#29 — Append writing credits.** `c` key in lyrics editor (SEG mode) appends TCOM (Music by) and TEXT (Words by) credit lines as untimed segments at the bottom of the lyric list.
- ✅ **#47 — (see Playback)** hide Edit Lyrics when there are no lyrics.
- ✅ **#66 — Removed `lyric_timer.py`.** The only used function (`save_sylt_entries`) moved into `lyrics.py`; the dead interactive sync tool deleted; both importers updated.
- ⬜ **#67 — Lyrics-format doc** (an md on how lyrics files should be formatted).
- ⬜ **#69 — More lyric imports.** SYLT from json+uslt or json+md; USLT from txt/rtf/md, etc.

## Layout & rendering

- ✅ **#77 — Lists never wrap at any width.** ANSI-aware clipping in select/checkbox/EQ; the tag list reflows column widths on resize; fixed a per-render file read that made resizing sluggish.
- 🟡 **#32 — Everything looks good at any window size.** Lists no longer wrap (see #77); broader per-screen polish ongoing (see #33, #34, #57).
- ⬜ **#4 — Global margins** for readability on terminals with zero margin — except album art (standard/narrow views) and horizontal rules.
- ⬜ **#30 — Fix boundary window-size duplication.**
- ⬜ **#71 — Album art rendering.** Get `viu` working cleanly with real images (spacing/layout currently breaks); otherwise drop the `viu` dependency and compute half-blocks in-project.

## Settings & power-user options

- ✅ **#2 — Lead-in time UI** prefilled with the current value, validated, clamped ≥0.
- ✅ **#3 — Tidy "Clear History".** Shows the entry count, no-ops when empty, single-line status.
- ✅ **#5 — Settings stay on the current item** after a change (`select(index=)`).
- ✅ **#6 — Clear iTunes XML path** option (only shown when a path is set).
- ✅ **#7 — Sectioned settings** (PLAYBACK / LIBRARY / EDITORS / HISTORY via `separator()`).
- ⬜ **#18 — POST-DEV:** make default options suit a general userbase, not my testing.
- ✅ **#43 — Re-add the "preferred friendly name per tag" setting.** Settings → EDITORS → Tag Name Preferences: all registry tags shown, edit the PREFERRED NAME column via a barrel selector (cycles through registry aliases) or free-text; barrel auto-skips to text mode when there's only one alias or the current value isn't in the list; backspace on empty buffer returns to barrel (2+ aliases only). First column (TAG ID) is locked/read-only. Changes saved per-tag; toast shows count of changed entries. Bug fixed: `handle_settings` was overwriting the saved prefs with its stale config copy — now reloads after the sub-handler returns. Fallback fixed: `get_preferred_tag_name` now falls back to `info.name[0]` (was returning the raw tag ID).
- ⬜ **#62 — Lean into power users.** *AGREED:* expose raw plain-text editing everywhere alongside the smart widget, plus a setting to default to plain-text. Add more default-behaviour settings.
- ⬜ **#64 — Theming** (NOT URGENT). Accent/background colours; option to derive the playback background from the album art.

## Docs & project hygiene

- ✅ **#45 — Pyright/Pylance clean.** `pyright src` reports 0 errors / 0 warnings (was 51); cascade fixed by re-annotating `select() -> Any` plus targeted fixes and library `# type: ignore` markers. Re-run before commits.
- ✅ **#46 — Better non-mp3 error handling.** The tag editor no longer `print()`/`input()`s on failure: untagged MP3s open a fresh ID3 (so tags can be added), non-MP3s show "tag editing is MP3-only", and OS errors show a clean status.
- ⬜ **#48 — Comprehensive READMEs** + a filesystem-advice doc (to maximise auto-detection) + a tag-etiquette doc.
- ⬜ **#65 — Keep this TODO current** (add anything discussed/fixed, with correct marks) — ongoing.
- 🟡 **#70 — Up-to-date docs everywhere** — ongoing. All 16 source modules now have a module docstring; per-function docstring coverage still being filled in.
- ✅ **#72 — Dependencies synced.** `pyperclip` bound aligned across both files; `colorama` is now actually used (`just_fix_windows_console()` in `main`); used imports all match the declared deps.

## Cross-platform

- ⬜ **#73 — Mobile/terminal robustness** (NOT URGENT). Investigate the mobile-terminal segfault; improve general cross-compatibility.

---

## IDEA — pattern-based bulk editing

A richer bulk-edit mode driven by patterns:

- **Periodic dates** — set original release dates on a schedule (e.g. weekly from a start date); support per-range rules (first 8 tracks one date, last 2 another), and per-series times (series 1 at 18:30, series 2 at 11:30).
- **Periodic / ranged text** — set disc subtitles or work names by range (every 6 episodes → `TSST = Series N`; tracks 1–6 → `TIT1 = … Act I`, 7–13 → `Act II`).
- **Movement ↔ disc track-numbering converter** — switch track numbers between album-relative (movement systems) and disc-relative (disc systems).
- **Regex support** throughout.
- **Smart sort orders in bulk** — apply the parsed sort order to each, or a chosen pattern. e.g. `Jeff Goldblum & The Mildred Snitzer Orchestra Feat. dodie` → `Goldblum, Jeff/Mildred Snitzer Orchestra, The/dodie`, with user-verifiable pattern matching and a chosen output pattern.
- **Per-file album art** — when files share a naming pattern relating tracks to covers, auto-apply the file-specific cover.

(Depends partly on the tag/filesystem-etiquette docs, #48.)

---

## Needs a live TUI pass
*(logic verified headlessly; colours/layout/audio want eyes on a real terminal)*

- **#13** — `w` cycle renders in wide + standard; in minimal (<60 cols) the right column isn't drawn, so cycling there only hides lyrics.
- **#21** — volume bar draws in the split view (gutter between art and pane); suppressed only in minimal layouts. Check the gutter spacing.
- **#74** — EQ applied via libvlc; the audible effect needs a real play. If it doesn't take on the first instant, move `set_equalizer` to just after `play()`.
- **#16/#75** — the graphic-EQ widget renders headlessly; check mouse column mapping, curve overlay, presets live.
- **#10** — q-in-editors uses a deferred-quit flag (editor commits → caller saves → next menu unwinds). Confirm the save lands before exit.
- **#35** — lyric-editor mouse: confirm click-to-row lands on the right line (mapped by header height + item rows).
- **#39** — hollow-cursor-on-blur relies on terminal focus reporting (`\033[?1004h`); confirm the cursor goes hollow when the window loses focus (and that supported terminals don't emit stray input).
- **#50** — ≤10% volume silent: deferred until a live audio test.
- **#82** — playback metadata: relaunch the player, press `m`, confirm `year · genre · …` appears (verified headlessly + fixed TYER/frame-buffer bugs this session). If still wrong, add the frame-dump debug.
- **#81** — confirm the transport controls don't shift when pressing `+`/`-`.
- **APIC add (fixed this session):** adding an APIC via "Add Tag" now saves (was a dead `frame_type=='IMAGE'` check; now dispatches on `ui_category=='image'`, and the chosen picture type/description are applied). Worth a quick live confirm that art shows after adding.
