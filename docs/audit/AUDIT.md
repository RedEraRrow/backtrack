# Backtrack — Audit: redundancies, inconsistencies & consolidation

> **What this is.** The findings half of a line-by-line audit of all ~21k lines of Backtrack (companion
> to **[FEATURES.md](FEATURES.md)**). It collects confirmed bugs, dead code, duplication, behavioural
> and stylistic inconsistencies, concurrency/resource concerns, design-principle drift, and
> doc/packaging drift — and ends with a prioritised consolidation roadmap.
>
> **Nothing in the codebase was changed** to produce this; it is analysis only. Every item carries a
> `file:line` anchor and a **confidence** tag:
> - **CONFIRMED** — verified by reading the code (and, where relevant, its callers).
> - **SUSPECTED** — a real concern, but depends on runtime conditions not exercised headlessly.
>
> Audit date: 2026-08-12. **Basis: the working tree, including uncommitted changes** in `menus.py`,
> `music_library.py`, `playback.py`, `playback_ui.py`, `prompt_core.py`, and `ui_utils.py`. Some
> tiny-terminal / geometry edge cases the readers flagged are now **mitigated** by guards added in that
> in-flight work (`_np_box_str`/`now_playing_box_segment`/`_render_status_bar` bail when `rows ≤ 1`;
> `get_status_line`/`_get_breadcrumb_str` guard `cols/width ≤ 1` and clip status overflow to width) —
> noted inline where relevant. The same in-flight work introduces two fresh findings (B2 `_format_queue_row`,
> a new perf note in Chapter E). Line anchors track this working tree and will drift as the code evolves.

## Severity legend

| | Meaning |
|---|---|
| 🔴 **High** | Wrong result, data loss, or a feature that silently doesn't work. |
| 🟠 **Medium** | Degraded behaviour, a platform-specific break, or a latent correctness/concurrency risk. |
| 🟡 **Low** | Minor bug, cosmetic, or edge case. |
| 🔵 **Cleanup** | Dead code, duplication, style — no behavioural impact, but consolidation value. |

---

## Remediation status (updated 2026-08-12)

A remediation pass is underway. All fixes below were verified with `pyright src` at **0 errors /
0 warnings**, `compileall`, full-module import, and targeted headless round-trips.

**✅ Fixed — correctness (Chapters A & D):**
A1 (rename_frame), A2 (URL frames now write + round-trip), A3 (binary frames blocked, not
silently dropped), A5 (.aac reclassified unsupported), A6 (list_edit discard), A7 (crew case), A8
(datetime TAB), A9 (unicode `_norm`), A10 (USLT colon-strip), A11 (`.host.lock` swept), A12
(SIGWINCH guard), A13 (VLC settle-sleep out of the lock), A14 (IPC send timeout), A15 ("And The"
band split), A17 (time_edit feedback), A18 (USLT handoff no-replay), A19 (do_commit guard), A20
(credit segs excluded from transcript), A22 (tick ternary), A25 (multi-digit day entry); D4
(cross-format duration), D5 (history Edit Metadata), D6 (config `.get`). Plus the cosmetic set
(double status, print-to-TUI ×2, ellipsis, PNG alpha, odd-height sliver, degenerate-art crash) and
the pre-existing `prompt_core.py:564` pyright error and the wrong `create_frame` return-type hint.

**🔬 Disproven by verification (NOT bugs — the audit was wrong):**
- **A4** — mutagen's v2.3 save does **not** drop TSOP/TSOA/TSST/TMOO/TDRC; they all round-trip
  intact, so `save_id3`'s cardinality-based version choice is fine. Left unchanged.
- **A16** — the honorific/suffix "misfires" don't occur: for a 2-word name the positional split
  yields the identical inversion, and keeping roman-numeral suffixes usefully leaves "Sammy V"
  **un-inverted** (strip → one word → returned as-is). Reverted; lists unchanged.

**⏸ Deliberately deferred (low return vs. risk on untestable audio / concurrency):**
- **A24** (phantom-track skip loop) — largely self-heals via `tick()`'s VLC-ERROR handling + the
  D4 duration fix; a deeper skip-loop risks the queue logic with no way to test audio headlessly.
- **E** — the SUSPECTED shared-`library` mutation race and `_cache_mtime` check-then-act (GIL makes
  these near-atomic; a real fix needs invasive reader-side locking) and the `list_sessions` probe
  briefly inflating `peer_count` (sub-millisecond window; a fix needs a protocol change).

**⏳ In progress / remaining:** Chapter B dead code, the Chapter C consolidations (ANSI/width,
tag_writer routing, widget driver), Chapter G doc/packaging drift, Chapter H magic numbers, and the
`tz_widget` zoneinfo rework. Findings below are kept as originally written for the record; consult
this status block for what's already resolved.

---

## Chapter A — Confirmed & suspected functional bugs

Ranked by severity. These are places where the software does the wrong thing (or nothing) rather than
merely being untidy.

### A1. 🔴 Tag **rename** is broken in every path — CONFIRMED
`id3_tag_handler.rename_frame` (`id3_tag_handler.py:379-417`) locates the old frame by scanning
`audio_obj.keys()` — but all three callers **pop the frame out first**: `id3_tag_handler.py:951`,
`id3_browser.py:831-832`, `bulk_id3_manager.py:1833-1836`. Because the popped frame is no longer in the
object, the search yields `None` → `return False`. Callers then re-add the original and report failure.
The "Rename tags" feature (single-track *and* bulk) can never succeed. The function was written to
receive a still-attached frame (it pops `old_id` itself at `:413`), contradicting how it's used.

### A2. 🔴 URL frames can never be written — CONFIRMED
`create_frame` (`id3_tag_handler.py:205-356`) has **no branch for `frame_type == 'URL'`**, so WCOM,
WCOP, WOAF, WOAR, WOAS, WORS, WPAY, WPUB and WXXX all fall through to `return None` (`:353`). Yet
`prompt_for_value` gives them a plain text editor (they're `ui_category 'text'`), so a user types a URL,
sees it accepted, and it is **silently discarded on save**. 9 registry entries are non-writable while
appearing editable.

### A3. 🔴 Many BINARY frames are prompt-editable but silently un-writable — CONFIRMED
Beyond the handled APIC/EQU2/RVA2/POPM/PCNT/RBUF, the BINARY frames UFID, MCDI, GEOB, PRIV, AENC, ENCR,
GRID, SIGN, SYTC, ETCO, MLLT, ASPI, POSS, SEEK, COMR, LINK have `ui_category 'text'` and **no
`create_frame` branch** → `return None`. `prompt_for_value` routes them to a text field
(`id3_tag_handler.py:775`), so edits are accepted then dropped on save. (SYLT and RVRB are at least
explicitly blocked; these are not.) The registry advertises editability the writer doesn't honour.

### A4. 🟠 `save_id3` can silently downgrade/drop v2.4-only frames — SUSPECTED
Version selection is driven purely by cardinality: v2.4 only when a multi-value frame exists, else v2.3
(`id3_tag_handler.py:192-202`). A file whose frames are all single-valued but include v2.4-only frames
(TMOO, TDRL, TDTG, TDEN, TDOR, the TSO* sort frames, TIPL/TMCL, RVA2, EQU2, ASPI, SEEK, SIGN…) is saved
as v2.3, and mutagen's downgrade drops/rewrites those frames. The decision should consider
representability, not just value count.

### A5. 🟠 `.aac` treated as MP4-atom family — SUSPECTED (likely CONFIRMED)
`tag_writer._MP4_EXTS` (`tag_writer.py:42`) and `file_namer._MP4_EXTS` (`file_namer.py:87`) include
`.aac`. Raw AAC/ADTS has no MP4 atoms, so `MP4('.aac')` typically raises → `write_fields` returns a
per-file `error` and `read_tokens` returns `{}` — yet `is_writable`/`writable_fields` **advertise `.aac`
as writable**. The preview promises writes that then silently fail per file.

### A6. 🟠 `list_edit` discard prompt is logically inverted — CONFIRMED
`prompt.py:1623`: `result = items if confirm("Discard changes?", default=False) else initial_items`.
Answering **yes, discard** returns the *edited* `items`; declining returns the *original*. Exactly
backwards. (Also: `q`/ENTER return the live-mutated `items`, which aliases the caller's `initial_items`
— edits mutate it in place.)

### A7. 🟠 Crew/cast credit matching never fires — CONFIRMED
`playback_ui.py:692,699-701`: `cast_name_lower` holds lower-cased names but they're compared against
`name.title()` (`if name_l in cast_name_lower`, `.index(name_l)`). Title-case vs lower-case never
matches, so the whole cast-first crew-ordering branch and the `priority_roles` selection are
effectively unreachable, quietly degrading the credits pane. (Still present after the queue-pane
rewrite that shifted `_build_crew_lines` down to `:686`.)

### A8. 🟠 `datetime_edit` TAB dead-ends in the time section — CONFIRMED
The docstring (`prompt.py:1928`) promises TAB from the last time field returns to the date section, but
the code (`:2092-2096`) only cycles time fields 0→3→0 forever. There is no path back to `section='date'`.

### A9. 🟠 Non-ASCII lyrics silently mis-align — CONFIRMED
`lyrics_editor._norm` (`lyrics_editor.py:76`) does `re.sub(r'[^a-z0-9 ]','')` after lowercasing, so
accented/Cyrillic/CJK words normalise to empty and vanish from the alignment stream — directly
contradicting its "single basis for ALL JSON↔MD comparison" docstring and disagreeing with
`lyrics.py._matchable` (`:948`, NFKD-based). Alignment/verify mishandle non-English lyrics.

### A10. 🟠 USLT timing skewed by a blanket colon-strip — CONFIRMED
`lyrics.build_uslt_line_times` (`lyrics.py:167-168`) runs `if ':' in text: text = text.split(':',1)[1]`
on **every** USLT line. Real lyrics contain colons (`"9:00"`, `"Chorus:"`, `"waiting: for you"`), so
word counts — and therefore the whole estimated USLT timeline and highlight positions — are skewed.

### A11. 🟠 Host handoff can dead-lock a session forever — CONFIRMED
`session.attempt_handoff` (`session.py:669`) creates `{session_id}.host.lock` with `O_CREAT|O_EXCL`;
only the winner removes it (`:679`). If the winner crashes during `_become_host_from`, the lock
persists, and `ipc._cleanup` (`ipc.py:132-138`) removes only `.json`+`.sock`, never `.host.lock`. Every
future election for that session then loses forever — the session is unrecoverable. `list_sessions`
never sweeps `.host.lock` either.

### A12. 🟠 `ui_utils` crashes on import on Windows — CONFIRMED
`signal.signal(signal.SIGWINCH, …)` runs unconditionally at `ui_utils.py:22`. `signal.SIGWINCH` doesn't
exist on Windows, so `import ui_utils` raises `AttributeError` there — even though `terminal_input.py`
and `prompt_core.py` keep `_IS_WINDOWS` branches. Either the Windows support is vestigial or the app is
simply broken on Windows.

### A13. 🟠 Track skip freezes the session for 0.3 s — CONFIRMED
`session._load` runs `time.sleep(_VLC_PLAY_SETTLE_S)` (`session.py:227`) **inside `with self._lock`**.
Every load/skip stalls all lock-guarded work — `now_playing`, `seek`, `set_volume`, `tick`, and IPC
command dispatch — for 0.3 s, felt as a freeze in every joined window on each track change.

### A14. 🟠 One stuck IPC client can freeze snapshot delivery to all — SUSPECTED
`ipc._broadcast_loop` calls blocking `sendall` under `_clients_lock` (`ipc.py:249-253`) and accepted
client sockets get no send timeout (`:209-215`). A client whose receive buffer fills blocks the loop
while holding the lock, also stalling `_accept_loop` and `peer_count()`.

### A15. 🟡 `_LIST_SPLIT_RE` splits "And The <band>" despite the comment — CONFIRMED
`id3_browser.py:110-122`: the comment says "and his/her/their/**the**" are protected backing-band
phrases, but the code only excludes `his|her|their`. "Paul Tremaine And The Aristocrats" is wrongly
split into two artists for sort generation.

### A16. 🟡 Honorific/suffix corpora misfire on stage names — CONFIRMED
`_HONORIFICS`/`_NAME_SUFFIXES` (`id3_browser.py:90-102`): `'st'` turns "St Vincent" → "Vincent, St";
`'don'` → "Don Omar" → "Omar, Don"; roman-numeral suffixes `v/vi/vii` mis-strip "Sammy V". Only
"type custom" saves these.

### A17. 🟡 `time_edit` silently ignores ENTER on invalid input — CONFIRMED
`prompt.py:2437`: if `_validate_time()` fails, ENTER does nothing and gives no feedback; `q` (`:2447`)
quits without saving but still sets `QUIT_REQUESTED`. Millis are never validated.

### A18. 🟡 SYLT→USLT handoff replays the whole USLT on no-match — CONFIRMED
`lyrics.find_uslt_handoff_index` (`lyrics.py:806`) returns `0` when the last SYLT line matches no USLT
line, replaying USLT from the top after SYLT ends instead of signalling "no handoff".

### A19. 🟡 `do_commit` reads `w['word']` unguarded — SUSPECTED
`lyrics_editor.py:1592` uses `w['word']` where every other read uses `.get('word','')`; a word dict
missing the key crashes the transcript.json write.

### A20. 🟡 `c` credits append malformed segments — CONFIRMED (leak)
`lyrics_editor.py:2074` appends `{"text": …}` with no `kind`/`start`/`end`; `do_commit` then treats them
as spoken segments and writes "Music by: …" lines into the Whisper transcript.json, and they enter MD
alignment. Credits are for SYLT/USLT display, not transcript source.

### A21. 🟡 Delete-then-fail data-loss window in raw-ID3 bulk ops — SUSPECTED
`apply_sort_orders` (`bulk_id3_manager.py:1215`) and Set-Common-Value (`:1850`) `delall` a frame before
`create_frame`. If the create returns None but another tag in the same file did produce a frame
(`changed=True`), the file is saved with the deleted tag gone and no replacement.

### A22. 🟡 `tick()` return ternary is dead — CONFIRMED
`session.py:451`: `return 'changed' if … else 'changed'` — both branches identical. Either vestigial or
a lost `'repeat'` value.

### A23. 🟡 `_reconcile_library` path membership is string-based — SUSPECTED
`music_library.py:102` tests `p == music_dir or p.startswith(music_dir + os.sep)` without
`realpath`/`normcase`. A trailing separator, symlink, or case-insensitive FS can misclassify entries for
the unmount guard and the removal set.

### A24. 🟡 Deleted/unreadable queued file plays silence, not an error — SUSPECTED
`session._load` swallows ID3 failure into empty ID3 (`:203-207`) and `media_new` on a bad path doesn't
fail synchronously, so `_load` returns True; combined with the `999.0 s` duration fallback (`:230`) the
queue can stall on a phantom track.

### A25. 🟡 `datetime_edit`/`calendar_select` digit-jump gaps — CONFIRMED
`calendar_select` accepts single digits 1–9 to jump to a day (`prompt.py:1907`) but can't reach days
10–31 by typing; `datetime_edit` has no digit-jump. Days 10–31 need arrows.

**Cosmetic / minor UX bugs:** double `show_status` for Toggle Hidden Files (`menus.py:619,621`, the
second overwrites the first); `print()` straight onto the TUI in `system_editor_edit` (`prompt.py:3125`),
`_open_apic_preview` (`id3_browser.py:387`), and `_show_load_error` paths; inconsistent ellipsis `".."`
vs `"…"` in `playback_ui`; odd-height art shows a thin black sliver (`album_art.py:32`); transparent PNG
covers lose alpha (`IMREAD_COLOR`, `album_art.py:14`).

---

## Chapter B — Dead code & unused surface

This codebase carries a substantial amount of dead code — the single largest cleanup opportunity.
**~1,700+ lines** are removable or unwired.

### B1. 🔵 Entire modules / large blocks

| What | Location | Notes |
|---|---|---|
| **`tz_widget.py` — whole module unwired** | `tz_widget.py` (1534 lines) | `timezone_select` has **no caller**; `datetime_edit` strips & discards TZ. Ships as a standalone toy. CONFIRMED. |
| **`search_library`** | `music_library.py:633-682` | Fully dead — superseded by `search.py`; duplicates search logic with a third weight table; does disk I/O. CONFIRMED. |
| **Legacy bulk entry path** | `bulk_id3_manager.py:1872-2065` | `select_files`, `bulk_edit_tags`, `bulk_replace_apic`, module `main_menu`, `__main__` — unreferenced (~195 lines). `bulk_edit_tags` re-implements Set/Rename/Delete divergently. CONFIRMED. |

### B2. 🔵 Dead functions, imports, constants, params (by module)

- **`tag_registry.py`**: `get_official_category` (`:373`), `tags_by_type` (`:385`),
  `tags_by_ui_category` (`:394`), `TagInfo.create_frame` method (`:39`), `TagInfo.description` field
  (`:37`, never populated) — all zero callers. `UICategory` members `timestamp`/`date/time` unused.
- **`id3_tag_handler.py`**: `_EXT_TO_MIME` (`:19-23`, magic-byte sniffing used instead); unused imports
  `TAG_REGISTRY`/`TagInfo`/`get_tag_category` (`:8`); dead `elif tag_id.startswith('SYLT'): return None`
  immediately before the unconditional `return None` (`:350-353`).
- **`id3_browser.py`**: `TAG_REGISTRY` import unused (`:33`); `has_id3` always True (`:688`, non-MP3
  rejected at `:559`) so the `else` branches are unreachable; triple-redundant emptiness check + wasted
  `cv2.imdecode` in `_open_apic_preview` (`:360-372`); superfluous `nonlocal` in `_add` (`:214`).
- **`playback_ui.py`**: `_volume_slider`, `get_ui_state`, `_ART_INNER_MARGIN`,
  `toggle_credits`/`toggle_lyrics`/`toggle_queue` all uncalled; the `toast` parameter is threaded
  through ~9 call sites but never read; `NAV_STACK` import unused (`:12`). **New:** `_format_queue_row`
  (`:674`) — added by the in-flight queue-pane rewrite but never called (`_build_queue_lines` uses
  `_render_queue_row` directly). CONFIRMED dead on arrival. (Line numbers of the older items above
  shifted downward with that rewrite.)
- **`album_art.py`**: `subprocess` imported, unused (`:3`, leftover from the `viu` era).
- **`playback.py`**: `REPEAT_OFF/ONE/ALL` imported, unused (`:42`).
- **`session.py`**: local `import uuid` (`:251`) shadows the module-level import (`:23`).
- **`prompt.py`**: `_toggle_hint()` dead (`:76`); `_build_list_edit_lines` tuple-guard dead (`:1076`,
  `_hint` returns str); unused imports `math`/`textwrap`/`time` (`:18-23`) and several import-only
  symbols (`_render_select_columns`, `_style_checkbox_label`, `_hint_lines`, `_clrline`, `_goto`,
  `_read_key_raw`); `_RATING_STAR_BYTES` unused (`:2835`).
- **`prompt_core.py`**: unused imports `tempfile`, `datetime`, `calendar`, `subprocess`; `_col` (`:197`)
  likely vestigial.
- **`menus.py`**: `SESSION` import unused (`:21`); `_sort_label` defined but never called (`:118`).
- **`src/main.py`**: `SESSION` import inside `_wire_playback` unused (`:145`; shutdown uses `sess.SESSION`).
- **`lyrics.py`**: `check_for_dialogue_files` (`:1076`), `get_dialogue_file_info` (`:1083`) — no callers.
- **`lyrics_editor.py`**: `_parse` (`:163`), `_range` (`:122`), `_TAP_HEADER_MAX_LEN` (`:41`), `_TS_W`
  (`:67`) — defined, never used; redundant local `ID3` re-import (`:2062`).
- **`ui_utils.py`**: `wrap_text` (`:333`) unused.
- **`terminal_input.py`**: the `~` terminator in the assembly loop (`:114`) is never delivered (return
  checks only `ABCD`, `:120`), so `~`-terminated keys (PgUp/Del) are silently swallowed — dead logic.
- **`filename_parser.py`**: `known_artist` is threaded through `parse_one`/`apply_regex`/`apply_template`/
  `_fields_from_groups` but **never read** (only `derive_all:304` uses it) — misleading dead API surface.
- **`config.py`**: `search_weights` (`:13`), `art_width` (`:17`), `player_view` config keys are **never
  read anywhere** — dead configuration surface.
- **`music_library.py`**: `to_num` catches an `IndexError` that cannot occur (`:57`).

---

## Chapter C — Duplication & consolidation opportunities

The dominant structural theme: the same logic re-implemented in several places, often divergently.

### C1. 🟠 ANSI-strip / visible-width — **5+ divergent implementations**
The most pervasive duplication, and it causes real width-math bugs:
1. `ui_utils.strip_ansi`/`visual_len` — `\x1b\[[0-9;]*[mGKFHF]` (`ui_utils.py:287,291`).
2. `prompt_core.py:224,637` — same regex inline.
3. `prompt.py:227,243` — same regex inline (`select._plain` + header width).
4. `playback_ui._ANSI_RE` — full CSI + Kitty/OSC/DCS (`playback_ui.py:24-30`).
5. `lyrics_editor._ANSI_RE` — `\x1b\[[0-9;]*[a-zA-Z]` (`:134`).

The `ui_utils` variant is the weakest: its terminator class **lists `F` twice** and omits common finals,
so it won't strip `\033[?25l/h` (cursor hide/show), `\033[3J`, etc. — `visual_len` over-counts for such
strings. Meanwhile `clip_ansi` in the *same file* (`ui_utils.py:294-324`) parses the full CSI range
correctly, so ui_utils contradicts itself. **Consolidate on one correct scanner** (`clip_ansi`'s / the
`playback_ui` one) and have every measurement path call it. None of these handle wide/CJK/combining
width — see C8. (The in-flight queue-pane rewrite is a step in the right direction — its new code uses
`ui_utils.clip_ansi`/`visual_len`/`strip_ansi` and the shared `prompt_core._table_widths` — but
`playback_ui` still keeps its own `_ANSI_RE`/`_visible_len`/`_clip_ansi_to_width`, so the duplication
stands.)

### C2. 🟠 Raw-ID3 apply scaffolding repeated 4× (bulk manager)
`bulk_people_editor` (`bulk_id3_manager.py:344-364`), `apply_sort_orders` (`:1207-1228`),
`assign_by_pattern` (`:1372-1399`), and the TAGS apply loop (`:1777-1868`) each hand-roll
`ID3(p)/except NoHeaderError → delall → create_frame → save_id3 → refresh_library_entry → broad except`.
This is also the **root cause** of the MP3-only inconsistency (D1) and the delete-then-fail window (A21).
Route them all through `tag_writer` (or one shared helper) to fix all three at once.

### C3. 🟠 Preview/apply scaffolding repeated across every automation op
Each automation op repeats: build `Choice(checked=True)` list → `select(multi=True)` → `if sel is None:
return` → compute `apply_set` → empty-check → `count=errors=0` loop → assemble a message with
`skipped_fmt`/`errors` suffixes (`bulk_id3_manager.py:595-600,663-694,837-842,1129-1136,1197-1235,
1362-1406`). Plus `_plan_write` vs `_detail_view` duplicate the same field-iteration (`:101-144` vs
`:179-205`). A shared "preview → confirm → apply" driver would shrink this 2,065-line file substantially.

### C4. 🟠 Widget redraw-loop scaffolding repeated ~11× (prompt.py)
The `_set_raw / write clear / while consume_resize / _wait_for_keypress(0.05) / _read_key / MODE_TOGGLE
guard / finally restore+clear` block, plus the mouse arm/disarm escape strings
(`\033[?1000h\033[?1006h`), are copy-pasted across `calendar_select`, `datetime_edit`, `fraction_edit`,
`time_edit`, `rva2_edit`, `number_edit`, `rating_edit`, `equaliser_edit`, and the three list widgets.
A shared `_run_widget(render, handle_key)` driver is the prime candidate.

### C5. 🟠 Sort-frame mapping defined 3× (+2 partials)
`_SORT_BASE` (`bulk_id3_manager.py:153`), `_SORT_SRC` (`:614`), and `tag_writer._SORT_MAP` (`:34`) encode
the same TSOP/TSO2/TSOA/TSOT relationship; `id3_browser._SORT_SOURCES` (`:47`) + `_NAME_SORT_TAGS`
(`:55`) mirror it and **drift** (bulk omits TSOC/composer). One canonical mapping.

### C6. 🟠 Key-decode logic duplicated (input layers)
`terminal_input.get_key_non_blocking` (`terminal_input.py:59-140`, non-blocking, raw strings, playback
loop) vs `prompt_core._read_key_raw` (`prompt_core.py:790-871`, blocking, named keys + mouse, widgets)
are two independent POSIX escape-sequence decoders with divergent Windows extended-key maps. Consolidation
is non-trivial (blocking vs non-blocking) but the escape-parsing is genuinely redundant.

### C7. 🟠 Lyrics: 4 normalizers + duplicated timing/air logic
`lyrics_editor._norm`/`_norm_words` (`:71-81`), `lyrics.py._matchable` (`:948`), and two local `_norm`
closures in `lyrics.py` (`:329,797`) all normalise words differently (and A9 is a consequence).
`_spoken_text` (`lyrics_editor.py:90`) overlaps `clean_text_for_timing` (`lyrics.py:300`).
`find_current_uslt_line` and `find_current_dialogue_line` are **byte-identical** (`lyrics.py:190-194`
vs `:589-593`). Air/silence detection lives in three overlapping places (post-pass `is_air`,
`draw_dialogue_window` `show_air`, `_merge_air_beats`), with two thresholds (1.5 s in `lyrics_editor`,
2.0 s in `lyrics`). `_verify_matchup` re-runs `_word_streams` and re-reads the `.md` multiple times per
verify/split (no caching). A shared lyrics-text module (one normalizer, one spoken-text stripper, one
find-current) would remove most of it.

### C8. 🟠 Wide/CJK/combining character width unhandled everywhere
`visual_len`/`clip_ansi`/`truncate_text` (ui_utils), `_render_cell_segments`/`_table_widths`
(prompt_core), `select` label truncation and cursor math (prompt), and all `playback_ui` builders count
**one column per code point**. CJK city names (many in `tz_widget`), emoji, and combining accents
misalign columns, break right-pin gaps, and misplace cursors. No `wcwidth` anywhere. This is one fix
(a shared display-width function) touching many call sites.

### C9. 🔵 Smaller duplications
- **Number parse/format**: `to_num` (`music_library.py:50`, float) vs `_num` (`bulk_pattern.py:14`, int);
  `_fmt_pair` (`tag_writer.py:89`) vs `_num_pair` (`bulk_id3_manager.py:147`); `_split_frac`
  (`file_namer.py:90`) — several hand-rolled inverses of the same `n/total` concept, with inconsistent
  Unicode-slash handling.
- **Extension lists**: `_MP3_EXTS`/`_MP4_EXTS` duplicated verbatim in `tag_writer.py:41-42` and
  `file_namer.py:86-87`; plus `filename_parser._AUDIO_EXTS`, `cover_matcher.IMAGE_EXTS`,
  `music_library.VALID_AUDIO_EXTENSIONS`.
- **MP4 atom maps** re-declared across `tag_writer` (`_mp4_present`/`_set_field`), `file_namer._MP4_TEXT`/
  `_read_mp4`, and `music_library._extract_mp4_metadata`.
- **Text cleaning forked**: `filename_parser._clean_text`, `file_namer._cleanup`/`sanitize`,
  `cover_matcher._norm` — three normalisers in four "naming" modules.
- **Article-move** implemented twice in `id3_browser` (`_sort_single_name:204-207` and
  `_sort_candidates:289-292`).
- **`_clip_ansi`** (`prompt_core.py:682`) duplicates `ui_utils.clip_ansi` with different escape scanning.
- **Legacy `performers` key** popped in two places (`music_library.py:493,159`).

### C10. 🟠 Three tag-dispatch tables must stay hand-synced
Structured-frame knowledge is split across `prompt_for_value` specials, `create_frame` specials, and
`summarize_tag_value` specials (`id3_tag_handler.py:618-640`, `:219-261`, `:828-873`), plus the
`format_spec`→widget strings duplicated inside `_edit_once` vs the `has_widget_toggle` set. Adding one
structured frame means editing all three + the registry. The registry is **not** the single behavioural
source of truth it's meant to be — see F2.

---

## Chapter D — Inconsistencies (behaviour, naming, error handling)

### D1. 🟠 MP3-only vs MP3+MP4 varies by operation — CONFIRMED
Derive / renumber / rename-files / set-art go through `tag_writer` and support MP4; but **all TAGS ops**
plus `apply_sort_orders` and `assign_by_pattern` are hard MP3-only (raw `ID3()`). The *same* logical
action (e.g. writing a sort tag) is cross-format in derive but MP3-only in `apply_sort_orders`
(`bulk_id3_manager.py:1159,1357`). User-visible surprise; fixed for free by C2.

### D2. 🟠 Widget cancel/quit conventions diverge — CONFIRMED
- `confirm` returns bool (never None); CTRL_C → False.
- `select`/`live_select` cancel → None, but `select` maps CTRL_C → break while `live_select` maps it →
  `QuitToTerminal`.
- `time_edit`/`fraction_edit`/`calendar_select`/`datetime_edit`/`equaliser_edit`/`rva2_edit` **don't
  handle CTRL_C at all** — only ESC cancels.
- `q` means "save-then-quit + set `QUIT_REQUESTED`" in value editors but "raise `QuitToTerminal`
  (discard)" in `select`. Two quit semantics under one key.

### D3. 🟠 MODE_TOGGLE (Ctrl-T) support is uneven — PARTLY ADDRESSED
**Advertising** is no longer uneven: `_with_toggle_hint()` puts `^t` in the hint bar of every widget
that answers it, and the `(^t widget)` suffix that only `text()` printed on its message line is gone
(the key belongs in the hint bar). The rest of this finding stands.

`rating_edit`, `rva2_edit`, `equaliser_edit` never check `_MODE_TOGGLE_KEY` (intentional for binary
frames), but the sentinel contract is convention-only — no guard. It works today solely because
`has_widget_toggle` (`id3_tag_handler.py:781`) excludes them. The whole mechanism is a **shared
mutable-global protocol** between two modules (`prompt._value_toggle_enabled/_toggle_carry` set by the
handler) — fragile under any re-entrant widget use.

### D4. 🟠 `get_song_duration` is MP3-only and mixed-typed — CONFIRMED
`music_library.py:210-220` returns `int 0` on failure but a `float` otherwise, and only handles MP3, so
m4a/aac tracks get `0` in playback timing (`session.py`). Inconsistent with `get_metadata`'s
`float(...)` coercion.

### D5. 🟠 `handle_history` omits Edit Metadata — CONFIRMED
`menus.py:425-436` offers only Play / Edit Lyrics and ignores `show_metadata_editor`, unlike
`handle_search` (`:289-293`) and `browse_menu` (`:1108-1113`). Sibling-handler drift.

### D6. 🟡 Config read style inconsistent — CONFIRMED
`handle_settings` reads `config["history_enabled"]`/`config["lyric_lead_in"]` by direct key
(`menus.py:539-540,557`) while every other toggle uses `.get(key, default)`. Safe only because
`load_config` backfills; a latent `KeyError` for any non-backfilled dict.

### D7. 🟡 Search weight tables disagree — CONFIRMED
`config.py:13` (artist 8, never read), `search.py:10` (artist 7, the live one), and dead `search_library`
(artist 8) hold three different weight sets. The config key is ignored entirely.

### D8. 🟡 Broad `except Exception` is the house style — CONFIRMED (pervasive)
Dozens of `except Exception: pass`/`→ status string` blocks swallow errors: e.g. `music_library`
get_metadata (`:241,260`), `id3_tag_handler` (`:35,375,1009`), `bulk_id3_manager` refresh guards
(`:592,686,834,1124,1225,1395,1864`), `ipc.py` (`:190,229,244,324,330`), `session.py` (many),
`prompt_core` render (`:157,165,184`), `prompt` `live_select._recompute` (`:548`), `ui_utils`
(`:138,155`). Several sit **next to** narrower catches in the same function (e.g. `get_metadata`'s MP4
branch is `(MutagenError, OSError)` while the ID3 branch is bare `Exception`). These hide genuine bugs;
a written file can be silently stale in the library with no signal.

### D9. 🟡 Smaller inconsistencies
- Ellipsis `".."` (cast/crew) vs `"…"` (meta/queue) in `playback_ui`.
- Air-gap threshold 1.5 s (`lyrics_editor:908`) vs 2.0 s (`lyrics:304`).
- Cast pane shows `limit*2` entries, crew only `limit` (`playback_ui:213` vs `:532`).
- `rva2_edit` uses a fixed 20-char separator (`prompt.py:2644`) while every other widget uses terminal
  width.
- `Column` style docstring lists 5 styles but `_style_cell` also accepts an undocumented `'dim'`
  (`prompt_core.py:418`) and silently renders unknown styles plain (`:426`).
- `present_fields`/`_*_present` leak an extra `compilation` key not in `FIELDS` on success paths but not
  error paths (`tag_writer.py:117,143` vs `:163,168`).

---

## Chapter E — Concurrency, resources & robustness

- **🟠 Unlocked mutation of the shared `library` list — SUSPECTED.** `_sync_worker` does
  `library[:] = …`, `.append`, `track.update` (`music_library.py:109,117,159,173`) with no lock while
  the UI thread reads/sorts/searches it. `_sync_lock` only guards thread startup. Concurrent iteration
  during a `library[:] =`/`.append` can raise `RuntimeError: list changed size` or yield torn reads.
- **🟠 `_cache_mtime` is a lock-free global written from multiple threads** (`music_library.py:43,124,
  169,465,483,491,496`) — the external-change check can race, causing a spurious reload or a missed one.
- **🟠 `_load` sleeps under the RLock** — see A13.
- **🟠 IPC broadcast blocks under lock, no send timeout** — see A14.
- **🟠 `list_sessions` probes inflate `peer_count` → spurious "player pinned"** — SUSPECTED.
  `_connectable` opens a real connection the host briefly counts as a peer (`ipc.py:91-103`), so
  `has_other_windows()` can momentarily report another window and pin the player view.
- **🟠 Unix socket has no explicit permissions** — SUSPECTED. `srv.bind` (`ipc.py:176`) relies on umask;
  no `chmod 0600`. On a shared host another local user could connect and drive playback / read
  now-playing.
- **🟡 Client volume/seek read-modify-write races on lagged snapshots** — `remote.set_volume(get_volume()+5)`
  reads the ~0.25 s-old snapshot (`playback.py:210-213`), so rapid presses lose increments.
- **🟡 `cv2.resize` can throw on degenerate art** — SUSPECTED. A very wide source at small width can
  round `height` to 0 → `cv2.error` propagates out of an un-`try`'d render path (`album_art.py:20-22`).
- **🟡 Unbounded caches** — the per-session art cache grows for every distinct track
  (`playback_ui.py:114`); `_expand_cache` keys on `id(lines)` which Python can reuse (`lyrics.py:124`).
- **🟡 Queue pane re-parses every queued file's tags on the render path — SUSPECTED (new).** The
  in-flight queue rewrite's `_queue_metadata` (`playback_ui.py:450-471`) calls `get_metadata(path)` — a
  **full ID3/MP4 tag read** — for **every** queued track each time `set_queue_context` runs, and that's
  called on every track change and every queue change (`playback.py:299,410`). For a long queue this is
  N full-file parses per queue event, unbounded and off the library cache (it re-reads rather than
  reusing the already-scanned library dicts). A broad `except Exception: pass` (`:467`) hides read
  failures. Prefer the in-memory library metadata.
- **🔵 Import-time side effects** — `CACHE_DIR.mkdir(...)` runs at `music_library` import (`:37`) — the
  config module avoids this; `signal.signal(SIGWINCH)` at `ui_utils` import (A12); colorama init at
  `src/__init__` import.
- **🔵 First-use cost of `tz_widget`** — `_get_world_bitmap` builds ~4.1M Python ints in a double loop on
  first render (`tz_widget.py:853-861`) — a visible hitch and large RSS (moot while unwired).
- **🔵 `shutdown` never joins the tick thread** (`session.py:330`) — harmless (daemon) but stderr restore
  can race a final `_load`.

---

## Chapter F — Design-principle adherence

### F1. 🟠 "Pure core, thin UI" — partial
- **Genuinely pure:** `filename_parser` (string-only), `search`, `bulk_pattern` (`os.path.basename` is a
  string op). Good.
- **Overstated purity docstrings:** `file_namer` ("Pure and unit-testable", `:6`) does real read I/O
  (`_read_id3`/`_read_mp4`, `plan_renames`'s `os.listdir`); `cover_matcher` ("Pure… no writes", `:25`)
  reads I/O (`find_images`, `read_image`). Neither prints, mutates globals, or touches UI — the violation
  is the wording, but it's misleading. (CONFIRMED.)
- **Logic baked into UI:** `bulk_id3_manager`'s TAGS ops / `apply_sort_orders` / `assign_by_pattern` bake
  ID3 mutation into prompt functions instead of `tag_writer` (root cause of A21/D1). `lyrics_editor` is a
  ~1,040-line function mixing render, VLC, mouse, and file I/O. `menus.py` holds pure-data helpers
  (`_sort_groups`, `_year_of`, `_disc_track_cell`). The sort engine's `_sort_candidates` does a hidden
  `load_config()` I/O read (`id3_browser.py:279`) inside an otherwise-pure function. `music_library`
  reaches into `ui_utils`/`show_status` (acceptable but non-headless-testable).

### F2. 🟠 "One source of truth for tags" — not upheld in practice
The registry drives *lookup*, but *behaviour* is duplicated across three dispatch tables (C10), and the
sort-frame mapping across three copies (C5). The registry also **over-promises**: it advertises URL and
most-BINARY frames as editable, but the create/rename path can't honour them (A2/A3). `create_frame`'s
return type hint (`id3_tag_handler.py:205`) is both over- and under-stated (claims SYLT, which it never
returns; omits the dozens of text classes it does).

### F3. 🟢 "Cross-format writing, report what you can't do" — mostly upheld
`tag_writer` reports `unsupported`/`skipped_format` honestly. **One silent gap:** MP4 `disc_subtitle` is
`continue`d without any record (`tag_writer.py:202-203`); mitigated only because `bulk_id3_manager`
pre-filters via `writable_fields`. **One data-loss default:** MP3 `write_cover` `delall('APIC')` wipes
back/booklet/artist covers even on overwrite (`:354-355`).

### F4. 🟢 "Pure heuristics, no name corpus" (sort engine) — accurate
Verified: `id3_browser`'s sort engine contains no person-name list, only grammatical pattern sets
(articles/honorifics/suffixes/prefixes). The docs' claim holds. The one nuance is the hidden `load_config`
read noted in F1.

---

## Chapter G — Documentation & packaging drift

- **🟠 README playback keys are stale.** README:141 says `b` = "Back (stop and return)", but with #14
  `b`/Esc now **minimise and keep playing** and stop moved to `s`. `s`, `p`/`P`, and `e` are undocumented
  in the controls table. (CONFIRMED — matches TODO #14 but not README.)
- **🟡 `pyproject.toml` classifiers cap at Python 3.11** (`:16-26`) while `requires-python = ">=3.8"` and
  the README/DEVELOPER say "developed against 3.13/3.14" (and the runtime is 3.14). Classifiers are stale;
  the version floor and reality also disagree.
- **🟡 `main.py` docstring still says "Music Browser Application"** (`main.py:2`) — the project's old name.
- **🟡 Dual colorama init on Windows** — `src/__init__.py:11` (`colorama.init()`) *and* `src/main.py:176`
  (`just_fix_windows_console()`) both run. Redundant.
- **🟡 `tz_widget` header comment is stale** — `:431-432` describes a "72×36, 5°/pixel" map; the actual
  data is 2880×1440 at 0.125° (`:841-842`).
- **🟡 `_find_markdown_for_audio` docstring lists 2 patterns; the code searches 3** (`lyrics.py:809-816`).
- **🟡 `prompt.py` module docstring lists 5 widgets** though the module exports ~15 (`:4-9`).
- **🔵 Stale `egg-info/SOURCES.txt`** — lists only 7 of the src files (missing `id3/`, `lyrics/`,
  `playback/`, `art/`, `utils/`). It is a **local, gitignored** build artifact (`*.egg-info/` in
  `.gitignore`), so not a repo concern, but it's out of date on disk.
- **🔵 `tz_widget` data errors** (feed the map/search if ever wired): "Palau"→`Asia/Hong_Kong` at Hong
  Kong's coords (`:389`), "Funafuti"→`Pacific/Apia` at Samoa's coords (`:417`), "São Tomé"→`Africa/
  Libreville` (`:218`), a Yekaterinburg/Ekaterinburg duplicate at +3 vs +5 (`:263`/`:314`), plus other
  Russia offset oddities. Hand-typed 365-row table vs a library source.

---

## Chapter H — Questioned decisions & magic numbers

- **🟠 `tz_widget` reinvents timezone data without DST.** ~530 KB + ~1 MB embedded rasters and a 365-row
  table, none DST-aware (offsets are static ints; `abbr` fakes DST as text). On Python 3.14, stdlib
  `zoneinfo` + a small city list would replace both rasters *and* fix the inaccuracy — **if** the widget
  is wired into `datetime_edit` at all (it currently isn't). Decision to question: keep, wire-in, or drop.
- **🟡 `score_match` floor false-positives on short titles** — a single shared word on a 2-word title =
  150 = exactly `MATCH_FLOOR`, with no stop-word filter (`cover_matcher.py:207`).
- **🟡 POPM boundary asymmetry** — write bytes `(0,1,64,128,196,255)` aren't the midpoints of the read
  boundaries `(32,96,160,224)`; a byte written by another app near a boundary (e.g. 100) re-saves as 128,
  silently shifting the stored value (`id3_tag_handler.py:441-466`). `196` (not 192) is a WMP-ism.
- **🟡 `set_album_art_op` hardcodes `overwrite=True`** (`bulk_id3_manager.py:1115`) — "Fill blanks only" is
  enforced only by the default checkbox state, unlike derive/sort/assign where it's a hard flag.
- **🟡 `_parse_date` DD/MM-vs-MM/DD heuristic** resolves ambiguous dates US-style with no locale awareness
  (`prompt.py:1694`); magic `1900` bound applied inconsistently.
- **Magic-number soup (🔵):** duration fallback `999.0`, seek margin `0.5`, EQ clamp `±20`, settle `0.3`,
  tick `0.1`, broadcast `0.25`, connect `0.4`, backlog `8`, handoff `25×0.1`, recv `4096`, `[:8]` id
  slice (session/ipc); loop `0.02`, key-poll `0.05`, resize debounce `0.15`, revert `4.0`, toasts
  `2.5/2.0/1.0`, `e`=`35 s` (playback); search tuning `30/60`, `1000` band, `0.15` ratio, `20×0.05`
  boost, `1.15` recency (search); pane estimates `credits_est=5`/`lyrics_est=6` and row fudge
  `-5/-6/-4/+2` (playback_ui); `2.2` wps / `0.5` min / `80`-word window / `5000 ms` fabricated end
  (lyrics). Almost none centralised.
- **🟡 `format_time` treats a month as 30 d / year as 365 d** (`ui_utils.py:360`) — odd for media
  durations, though rarely reached.

---

## Chapter I — Prioritised consolidation roadmap

A suggested order of attack, weighing impact against effort. (Recommendations only — nothing here was
applied.)

**Tier 1 — correctness (fix the silent failures)**
1. **Fix `rename_frame`** (A1) — hand it a still-attached frame, or make it accept a popped frame; touches
   all three callers.
2. **Handle URL + creatable BINARY frames, or block them in the UI** (A2, A3) — stop advertising
   editability the writer can't honour; align the registry contract (F2).
3. **Fix the `list_edit` inverted discard** (A6) and **`datetime_edit` TAB** (A8).
4. **Gate `signal.SIGWINCH` behind `_IS_WINDOWS`** (A12) — one line; unblocks Windows import (or drop
   Windows support deliberately).
5. **Version-select on representability, not cardinality** (A4) — avoid dropping v2.4-only frames.
6. **Fix the crew/cast case bug** (A7) and the **`.aac` mis-classification** (A5).

**Tier 2 — delete dead code (~1,700 lines, low risk)**
7. Decide `tz_widget`'s fate (wire into `datetime_edit`, replace with `zoneinfo`, or delete — B1/H).
8. Remove `search_library`, the legacy bulk entry path, and the per-module dead functions/imports/params
   in Chapter B. Remove the unused config keys or wire them up.

**Tier 3 — de-duplicate (the structural wins)**
9. **One ANSI/display-width module** (C1 + C8) — single scanner + `wcwidth`; replaces 5 implementations
   and fixes wide-char alignment everywhere.
10. **Route all bulk writes through `tag_writer`** (C2) — collapses the 4× raw-ID3 scaffolding, fixes the
    MP3-only inconsistency (D1) and the delete-then-fail window (A21) together.
11. **A shared widget event-loop driver** (C4) and a **shared preview→apply driver** (C3) — shrink
    `prompt.py` and `bulk_id3_manager.py` markedly.
12. **One sort-frame mapping** (C5), **one number parse/format helper** (C9), **one lyrics-text module**
    (C7 — also fixes A9/A10 indirectly), and **one ext-list/atom-map source** (C9).

**Tier 4 — concurrency & polish**
13. Guard the shared `library` mutation and `_cache_mtime` with the existing lock (E); move the VLC
    settle-sleep out of the lock (A13); add a send timeout to IPC broadcast (A14) and `chmod 0600` the
    socket (E); sweep `.host.lock` in `_cleanup`/`list_sessions` (A11).
14. Reconcile the docs: README keys, classifiers, `main.py` name, dual colorama, and the stale docstrings
    (Chapter G). Centralise the magic numbers (H).

---

*End of audit. See [FEATURES.md](FEATURES.md) for the full feature catalogue these findings sit against.*
