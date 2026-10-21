"""The player screen: its layout (the art and details, the panels in their
places), the details lines and the people. Beside it: the panels' registry and
arrangement (panel_state), the transport (player_controls), the frame painter
(player_frame) and the state they share (player_state)."""
from __future__ import annotations
import re
import time
import sys

from backtrack.music_library import (chapter_at, chapter_label,
                                    format_value_list, track_title, first_text)
from backbone.prompt import core as pc
from backbone import nav
from backbone import keys, ui
from backtrack.playback.player_geom import geom
from backbone import numbering
from backbone.prompt.core import _hint
from backbone.prompt.core import add_hint_click_cells
from backbone.ui import Colors as C
from backtrack import tuning as tune
from backtrack.playback import queue_pane
from backtrack.playback.queue_pane import (
    queue_title,  # noqa: F401 (re-exported)
    _place_queue, _queue_area, _queue_click_rows, in_queue, move_queue_cursor, queue_click_index, queue_cursor,
    set_chapter_context, set_queue_context, set_queue_cursor, use_list,
)
from backtrack.playback.player_art import (  # noqa: F401 (re-exported)
    ART_MAX_WIDTH, _art_width_for_height, _inline_art, art_image_incomplete, redraw_art_image, set_resizing,
    boxed_art, cover_box,
)
from backtrack.config import save_setting, setting
from backtrack.playback.player_state import _frame, _ui_state
from backtrack.playback.player_frame import _frame_borders, _render_frame_buffer  # noqa: F401 (re-exported)
from backtrack.playback.player_controls import (  # noqa: F401 (re-exported)
    _TINY_COLS, _TINY_ROWS, _controls_line, _draw_tiny, _icons_fitting, _last_hint_cells,
    _last_transport_cols, _place_controls, compute_controls_hint_cells, hint_click_key,
    progress_from_click, shown_time, step_volume, time_clicked, timer_extra, toggle_chapter_time,
    transport_click_action, update_progress_ui, write_controls,
)
from backtrack.playback.panel_state import (  # noqa: F401 (re-exported)
    PANELS, PLACES, _ARRANGE_FIRST, _LIVE_PANELS, _track_ctx, arrange_key, arrange_start,
    chapters_listed, cycle_visualiser, live_context, lyrics_laid_out, page_click, panel_notes, people_of,
    set_panels, set_track_context, shown_panels, track_has, turn_page,
)
from backtrack.music_library import year_of


# The player's keys, shared by the host player and a joined window's view.
keys.define("player", "Player", [
    ("playpause", ("SPACE", "p", "P"), "play / pause"),
    ("back_5", ("LEFT",), "back 5s"),
    ("fwd_5", ("RIGHT",), "forward 5s"),
    ("back_1", ("j",), "back 1s"),
    ("fwd_1", ("l",), "forward 1s"),
    ("back_30", (",",), "back 30s"),
    ("fwd_30", (".",), "forward 30s"),
    ("near_end", ("E",), "jump to near the end, with Diagnostics on"),
    ("edit", ("e",), "edit this track's tags"),
    ("album", ("a",), "this track's album, in Browse"),
    ("artist", ("A",), "this track's artist, in Browse"),
    ("prev", ("[",), "previous chapter or track"),
    ("next", ("]",), "next chapter or track"),
    ("slower", ("<",), "slower, for audiobooks"),
    ("faster", (">",), "faster, for audiobooks"),
    ("sleep", ("z", "Z"), "sleep timer"),
    ("chapter_time", ("t", "T"), "chapter or whole-file time"),
    ("meta", ("m", "M"), "show or hide the details"),
    ("tabs", ("f", "F"), "show or hide the tab bar"),
    ("resume", ("r", "R"), "resume the last run's queue, with nothing playing"),
    ("panel", ("w", "W"), "choose and arrange the panels"),
    ("visualiser", ("v", "V"), "the visualiser's next style"),
    ("page_prev", ("{",), "previous booklet page"),
    ("page_next", ("}",), "next booklet page"),
    ("back", ("b", "B", "ESC"), "back, keep playing"),
    ("stop", ("s", "S"), "stop"),
    ("quit", ("q", "Q"), "quit the app"),
])
# While the queue panel is showing (w): a cursor through the queue, and edits at it.
keys.define("player_queue", "Player: queue panel", [
    ("up", ("UP",), "cursor up the queue"),
    ("down", ("DOWN",), "cursor down the queue"),
    ("play", ("ENTER",), "play the track at the cursor"),
    ("move_up", ("J",), "move it up"),
    ("move_down", ("K",), "move it down"),
    ("remove", ("d", "DELETE", "BACKSPACE"), "remove it"),
    ("shuffle", ("x",), "shuffle what's coming"),
    ("clear", ("c",), "clear what's coming"),
    ("undo", ("u",), "undo the last queue change"),
], within=("player", "global"))



_WIDE_SPLIT_GUTTER = 3
# Share of the width left blank before the lyrics in the standard layout, so
# they float under the controls rather than hug the edge.
_LYRICS_INSET_FRAC = 0.2
_PANEL_SIDE_MIN = 24            # a column of panels beside (or under) the player keeps at least this many columns
_PANEL_WANT = {'lyrics': 56, 'queue': 44, 'chapters': 40, 'people': 40,   # what each shares spare width by
               'visualiser': 48, 'credits': 40, 'info': 44, 'notes': 48,
               'booklet': 48}
_PANEL_ROWS_WEIGHT = {'lyrics': 3, 'queue': 2, 'chapters': 2,               # and spare rows, stacked in a column
                      'visualiser': 2, 'credits': 2, 'info': 2, 'notes': 2,
                      'booklet': 3}
_MID_MIN_TEXT = 20              # the player's box shrinks for the panels no narrower than this inside
_SIDE_ART_SHARE = 0.75          # panels beside the player unless its art there is smaller than this share of under
_PLAYER_SHARE = 0.5             # beside the panels, the player's box takes at most this share
_PLAYER_SHARE_TWO = 0.4         # and between a column of them each side
_PLAYER_MIN_W = 28              # its inside with no art (no room for it)
_BOX_MIN_BODY = 3               # rows over the transport box a box needs (its borders and a line)
_BESIDE_TEXT = 24               # the details beside the art (a short, wide window) keep this many columns
_DETAILS_W = 40                 # beside the panels, the player's box keeps this for the details, whatever the track's
_DETAILS_MAX_W = 60             # the details beside the art get at most this
_BESIDE_GAIN = 1.3              # the art goes beside the details only when that makes it this much bigger
_PANEL_MIN_ROWS = 8             # under the player, the lyrics or the queue keep at least this
_BAND_ROWS = 8                  # a full-width band (or a panel above or below the art) of a panel that isn't fitted
_MID_MIN_H = 10                 # the bands leave the player's row at least this




PLAYER_CREDITS_ROLES = [
    'performer',
    'various',
    'cast',
    'main cast',
    'guest',
    'starring',
    'featuring',
    'ensemble',
    'ensemble cast',
    'ensemble actor',
]

_CREW_ORDER = ['creator', 'writer', 'producer', 'director', 'script editor', 'composer']


# Below this width the single column shows no art: too small to be worth it.
_MIN_ART_COLS = 34


def _layout_mode(cols: int) -> str:
    """Classify terminal width into a layout mode: wide (art beside a pane) or
    standard (one column)."""
    return 'wide' if cols >= 120 else 'standard'




def set_chapter(chapters: list, index: int) -> None:
    """The chapter now playing, for the title and details lines and the chapters panel."""
    set_chapter_context(chapters, index)
    title, pos = chapter_label(chapters, index) if 0 <= index < len(chapters) else ('', '')
    _ui_state['chapter_title'], _ui_state['chapter_pos'] = title, pos



def _build_cast_lines(people: list[tuple[str, str]], max_w: int, limit: int = 4) -> list[str]:
    """Build display lines for cast/performer credits, truncating long labels and summarizing overflow past the limit."""
    total = len(people)
    cap = limit * 2
    lines = []

    for i, (role, name) in enumerate(people[:cap]):
        is_named = role.lower() not in PLAYER_CREDITS_ROLES
        label = f"{role}: {name}" if is_named else name
        if len(label) > max_w - 3:
            label = label[:max_w - 4] + "…"
        lines.append(f" {label}")

    if total > cap:
        lines.append(f"{C.DIM} + {total - cap} more…{C.RESET}")

    return lines








def toggle_metadata() -> None:
    """Show or hide the track details line, and remember the choice."""
    _ui_state['show_metadata'] = not _ui_state['show_metadata']
    save_setting('player_show_metadata', _ui_state['show_metadata'])
    refresh_player_settings()

def toggle_tabs() -> None:
    """Show or hide the tab bar over the player, and remember the choice."""
    _ui_state['show_tabs'] = not _ui_state['show_tabs']
    save_setting('player_show_tabs', _ui_state['show_tabs'])
    ui.set_tabs_hidden(not _ui_state['show_tabs'])


def view_chrome(open_: bool) -> None:
    """The app's chrome while a player view is open: the tab bar as `f` last
    left it."""
    if not open_:
        pc.screen_release()                  # the lyrics' cells are every screen's again
        pc.screen_picture_area('player art', None)
        if _inline_art['path'] and geom.art_top and geom.art_width:
            # The cover image: the next screen writes over every cell of it,
            # whatever the painter thinks is there, so none of it is left.
            pc.screen_forget_cells(geom.art_top, geom.art_top + geom.art_height - 1,
                                   geom.art_left + 1, geom.art_left + geom.art_width)
    if open_:
        refresh_player_settings()
    ui.set_tabs_hidden(open_ and not _ui_state['show_tabs'])


def toggle_help() -> None:
    """Show or hide the hint bar: the app-wide switch, so every screen follows."""
    pc.toggle_hints()


_lyric_src: dict = {'track': '', 'files': [], 'estimated': False}


def refresh_player_settings() -> None:
    """Re-read the player's settings (`debug`, `player_show_metadata`,
    `player_nerd_font_icons`) into `_ui_state`.

    Called when a track loads and when the metadata panel is toggled (both rare,
    both moments where the answer could have changed) rather than on every draw,
    which would re-read the file for a value that almost never moves.
    """
    try:
        from backtrack.config import load_config
        cfg = load_config()
        _ui_state['debug'] = bool(setting(cfg, 'debug'))
        _ui_state['show_metadata'] = bool(setting(cfg, 'player_show_metadata'))
        _ui_state['show_tabs'] = bool(setting(cfg, 'player_show_tabs'))
        _ui_state['nerd_icons'] = bool(setting(cfg, 'player_nerd_font_icons'))
        _ui_state['people_all'] = bool(setting(cfg, 'player_people_all_credits'))
        _ui_state['visualiser'] = setting(cfg, 'player_visualiser')
        saved = cfg.get('player_panels') or {}
        _ui_state['panels'] = {shape: [[k, side] for k, side in (saved.get(shape) or [])
                                       if k in dict(PANELS) and side in PLACES]
                               for shape in ('wide', 'tall')}
        kinds = shown_panels()
        _ui_state['show_lyrics'] = 'lyrics' in kinds
        _ui_state['show_credits'] = 'people' in kinds
        _ui_state['show_queue'] = 'queue' in kinds or 'chapters' in kinds
        use_list('chapters' if chapters_listed() else 'queue')
    except Exception:
        _ui_state['debug'] = False


def set_lyric_sources(track: str, files: list[str], estimated: bool = False) -> None:
    """Register the files the lyric layer actually opened for this track.

    Set by the player from what the load RESOLVED, never from what it hoped to
    find, so the 'm' panel names the document being read rather than the one that
    ought to be there: the difference between the two is the bug class this
    exists to make visible. The panel only shows it with Diagnostics on.

    Stamped with the track it belongs to, and the panel only shows it for that
    track. Naming another episode's files would be worse than naming none, and a
    module-level record outlives the track that set it.
    """
    refresh_player_settings()
    _lyric_src['track'] = track or ''
    _lyric_src['files'] = [f for f in (files or []) if f]
    _lyric_src['estimated'] = estimated



def _build_crew_lines(people: list[tuple[str, str]], max_w: int,
                     cast_names: list[str] | None = None,
                     limit: int = 4) -> list[str]:
    """Build production-team credit lines: names matching cast_names surface first
    (in cast order), then priority roles, then the rest, truncated to limit with an overflow summary."""
    cast_names = cast_names or []
    cast_name_lower = [n.lower() for n in cast_names]

    cast_matches = []
    others = []
    seen_cast = set()

    for role, name in people:
        name_l = name.lower()
        if name_l in cast_name_lower and name_l not in seen_cast:
            cast_matches.append((cast_name_lower.index(name_l), role, name))
            seen_cast.add(name_l)
        else:
            others.append((role, name))

    cast_matches.sort(key=lambda x: x[0])
    cast_matches = [(role, name) for _, role, name in cast_matches]

    priority_roles = ['creator', 'producer', 'script editor']
    if not cast_matches:
        priority_roles = ['creator', 'producer', 'writer', 'script editor']

    ordered = []
    extra = []
    for role, name in others:
        if role.lower() in priority_roles:
            ordered.append((role, name))
        else:
            extra.append((role, name))

    ordered.sort(key=lambda x: _CREW_ORDER.index(x[0].lower()) if x[0].lower() in _CREW_ORDER else 99)
    combined = cast_matches + ordered + extra
    total = len(combined)
    lines = []

    for role, name in combined[:limit]:
        label = f"{role}: {name}"
        if len(label) > max_w - 3:
            label = label[:max_w - 4] + "…"
        lines.append(f" {label}")

    if total > limit:
        lines.append(f"{C.DIM} + {total - limit} more…{C.RESET}")

    return lines



def _movement_roman(s: str) -> str:
    """A movement number as a Roman numeral, the convention for classical
    movements. Non-numeric (or unnumbered) values pass through unchanged.
    """
    return numbering.roman(s) or s


def _credits_rule(width: int) -> str:
    """The divider between the credits and the lyrics under them."""
    return f"{C.FRAME}{'─' * max(0, width)}{C.RESET}"


def _meta_left_lines(audio, file_path: str, max_val_w: int) -> list[str]:
    """Build the left-column metadata lines (title, artist/album, and optional
    extras like year/genre/track/disc) shown beside the album art: always as
    many rows as the settings allow (blanks for what this track lacks), so the
    art keeps its size and place from track to track."""
    def _trim(text: str) -> str:
        """Truncate text to max_val_w with an ellipsis."""
        return ui.truncate_text(text, max(1, max_val_w), placeholder='…')

    def _scroll(text: str) -> str:
        """Fit text to max_val_w by scrolling through it (ui.marquee), and say
        the view needs redrawing while it does."""
        if ui.visual_len(text) > max(1, max_val_w):
            _ui_state['scrolling'] = True
        return ui.marquee(text, max(1, max_val_w), time.monotonic())

    _ui_state['scrolling'] = False

    def _txt(frame_id: str) -> str:
        """Return a text frame's stripped value, or empty string if absent."""
        return first_text(audio.get(frame_id))

    def _txts(frame_id: str) -> str:
        """A list-like frame's values for display, comma-separated.

        `_txt` returns only `text[0]`, which silently hid every value after the
        first on a duet credit or a two-genre track.
        """
        fr = audio.get(frame_id)
        if fr is None or not getattr(fr, 'text', None):
            return ""
        return format_value_list(list(fr.text))

    def _frac(frame_id: str) -> tuple[str, str]:
        """Return (current, total) from a fractional frame like '3/12'."""
        clean = re.sub(r'\s+', ' ', _txt(frame_id))
        parts = [p.strip() for p in re.split(r'[/|∕⁄]|\bof\b', clean, flags=re.IGNORECASE) if p.strip()]
        return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")

    title = _ui_state['chapter_title'] or _txt('TIT2') or track_title(file_path)
    artist = _txts('TPE1') or _txts('TPE2')
    album = _txt('TALB')

    lines: list[str] = []
    if title:
        lines.append(f"{C.BOLD}{_scroll(title)}{C.RESET}")

    # Album and artist ALWAYS show; the 'm' toggle only governs the extras below.
    if artist and album:
        second = f"{artist} - {album}"
    else:
        second = artist or album
    if second:
        lines.append(f"{C.DIM}{_scroll(second)}{C.RESET}")

    if _ui_state['show_metadata']:
        details: list[str] = []

        # Year from TDRC (v2.4) or TYER (v2.3), else the original-release frames.
        year = year_of(_txt('TDRC') or _txt('TYER') or _txt('TDOR') or _txt('TORY'))
        if year:
            details.append(str(year))
        genre = _txts('TCON')
        if genre:
            details.append(genre)

        # The work as the library reads it (TIT3, ©wrk, WORK or TXXX:WORK), so
        # the player and Browse can't disagree, whatever the format.
        from backtrack.music_library import library_entry
        work = str((library_entry(file_path) or {}).get('work') or '') if file_path else ''
        movement, _ = _frac('MVIN')
        disc, disc_total = _frac('TPOS')
        disc_subtitle = _txt('TSST')
        track, track_total = _frac('TRCK')

        def _track_str(t: str, total: str, label: str = "Track ") -> str:
            """TRCK in display form: 'Track 3 of 12', or 'Track 3' with no total."""
            return f"{label}{t} of {total}" if (t and total) else f"{label}{t}"

        def _disc_str(d: str, total: str) -> str:
            """TPOS in display form: 'Disc 1 of 2', or 'Disc 1' with no total."""
            return f"Disc {d} of {total}" if (d and total) else f"Disc {d}"

        # Classical: the work, then its movement in Roman numerals. These sit
        # alongside the disc/track fields rather than replacing them.
        if work:
            details.append(work)
        if movement:
            details.append(f"Movement {_movement_roman(movement)}")

        # Which part of the set this is: the disc subtitle names it better than a
        # number ever does, so TSST wins and TPOS is the fallback.
        if disc_subtitle:
            details.append(disc_subtitle)
        elif disc:
            details.append(_disc_str(disc, disc_total))

        # A movement number already says where the track sits in the work, so the
        # track number would only repeat it: show one or the other, never both.
        # Short of room, the track number gives way first: its word, then itself.
        track_at = len(details) if track and not movement else None
        if track_at is not None:
            details.append(_track_str(track, track_total))
        if _ui_state['chapter_pos']:
            details.append(_ui_state['chapter_pos'])

        fallbacks = [details]
        if track_at is not None:
            fallbacks += [details[:track_at] + [_track_str(track, track_total, "")] + details[track_at + 1:],
                          details[:track_at] + details[track_at + 1:]]
        joined = [' · '.join(d) for d in fallbacks if d]
        if joined:
            fits = next((j for j in joined if ui.visual_len(j) <= max(1, max_val_w)), joined[-1])
            lines.append(f"{C.DIM}{_trim(fits)}{C.RESET}")

        # Which documents the words on screen are coming from. A track can have a
        # script, a timed transcript, both or neither, and until this was shown the
        # only way to tell an accurate timeline from a guessed one was to watch it
        # drift for ten minutes.
        if _ui_state.get('debug') and _lyric_src['track'] == file_path:
            src = ' · '.join(_lyric_src['files']) or 'none found'
            est = _lyric_src['estimated']
            txt = _trim(f"lyrics: {src}" + (" · estimated, will drift" if est else ""))
            lines.append(f"{C.WARN if est else C.DIM}{txt}{C.RESET}")

    if not lines:
        fp = ui.truncate_text(file_path, max(1, max_val_w), placeholder='…', front=True)
        lines.append(f"{C.DIM}{fp}{C.RESET}")

    rows = 2 + (1 + bool(_ui_state.get('debug')) if _ui_state['show_metadata'] else 0)
    return lines + [""] * (rows - len(lines))


def _align_art_lines(art_lines: list[str], cols: int) -> list[str]:
    """Center art_lines horizontally within cols, padding every line to a uniform width."""
    if not art_lines:
        return []
    art_width = max(ui.visual_len(line) for line in art_lines)
    left_pad = max(0, (cols - art_width) // 2)
    aligned = []
    for line in art_lines:
        extra_padding = max(0, art_width - ui.visual_len(line))
        aligned.append(" " * left_pad + line + " " * extra_padding)
    return aligned


def _center_lines(lines: list[str], cols: int) -> list[str]:
    """Center each line horizontally within cols based on its visible length."""
    if not lines:
        return []
    centered: list[str] = []
    for line in lines:
        vis = ui.visual_len(line)
        left = max(0, (cols - vis) // 2)
        centered.append(" " * left + line)
    return centered


def draw_full_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                 is_paused: bool = False) -> tuple[int, int, int, int, int]:
    """Hide the cursor and draw the default playback UI layout."""
    sys.stdout.write(f"{C.HIDE}")
    return _draw_default_ui(file_path, audio, pre_art, size, is_paused)


_MIN_ART_ROWS = 3


def _art_room(avail: int) -> int:
    """The rows the art gets out of `avail`: all of them, or none when there's
    too little for art worth showing, rather than art forced in over the controls."""
    return avail if avail >= _MIN_ART_ROWS else 0


def _single_column_room(rows: int, meta_rows: int, control_rows: int, pane_rows: int) -> int:
    """Rows left for the art in the single-column layout. Counts exactly what is
    drawn under it (a blank, the metadata, progress, controls and hints, any pane
    below) plus the bottom margin, so at the window height that fits full-width
    art the hints sit on the last row with nothing spare under them."""
    return rows - (1 + meta_rows + control_rows + pane_rows) - ui.MARGIN_V


def _art_w(lines: list[str]) -> int:
    return max((ui.visual_len(l) for l in lines), default=0)


def _centred_art(file_path: str, box_w: int, max_w: int, avail_h: int,
                 pre_art: str | None) -> list[str]:
    """Art for a box `box_w` wide with equal gaps either side: when the best fit
    would leave an odd gap, one column wider if that still fits, else one narrower."""
    lines = _art_width_for_height(file_path, max_w, avail_h, pre_art)[1]
    w = _art_w(lines)
    if not lines or w >= box_w or (box_w - w) % 2 == 0:
        return lines
    wider = _art_width_for_height(file_path, w + 1, avail_h, pre_art)[1]
    if _art_w(wider) == w + 1:
        return wider
    return _art_width_for_height(file_path, w - 1, avail_h, pre_art)[1]


_PEOPLE_COL_MIN = 24             # a column of people narrower than this doesn't split in two


def _people_lines(cast: list, crew: list, width: int, rows: int) -> list[str]:
    """The People box's lines in `width` × `rows`: everyone credited, name
    then a dim role, the performers and then the production team. Two columns
    side by side when the width allows and either every entry fits whole in
    half of it or one column would run out of rows; else one. In two, the
    performers take the left and the team the right when each fits the rows,
    else they run on evenly. Whatever still doesn't fit is cut with a count
    of the rest."""
    entries = ([(name, role) for role, name in cast] + ([("", "")] if cast and crew else [])
               + [(name, role) for role, name in crew])
    half = (width - 3) // 2
    widest = lambda i: max((ui.visual_len(e[i]) for e in entries), default=0)   # noqa: E731
    neat = len(entries) >= 4 and widest(0) <= half * 3 // 5 and widest(0) + 2 + widest(1) <= half
    two = (neat or len(entries) > rows) and width >= 2 * _PEOPLE_COL_MIN + 3
    col_w = half if two else width
    grouped = two and cast and crew and max(len(cast), len(crew)) <= rows
    per = -(-len(entries) // 2) if two else len(entries)            # rows each column needs
    if not grouped and per > rows > 0:                               # still too many: the rest counted
        keep = rows * (2 if two else 1) - 1
        entries = entries[:keep] + [(f"{C.DIM}… {len(entries) - keep} more{C.RESET}", "")]
        per = -(-len(entries) // 2) if two else len(entries)
    name_w = min(max((ui.visual_len(n) for n, _r in entries), default=0), max(8, col_w * 3 // 5))

    def cell(name: str, role: str) -> str:
        name = ui.truncate_text(name, name_w)
        role = ui.truncate_text(role, col_w - name_w - 2) if col_w - name_w - 2 >= 2 else ""   # cut like the name, with a …
        line = f"{name}{' ' * (name_w - ui.visual_len(name))}  {C.DIM}{role}{C.RESET}" if role else name
        line = ui.clip_ansi(line, col_w) if ui.visual_len(line) > col_w else line
        return line + " " * max(0, col_w - ui.visual_len(line))
    columns = ([entries[:len(cast)], entries[len(cast) + 1:]] if grouped     # performers left, the team right
               else [entries[:per], entries[per:]] if two else [entries])
    if two:                                    # the gap between performers and crew never ends or starts a column
        columns = [[e for i, e in enumerate(col) if e != ("", "") or 0 < i < len(col) - 1] for col in columns]
        per = max(len(c) for c in columns)
    return [f" {C.FRAME}│{C.RESET} ".join(cell(*col[k]) if k < len(col) else " " * col_w for col in columns).rstrip()
            for k in range(per)]


def _static_panel(kind: str, audio, file_path: str, width: int, rows: int, box: tuple, titles: dict,
                  top: int, left: int) -> list[str]:
    """A panel drawn with the frame: its lines in width × rows, its title set."""
    from backtrack.playback import panels
    titles[box] = dict(PANELS)[kind]
    if kind == 'credits':
        return _people_lines([], panels.tag_credits(audio), width, rows)
    if kind == 'info':
        return panels.lines_of(panels.info_pairs(audio, file_path, _track_ctx['started']), width, rows)
    if kind == 'notes':
        return panels.notes_lines(audio, width, rows)
    pages = panels.booklet_pages(file_path)
    if not pages:
        return []
    at = _track_ctx['page'] % len(pages)
    titles[box] = f"Booklet {at + 1}/{len(pages)}"
    cols, prows = panels.page_box(pages[at], width, rows)
    cells, escape = panels.page_lines(pages[at], cols, prows)
    pad_l, pad_t = (width - cols) // 2, (rows - prows) // 2
    if escape:
        _frame['images'].append((top + pad_t, left + pad_l, escape))
    return [""] * pad_t + [" " * pad_l + line for line in cells]



# Panels whose content is a fixed set of lines: they take the rows those need,
# and the rest (lyrics, the queue, the visualiser, the booklet) share what's left.
_FITTED = ('people', 'credits', 'info', 'notes')


def _stack(kinds: list, y0: int, y1: int, fit) -> list:
    """A column of panels stacked down rows y0..y1, no gap left: (kind, top,
    bottom) each. A fitted panel gets the rows `fit(kind)` says its lines need
    (box included), never so many the others go short; the others share the
    rest by weight. A column of only fitted panels gives its spare rows to the
    last. Panels that would get fewer than 3 rows (a box and a line) wait,
    from the last."""
    kinds = list(kinds)
    while kinds:
        h = y1 - y0 + 1
        flex = [k for k in kinds if k not in _FITTED]
        size = {}
        room = h - 3 * len(flex)                                  # what the fitted ones may take
        for k in (k for k in kinds if k in _FITTED):
            size[k] = max(3, min(fit(k), room))
            room -= size[k]
        rest = h - sum(size.values())
        if flex:
            weights = [_PANEL_ROWS_WEIGHT.get(k, 2) for k in flex]
            for k, wt in zip(flex, weights):
                size[k] = rest * wt // sum(weights)
            for k in flex[:rest - sum(size[k] for k in flex)]:   # what rounding left: one each from the top
                size[k] += 1
        elif kinds:
            size[kinds[-1]] += rest                               # nothing to share it: the last takes it
        if all(v >= 3 for v in size.values()) and sum(size.values()) <= h:
            break
        kinds.pop()
    out, y = [], y0
    for k in kinds:
        out.append((k, y, y + size[k] - 1))
        y += size[k]
    return out


def _draw_boxed_ui(file_path: str, audio, pre_art: str | None, size: tuple, is_paused: bool,
                   cast_people: list, crew_people: list, has_cast: bool, has_lyrics: bool, has: dict):
    """The player in boxes, under the tab bar or with a panel showing: the art
    and the details in a box only as wide as the art; the panel's boxes (the
    people over the lyrics, or the queue) beside it in a wide window, else
    under it; along the bottom, a box with the controls, the progress bar and
    the times; the hints free under that. Returns what _draw_default_ui does."""
    cols, rows = size
    mh, mv = ui.MARGIN_H, ui.MARGIN_V
    frame_buffer = ["\033[H\033[3J\033[J"]
    emit = frame_buffer.append
    # The panels this track has something for, as each shape arranges them.
    live: dict = {}                                   # the moving panels' regions, for panels.LIVE
    arrange = {shape: [(k, s) for k, s in _ui_state['panels'][shape] if has.get(k)]
               for shape in ('wide', 'tall')}

    # Bottom up: the hints, under them nothing; over them the transport box.
    _frame['icons_col'] = mh + 3
    _frame['icons_n'] = _icons_fitting(cols - 2 * mh - 4)
    status, hints = _controls_line(is_paused, has_lyrics=has_lyrics, has_credits=has_cast)
    hint_lines = hints.splitlines() if hints else []
    # The hints keep what's left once the transport box has its rows.
    hint_lines = hint_lines[:max(0, rows - mv - ui.top_margin() - 3)]
    hint_top = rows - mv - len(hint_lines) + 1
    t_bot = hint_top - 1
    t_top, ctrl = t_bot - 2, t_bot - 1
    top, body_bot = ui.top_margin() + 1, t_bot - 3
    body_h = body_bot - top + 1
    full_l, full_r = mh + 1, cols - mh
    aspect = ui.cell_aspect()
    boxes: list = []
    titles: dict = {}
    lyric_row, lyric_bot = body_bot, body_bot - 1
    side = beside = False
    # Too short for any box over the transport's (a tiny window, all the
    # hints): that one alone, still boxed.
    if body_h >= _BOX_MIN_BODY:

        # The boxes tile the body with no gap. A panel's place: a full-width band
        # at the top or bottom; in a column left or right of the player; or in
        # the player's own column, above or below its art and details. A wide
        # window has columns when they fit; else (a tall window, or no column
        # panels) the player is full width, its column panels banded with the
        # rest, and any left / right ones under it, in two columns or one.
        meta_n = len(_meta_left_lines(audio, file_path, _DETAILS_W))
        width = full_r - full_l + 1
        band_w = full_r - full_l - 3                              # a band's inside
        _fits: dict = {}

        def rows_for(kind: str, w: int) -> int:
            """Rows a fitted panel's lines need at `w` inside, its box included."""
            if (kind, w) not in _fits:
                from backtrack.playback import panels as _p
                lines = (_people_lines(cast_people, crew_people, w, body_h // 3) if kind == 'people'
                         else _people_lines([], _p.tag_credits(audio), w, body_h // 3) if kind == 'credits'
                         else _p.lines_of(_p.info_pairs(audio, file_path, _track_ctx['started']), w, body_h)
                         if kind == 'info' else _p.notes_lines(audio, w, body_h))
                _fits[kind, w] = len(lines) + 2
            return _fits[kind, w]

        def need(kinds: list, w: int, flex: int = _PANEL_MIN_ROWS) -> int:
            """Rows a stack of panels keeps: the fitted ones what their lines need, the others `flex`."""
            return sum(rows_for(k, w) if k in _FITTED else flex for k in kinds)

        def split(shape: str, columns: bool) -> tuple:
            """This shape's panels by place: (top band, [left, right], above, below,
            bottom band). Without columns the player's column panels join the
            bands. Bands give way, from the bottom, to leave the middle its room."""
            at = lambda *where: [k for k, s in arrange[shape] if s in where]   # noqa: E731
            if columns:
                t, a_, b_, bt = at('top'), at('above'), at('below'), at('bottom')
            else:
                t, a_, b_, bt = at('top') + at('above'), [], [], at('below') + at('bottom')
            while t + bt and need(t, band_w, _BAND_ROWS) + need(bt, band_w, _BAND_ROWS) > body_h - _MID_MIN_H:
                (bt if bt else t).pop()
            return t, [at('left'), at('right')], a_, b_, bt

        # Beside the panels, the player's box at most its share: the art over
        # the details, or beside them when that makes it bigger; never narrower
        # than the details need.
        def fit(inner: int, h: int) -> tuple:
            """The player's box beside the panels, `inner` columns at most and
            `h` rows: (its inside width, art rows over the details, art rows
            beside them, whether beside, the details' width beside)."""
            s_rows = min(_art_room(h - 3 - meta_n), int(inner / aspect))
            b_text = min(_DETAILS_W, inner - 2 - round(_MIN_ART_ROWS * aspect))
            b_rows = min(_art_room(h - 2), int((inner - 2 - b_text) / aspect)) if b_text >= _BESIDE_TEXT else 0
            by = b_rows >= max(_MIN_ART_ROWS, s_rows * _BESIDE_GAIN)
            used = (round(b_rows * aspect) + 2 + b_text if by
                    else max(round(s_rows * aspect), min(_PLAYER_MIN_W, inner)))
            return min(used, inner), s_rows, b_rows, by, b_text

        # Wide: a column of panels beside the player on each side that has
        # any. The player's box takes its art at full height when that leaves
        # each column _PANEL_SIDE_MIN; else it shrinks (smaller art) down to
        # _MID_MIN_TEXT inside. The columns share what's left by what they
        # hold (lyrics wider than the queue), so the boxes still tile the row.
        w_top, cols_w, w_above, w_below, w_bot = split('wide', True)
        n_cols = sum(bool(c) for c in cols_w)
        room = width - n_cols * mh - n_cols * _PANEL_SIDE_MIN      # the player's box at most, beside them
        share = _PLAYER_SHARE if n_cols < 2 else _PLAYER_SHARE_TWO   # between two columns, a third or so
        mid_w = body_h - need(w_top, band_w, _BAND_ROWS) - need(w_bot, band_w, _BAND_ROWS)
        guess = min(int(width * share), room) - 4
        side_in, s_rows, b_rows, side_beside, b_text = fit(guess, mid_w - need(w_above + w_below, guess))
        side = n_cols > 0 and room - 4 >= _MID_MIN_TEXT
        spare = width - (side_in + 4) - n_cols * mh                 # the columns' share
        want = [max((_PANEL_WANT.get(k, 40) for k in c), default=0) for c in cols_w]
        lw = 0
        if cols_w[0]:                                             # the left column; the right takes the rest
            lw = spare if not cols_w[1] else max(_PANEL_SIDE_MIN, min(spare - _PANEL_SIDE_MIN,
                                                                      spare * want[0] // max(1, sum(want))))
        # Without columns: the wide arrangement when it has none (it fits any
        # window), else the tall one.
        under_shape = 'wide' if n_cols == 0 else 'tall'
        u_top, cols_t, _a, _b, u_bot = split(under_shape, False)
        two_under = all(cols_t) and width - mh >= 2 * _PANEL_SIDE_MIN
        want_t = [max((_PANEL_WANT.get(k, 40) for k in c), default=0) for c in cols_t]

        def reserve(cols_: list) -> int:
            """Rows the columns under the player keep."""
            return (max(need(cols_[0], band_w), need(cols_[1], band_w)) if two_under
                    else need(cols_[0] + cols_[1], band_w))

        # Beside wins wherever it fits, unless it would leave the art much
        # smaller than under the panels gives it (a tall, narrow window).
        mid_t = body_h - need(u_top, band_w, _BAND_ROWS) - need(u_bot, band_w, _BAND_ROWS)
        if side:
            art_under = min(_art_room(mid_t - 3 - meta_n - reserve(cols_t)), int((width - 4) / aspect))
            side = (b_rows if side_beside else s_rows) >= _SIDE_ART_SHARE * art_under

        shape = 'wide' if side else under_shape
        if side:
            top_k, cols, above_k, below_k, bot_k = w_top, cols_w, w_above, w_below, w_bot
        else:
            top_k, cols, above_k, below_k, bot_k = u_top, cols_t, [], [], u_bot
        m_top = top + need(top_k, band_w, _BAND_ROWS)            # the middle row, between the bands
        m_bot = body_bot - need(bot_k, band_w, _BAND_ROWS)
        m_h = m_bot - m_top + 1

        # The player's box: the art as big as the room allows, the details under it.
        if side:
            max_inner = side_in
            c_need = need(above_k + below_k, side_in)
            avail = m_h - c_need - 3 - meta_n
        else:
            max_inner = width - 4
            # Under it, the columns keep what they need: the fitted panels' rows,
            # a few for the lyrics or the queue.
            avail = m_h - 3 - meta_n - (reserve(cols) if any(cols) else 0)
        # The art's height is settled as for a square, as big as the room
        # allows; then the cover takes its own shape in it (cover_box), whole.
        # The box is the art and its inset, no wider.
        art_rows = min(_art_room(avail), int(max_inner / aspect))
        # A short, wide box: the art beside the details rather than over them,
        # when that makes it bigger.
        beside = False
        if side:
            beside = side_beside
            art_rows = b_rows if beside else s_rows
            art_rows = min(art_rows, _art_room(avail + (meta_n + 1 if beside else 0)))
        else:
            b_rows = min(_art_room(avail + meta_n + 1), int((max_inner - 2 - _BESIDE_TEXT) / aspect))
            beside = b_rows >= max(_MIN_ART_ROWS, art_rows * _BESIDE_GAIN)
            art_rows = b_rows if beside else art_rows
        # Beside the details it keeps to the square's width, the details' column
        # being reserved beside that; over them it may take the box's width.
        wide = min(max_inner, round(art_rows * aspect)) if beside else max_inner
        art_cols, art_rows = cover_box(file_path, wide, art_rows, pre_art)
        art = boxed_art(file_path, art_cols, art_rows, pre_art) if art_rows >= _MIN_ART_ROWS else []
        beside = beside and bool(art)
        art_w = _art_w(art) if art else 0
        content_h = max(len(art), meta_n) if beside else len(art) + (1 if art else 0) + meta_n
        if not side and any(cols) and m_bot - (m_top + 1 + content_h) < 3:
            cols = [[], []]                   # no room under the player for the columns' boxes: they wait
        lw = lw if side else 0                                    # the left column's width
        p_l = full_l + (lw + mh if lw else 0)
        p_r = p_l + side_in + 3 if side else full_r
        box_in = p_r - p_l - 1                                    # between the borders
        # The player's box: between what's above and below it in its column;
        # with columns under it, just its content; else down to the band or
        # the transport, what's in it centred.
        if side:
            p_top = m_top + need(above_k, box_in - 2)
            p_bot = m_bot - need(below_k, box_in - 2)
        else:
            p_top = m_top
            p_bot = m_top + 1 + content_h if any(cols) else m_bot
        c_top = p_top + 1 + max(0, (p_bot - p_top - 1 - content_h) // 2)
        if beside:                                                # the pair centred across the box
            # The details' column, not this track's details, so the art holds still.
            text_col = min(box_in - 4 - art_cols, _DETAILS_MAX_W)
            meta = _meta_left_lines(audio, file_path, text_col)
            pair_w = art_cols + 2 + text_col
            left = p_l + 2 + max(0, (box_in - 2 - pair_w) // 2)
        else:
            left = p_l + 2 + (box_in - 2 - art_w) // 2           # centred across the box
        for k, line in enumerate(art):
            emit(f"\033[{c_top + k};{left}H{line}")
        geom.art_top, geom.art_left, geom.art_width, geom.art_height = c_top, left - 1, art_w, len(art)
        geom.art_gap = bool(art)
        # The details, as many as the box holds (a tiny window: the title first).
        if beside:
            r = c_top + max(0, (len(art) - len(meta)) // 2)
            for line in meta[:max(0, p_bot - r)]:
                emit(f"\033[{r};{left + art_cols + 2}H{line}")
                r += 1
        else:
            r = c_top + len(art) + (1 if art else 0)
            text_w = box_in - 2                                   # the details: inside "│ " … " │"
            for line in _meta_left_lines(audio, file_path, text_w)[:max(0, p_bot - r)]:
                emit(f"\033[{r};{p_l + 2 + max(0, (text_w - ui.visual_len(line)) // 2)}H{line}")
                r += 1
        boxes.append((p_top, p_bot, p_l, p_r))

        # The panels, each place stacked in its order.
        regions = []
        if top_k:
            regions.append((top_k, top, m_top - 1, full_l, full_r))
        if side:
            regions += [(c, m_top, m_bot, l_, r_) for c, l_, r_ in (
                (cols[0], full_l, full_l + lw - 1), (cols[1], p_r + 1 + mh, full_r)) if c]
            if above_k:
                regions.append((above_k, m_top, p_top - 1, p_l, p_r))
            if below_k:
                regions.append((below_k, p_bot + 1, m_bot, p_l, p_r))
        elif two_under and all(cols):                             # split by what they hold, each its least at most
            half = (width - mh) * want_t[0] // max(1, sum(want_t))
            half = max(_PANEL_SIDE_MIN, min(width - mh - _PANEL_SIDE_MIN, half))
            regions += [(cols[0], p_bot + 1, m_bot, full_l, full_l + half - 1),
                        (cols[1], p_bot + 1, m_bot, full_l + half + mh, full_r)]
        elif any(cols):
            regions.append((cols[0] + cols[1], p_bot + 1, m_bot, full_l, full_r))
        if bot_k:
            regions.append((bot_k, m_bot + 1, body_bot, full_l, full_r))
        if regions:
            order = []
            for kinds, y0, y1, l_, r_ in regions:
                if y1 - y0 + 1 < 3:
                    continue
                for kind, b0, b1 in _stack(kinds, y0, y1, lambda k, w=r_ - l_ - 3: rows_for(k, w)):
                    box = (b0, b1, l_, r_)
                    boxes.append(box)
                    order.append(kind)
                    if kind == _ui_state['arranging']:
                        _frame['focus'] = box
                    a_in = r_ - l_ - 3                                # inside "│ " … " │"
                    _frame['panel_boxes'][kind] = box
                    if kind in _LIVE_PANELS:
                        titles[box] = dict(PANELS)[kind]
                        live[kind] = (b0 + 1, l_ + 2, a_in, b1 - b0 - 1)
                        pc.screen_reserve(b0 + 1, b1 - 1, l_ + 2, l_ + 1 + a_in)   # its own, painted each tick
                    elif kind in ('credits', 'info', 'notes', 'booklet'):
                        for k, line in enumerate(_static_panel(kind, audio, file_path, a_in, b1 - b0 - 1,
                                                               box, titles, b0 + 1, l_ + 2)):
                            emit(f"\033[{b0 + 1 + k};{l_ + 2}H{line}")
                    elif kind == 'people':
                        titles[box] = "People"
                        for k, line in enumerate(_people_lines(cast_people, crew_people, a_in, b1 - b0 - 1)):
                            emit(f"\033[{b0 + 1 + k};{l_ + 2}H{line}")
                    elif kind == 'lyrics':
                        titles[box] = "Lyrics"
                        lyric_row, lyric_bot = b0 + 1, b1 - 1
                        geom.lyric_left, geom.lyric_width = l_ + 2, a_in
                        geom.lyric_centre = (lyric_row + lyric_bot) // 2
                        pc.screen_reserve(lyric_row, lyric_bot, l_ + 2, l_ + 1 + a_in)   # the lyrics' own
                    else:                                            # the queue, or the chapters
                        use_list(kind)
                        titles[box] = queue_title(r_ - l_ - 6)     # the border's room for it
                        _place_queue(emit, b0 + 1, l_ + 2, a_in, b1 - b0 - 1, heading=False, kind=kind)
            use_list('chapters' if chapters_listed() else 'queue')
            _frame.update(panel_order=order, shape=shape)
            if queue_pane.scrolled():            # a queue title scrolling: keep redrawing as it moves
                _ui_state['scrolling'] = True
            if _ui_state['arranging'] == _ARRANGE_FIRST and order:
                _ui_state['arranging'] = order[0]
                _frame['focus'] = _frame['panel_boxes'][order[0]]
        if _ui_state['arranging'] is not None and _ui_state['arranging'] not in (_frame.get('panel_order') or []):
            _ui_state['arranging'] = None                    # nothing of it on screen: arranging ends

    # The transport box: the controls, then the bar and the times (update_progress_ui).
    # Over it a body too short for a box of its own: the transport's box
    # takes those rows too, the title in them.
    if not boxes and body_h > 0:
        t_top = top
        for k, line in enumerate(_meta_left_lines(audio, file_path, full_r - full_l - 3)[:ctrl - top - 1]):
            emit(f"\033[{top + 1 + k};{full_l + 2}H{line}")
    controls = status.strip()
    bar_l = _frame['icons_col'] + ui.visual_len(controls) + 3
    boxes.append((t_top, t_bot, full_l, full_r))
    _frame.update(boxes=boxes, titles=titles, prog_row=ctrl, bar=(bar_l, max(0, full_r - 2 - bar_l + 1)),
                  side=side, beside=beside)
    from backtrack.playback import panels
    panels.LIVE.place(live)

    for k, line in enumerate(hint_lines):
        emit(f"\033[{hint_top + k};1H\033[K{' ' * mh}{line}")
    compute_controls_hint_cells(hint_lines, hint_top)
    _render_frame_buffer(frame_buffer, rows)
    for row, col, escape in _frame['images']:          # the booklet's page, as an image over its cells
        sys.stdout.write(f"\0337\033[{row};{col}H{escape}\0338")
    # The transport row is the controls' and the bar's, not the frame's (see _frame_borders).
    write_controls(ctrl, status)
    sys.stdout.flush()
    return ctrl, ctrl, lyric_row, cols, lyric_bot


def _draw_default_ui(file_path: str, audio, pre_art: str | None, size: tuple,
                     is_paused: bool = False) -> tuple[int, int, int, int, int]:
    """Render the full playback screen (art, metadata, controls, and any active pane)
    for the current layout mode, returning the progress/control/lyric row positions and art bottom row."""
    cols, rows = size
    mode = _layout_mode(cols)
    # Wide is the split view; with nothing to put beside the art, a wide window
    # is laid out like a standard one, so the art doesn't halve at 120 columns.
    if mode == 'wide' and not (_ui_state['show_credits'] or _ui_state['show_lyrics'] or _ui_state['show_queue']):
        mode = 'standard'
    # Reset art geometry each frame; only branches that draw art repopulate it.
    # Likewise the queue's click rows: only a frame that draws the queue has any,
    # and the inline image: only a frame that lays out art has one.
    _queue_click_rows.clear()
    _queue_area[0] = None
    _inline_art['path'] = None
    geom.reset_frame()
    pc.screen_release()            # this frame reserves the lyrics' cells afresh
    pc.screen_forget_pictures()    # a picture the screen before left: written over in full
    cols_, rows_ = size
    _frame.update(icons_n=3)
    if rows_ <= _TINY_ROWS or cols_ < _TINY_COLS:
        return _draw_tiny(size, is_paused)

    # buf[0] is a clear sequence that _render_frame_buffer drops (see there).
    frame_buffer = ["\033[H\033[3J\033[J"]

    def emit(text):
        frame_buffer.append(text)

    cast_people, crew_people = people_of(audio)
    has_cast = bool(cast_people or crew_people)
    # What the player resolved for this track (set_lyric_sources), not the tags:
    # its lyrics may be a file beside it, or none where the setting says synced only.
    has_lyrics = (_ui_state['lyrics_pane'] and _lyric_src['track'] == file_path
                  and bool(_lyric_src['files']))

    # Boxed under a tab bar or with a panel showing; clean, edge to edge, with neither.
    has = track_has(audio, file_path, has_cast, has_lyrics)
    pane_on = _ui_state['show_queue'] or any(has.get(k) for k in shown_panels())
    # Boxed whenever the tab bar shows or a panel does, at any size: only with
    # the bar hidden (f) and no panel is the player clean, edge to edge.
    boxed = bool(ui.tab_rows()) or bool(pane_on)
    _frame.update(boxed=boxed, boxes=[], titles={}, prog_row=None, icons_col=None, bar=None,
                  focus=None, panel_order=[], shape=None, panel_boxes={}, images=[])
    if boxed:
        drawn = _draw_boxed_ui(file_path, audio, pre_art, size, is_paused,
                               cast_people, crew_people, has_cast, has_lyrics, has)
        if drawn is not None:
            return drawn                                     # else no room for boxes: clean
    lyrics_on = has_lyrics and _ui_state['show_lyrics'] and not _ui_state['show_queue']
    above = ui.tab_rows()                                    # rows over the content

    row_cursor = 0

    if mode == 'wide':

        # Split view: left half = art + meta, right pane = credits/lyrics.
        art_w = min(cols // 2, ART_MAX_WIDTH)
        right_w = cols - art_w - _WIDE_SPLIT_GUTTER
        meta_val_w = right_w - 10

        left_col = _meta_left_lines(audio, file_path, meta_val_w)
        # Size the controls/hints first so the art leaves room for them: showing
        # help (several hint lines) shrinks the art instead of drawing over the controls.
        status_ln, shortcuts_ln = _controls_line(is_paused, has_lyrics=has_lyrics, has_credits=has_cast)
        shortcut_lines = shortcuts_ln.splitlines() or [""]
        avail_h = _art_room(rows - len(left_col) - len(shortcut_lines) - 4 - ui.MARGIN_V
                            - max(ui.MARGIN_V, above))
        # Art is inset from the panel edges so it floats with breathing room.
        art_inner_w = max(10, art_w * 3 // 4)
        art_lines = _centred_art(file_path, art_w, art_inner_w, avail_h, pre_art)

        art_vis_w = _art_w(art_lines)
        left_margin = max(ui.MARGIN_H, (art_w - art_vis_w) // 2)

        geom.art_width = art_vis_w
        geom.art_left = left_margin
        geom.art_height = len(art_lines)
        geom.right_left = art_w + _WIDE_SPLIT_GUTTER
        geom.right_width = right_w

        # The art sits at the top, under the top margin (the tab bar's row);
        # a taller window only adds space at the bottom (and to the pane beside it).
        top = max(ui.MARGIN_V if art_lines else 0, above)
        for _ in range(top):
            emit("")
        row_cursor += top
        geom.art_top = row_cursor + 1

        for line in art_lines:
            emit(" " * left_margin + line)
        row_cursor += len(art_lines)
        emit("")
        row_cursor += 1

        for line in left_col:
            vis = ui.visual_len(line)
            pad = ' ' * max(0, (art_w - vis) // 2)
            emit(f"{pad}{line}")
        row_cursor += len(left_col)

        emit("")
        prog_row = row_cursor + 2
        _frame['prog_row'] = prog_row                 # update_progress_ui's, not the frame's
        ctrl_row = prog_row + 1

        # Recompute now that the art geometry is set, so the transport line centres over the art.
        ctrl_row, shortcut_lines = _place_controls(
            emit, ctrl_row, rows, is_paused, has_lyrics, has_cast)

        _pane_top = geom.art_top  # right pane aligns with art top after any vertical centring

        # Queue view takes over the whole right pane when toggled on.
        if _ui_state['show_queue']:
            _place_queue(emit, _pane_top, geom.right_left, right_w, (ctrl_row - 1) - _pane_top)
            lyric_row = ctrl_row  # suppress the lyric area while the queue shows
            art_bottom_row = ctrl_row - 1
            _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
            sys.stdout.flush()
            return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

        credits_lines = []
        if has_cast and _ui_state['show_credits']:
            cast_limit, crew_limit = 3, 3
            gap = ' ' * 4
            cast_col_w = max(12, (right_w - len(gap)) // 2)
            crew_col_w = max(12, right_w - cast_col_w - len(gap))

            cast_heading = [f"{C.DIM}PERFORMERS{C.RESET}"] if cast_people else []
            crew_heading = [f"{C.DIM}PRODUCTION TEAM{C.RESET}"] if crew_people else []
            cast_body = _build_cast_lines(cast_people, cast_col_w, limit=cast_limit) if cast_people else []
            crew_body = _build_crew_lines(crew_people, crew_col_w, cast_names=[n for _, n in cast_people[:cast_limit]], limit=crew_limit) if crew_people else []

            c_lines = cast_heading + cast_body
            cr_lines = crew_heading + crew_body
            for i in range(max(len(c_lines), len(cr_lines))):
                lft = c_lines[i] if i < len(c_lines) else ''
                rgt = cr_lines[i] if i < len(cr_lines) else ''
                pad_c = ' ' * max(0, cast_col_w - ui.visual_len(lft))
                credits_lines.append(f"  {lft}{pad_c}{gap}{rgt}")

        for idx, line in enumerate(credits_lines):
            emit(f"\033[{_pane_top + idx};{geom.right_left}H{line}")

        lyric_row = _pane_top
        if credits_lines:
            rule_row = _pane_top + len(credits_lines) + 1
            emit(f"\033[{rule_row};{geom.right_left}H{_credits_rule(right_w)}")
            lyric_row = rule_row + 2
        if art_lines:
            geom.lyric_centre = geom.art_top + (len(art_lines) - 1) // 2
        # Cap the right pane at the row above the transport controls so the
        # lyric-window clearing loop never touches the controls/hints rows.
        art_bottom_row = max(lyric_row + 2, ctrl_row - 1)
        if lyrics_on:                 # the lyrics' own cells: the frame leaves them be
            pc.screen_reserve(lyric_row, art_bottom_row, geom.right_left, geom.right_left + right_w - 1)

        _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

    else:
        geom.right_left = None
        geom.right_width = None

        meta_val_w = cols - 12
        left_col = _meta_left_lines(audio, file_path, meta_val_w)

        _, temp_shortcuts = _controls_line(is_paused, width=cols, has_lyrics=has_lyrics, has_credits=has_cast)
        control_rows = 2 + len(temp_shortcuts.splitlines() or [""])

        credits_est = tune.PANE_CREDITS_EST_ROWS if (has_cast and _ui_state['show_credits']) else 0
        lyrics_est = tune.PANE_LYRICS_EST_ROWS if _ui_state['show_lyrics'] else 0
        queue_est = tune.PANE_QUEUE_MIN_ROWS if _ui_state['show_queue'] else 0
        tab = above
        room = _single_column_room(rows, len(left_col), control_rows,
                                   credits_est + lyrics_est + queue_est) - tab
        max_art_h = _art_room(room) if cols >= _MIN_ART_COLS else 0

        # Art the height holds back from full width gets the blank row above the
        # title too: a row taller, wider, and with room to make its sides even.
        art_lines = _art_width_for_height(file_path, cols, max_art_h, pre_art)[1]
        gap_used = int(bool(art_lines) and _art_w(art_lines) < cols)
        # Narrowed art leaves room beside it for the volume bar: both then keep
        # the wide layout's top margin rather than touching the window's edge.
        # The tab bar, when there is one, is that margin.
        top_margin = max(ui.MARGIN_V if gap_used else 0, tab)
        if gap_used:
            art_lines = _centred_art(file_path, cols, cols, _art_room(room + tab + 1 - top_margin), pre_art)
        geom.art_gap = not gap_used
        actual_art_w = _art_w(art_lines) or cols
        if actual_art_w < cols:
            # Art was narrowed to fit terminal height: centre it.
            art_lines = _align_art_lines(art_lines, cols)
            geom.art_left = max(0, (cols - actual_art_w) // 2)
            geom.art_width = actual_art_w
        else:
            geom.art_left = 0
            geom.art_width = cols if art_lines else 0

        geom.art_height = len(art_lines)

        # The art sits at the top: any rows it doesn't use (height rounding, a
        # window taller than full-width art needs, no art at all) are left at
        # the bottom, or go to a panel under the controls.
        for _ in range(top_margin):
            emit("")
        row_cursor += top_margin
        geom.art_top = row_cursor + 1

        for line in art_lines: emit(line)
        row_cursor += len(art_lines)
        if geom.art_gap:
            emit("")
            row_cursor += 1

        left_col = _center_lines(left_col, cols)
        for line in left_col: emit(line)
        row_cursor += len(left_col)

        # The progress row stays out of the frame: update_progress_ui owns it,
        # and a frame that painted it blank made the bar flicker on each redraw.
        row_cursor += 1
        prog_row = row_cursor
        _frame['prog_row'] = prog_row
        ctrl_row = prog_row + 1

        ctrl_row, shortcut_lines = _place_controls(
            emit, ctrl_row, rows, is_paused, has_lyrics, has_cast)

        ctrl_row_end = ctrl_row + len(shortcut_lines)
        p0 = ctrl_row_end + 2                                  # the panel's first row

        if _ui_state['show_queue']:
            q_start = p0
            _place_queue(emit, q_start, 1 + ui.MARGIN_H, cols - ui.MARGIN_H,
                         rows - ui.MARGIN_V - q_start + 1)     # down to the last row, inclusive
            lyric_row = rows - ui.MARGIN_V
            art_bottom_row = rows - ui.MARGIN_V
            _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
            sys.stdout.flush()
            return prog_row, ctrl_row, lyric_row, cols, art_bottom_row

        c_lines = []
        cr_lines = []

        if has_cast and _ui_state['show_credits']:
            cast_limit, crew_limit = 3, 3
            gap = '  '
            col_w = max(12, (cols - len(gap)) // 2)
            c_lines = [f"{C.DIM}PERFORMERS{C.RESET}"] + _build_cast_lines(cast_people, col_w, limit=cast_limit)
            cr_lines = [f"{C.DIM}PRODUCTION TEAM{C.RESET}"] + _build_crew_lines(crew_people, col_w, cast_names=[n for _, n in cast_people[:cast_limit]], limit=crew_limit)

            for i in range(max(len(c_lines), len(cr_lines))):
                lft = c_lines[i] if i < len(c_lines) else ''
                rgt = cr_lines[i] if i < len(cr_lines) else ''
                pad = ' ' * max(0, col_w - ui.visual_len(lft))
                emit(f"\033[{p0 + i};1H\033[K{lft}{pad}{gap}{rgt}")

        lyric_row = ctrl_row_end + 3
        n_credits = max(len(c_lines), len(cr_lines))
        if c_lines or cr_lines:
            rule_row = ctrl_row_end + 3 + n_credits
            emit(f"\033[{rule_row};1H\033[K{_credits_rule(cols)}")
            lyric_row = rule_row + 2
        inset = max(ui.MARGIN_H, int(cols * _LYRICS_INSET_FRAC))
        geom.lyric_left = inset + 1
        geom.lyric_width = cols - inset - ui.MARGIN_H
        if lyrics_on:                 # the lyrics' own cells: the frame leaves them be
            pc.screen_reserve(lyric_row, rows - ui.MARGIN_V, geom.lyric_left,
                              geom.lyric_left + geom.lyric_width - 1)
        art_bottom_row = rows - ui.MARGIN_V
        _render_frame_buffer(frame_buffer, rows - ui.MARGIN_V)
        sys.stdout.flush()
        return prog_row, ctrl_row, lyric_row, cols, art_bottom_row
