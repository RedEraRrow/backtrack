# PR: background playback, column layout, tag editors, multi-disc art

Four changes in this branch, plus doc rewrites.

## Multi-window / background playback (#14)

Process-wide audio session decoupled from the view; keeps playing when minimised and shared across windows over a local socket.

- **New `src/playback/session.py`** — `PlaybackSession` (singleton `SESSION`) owns VLC/track/queue; background thread auto-advances and logs history. `RemoteSession` proxies to a remote host; host hand-off, view lock, repeat modes. Audio primitives moved here from `playback.py`.
- **New `src/playback/ipc.py`** — Unix-socket IPC: `SessionServer`, `SessionClient`, `list_sessions()`; state under `CONFIG_DIR/sessions/`.
- **`playback.py`** — reduced to a view over `SESSION`. `music_player()` gains `queue_paths`/`mode`; new `open_player_view`/`open_client_player_view`/`_player_view_loop`. `b`/`Esc` minimise (keep playing); only `s` stops.
- **`main.py`** — `_maybe_join_session` (join via IPC or start new), `_wire_playback` (now-playing bar, Ctrl-P, beacon→notifications); session shutdown on exit.
- **`menus.py`** — `play_queue` starts the shared session instead of a local loop; queue row-actions (`n`=play next, `a`=queue); `notification_centre()` activity panel.
- **`prompt.py`** — `select()` gains `row_actions`/`row_action_hints`; Ctrl-P player hotkey; status-bar ● beacon click.
- **`prompt_core.py` / `ui_utils.py`** — now-playing box rendered with the status bar, ~4 Hz refresh via self-pipe wake, animated beacon (`pulse_circle`); `_visible_rows` reserves box height.
- **`playback_ui.py`** — `format_now_playing_bar()` (box border = progress bar); `_clip_ansi_to_width` CSI-scan fix (leaked `7m`/`2m`).
- **`terminal_input.py`** — lone `Esc` emitted immediately, no longer dropped.

## Columned-menu layout rewrite

- **`prompt_core.py`** — `_table_widths` rewritten: content sizing across all rows, flex columns share surplus (capped by `max_frac`/`max_width`), over-budget shaves widest to a floor, reserves pinned-gap. New `Column.priority` → narrow-terminal **column dropping** (lowest dropped first; `None` = never); `_render_table_row` skips dropped columns and gaps. New `_MIN_COL_FLOOR`, `_MIN_PIN_GAP`.
- Drop priorities added to specs in `menus.py`, `bulk_id3_manager.py`, `id3_browser.py`, `id3_tag_handler.py`.

## Tag editing

- **`tag_registry.py`** — TCMP, TKEY, TMED marked editable.
- **`id3_tag_handler.py`** — editors for TKEY, TMED, TSRC (validated), TCMP (1/0), RBUF; RVRB read-only. `create_frame`/`summarize_tag_value` handle RBUF/TCMP.

## Multi-disc cover art

- **`cover_matcher.py`** — detects per-disc subfolders (CD1/Disc 2/DVD1), scans parent album folder for a shared cover; assigns the sole album image to all tracks when no per-track pairing exists.
- **`bulk_id3_manager.py`** — `set_album_art_op` adds "this track / all N tracks" scope prompt.

## Docs

- **`README.md`** rewritten; `viu` dependency removed (art now in-project OpenCV/NumPy).
- **`DEVELOPER.md`** rewritten (annotated tree, data-locations table, design principles).
- New **`filesystem-etiquette.md`**, **`tag-etiquette.md`**; `library-layout.md` cross-links them.
- **`TODO.md`** — #48/#16/#50 done; #14 plan documented.
