"""The player's queue pane: the list around the current track, its layout,
and the rows a click can play."""
from __future__ import annotations
import re
import time
from backtrack.music_library import chapter_label, format_tag_values, library_entry, live_library
from backbone import ui
from backbone.prompt.core import Column, _table_widths
from backbone.ui import Colors as C
from backbone.log import quietly


# Up-next context for the queue view: list of display titles + current index,
# and the queue keys' cursor (a queue position, or None to follow the current).
_queue_ctx: dict = {'titles': [], 'paths': [], 'index': 0, 'meta': [], 'visible': [],
                    'cursor': None, 'label': 'Queue'}
# The same pane can list the current file's chapters instead (see use_list).
_chapter_ctx: dict = {'titles': [], 'paths': [], 'index': 0, 'meta': [], 'visible': [],
                      'cursor': None, 'label': 'Chapters'}
_shown = [_queue_ctx]


def use_list(name: str) -> None:
    """Which list the pane, its cursor and its clicks work on: 'queue' or 'chapters'."""
    _shown[0] = _chapter_ctx if name == 'chapters' else _queue_ctx


def _ctx() -> dict:
    return _shown[0]


def set_chapter_context(chapters: list, current: int) -> None:
    """Register the current file's chapters ([[start s, title], ...]) and the one playing."""
    times = [ui.format_time(int(start)) for start, _t in chapters]
    w = max(map(len, times), default=0)              # right-aligned, so the titles line up
    titles = [f"{t.rjust(w)}  {chapter_label(chapters, i)[0]}" for i, t in enumerate(times)]
    if titles != _chapter_ctx['titles']:
        _chapter_ctx['meta'] = [{'title': t, 'artist': '', 'album': '', 'album_artist': ''} for t in titles]
        _chapter_ctx['titles'] = titles
        _chapter_ctx['cursor'] = None
    _chapter_ctx['index'] = current


def set_queue_context(titles: list[str], index: int, paths: list[str] | None = None) -> None:
    """Register the current play queue so the queue view can render it. The
    per-track details are only re-read when the queue itself changed:
    moving to the next track changes just the index."""
    titles, paths = list(titles or []), list(paths or [])
    if titles != _queue_ctx['titles'] or paths != _queue_ctx['paths']:
        _queue_ctx['meta'] = _queue_metadata(titles, paths)
    _queue_ctx['titles'] = titles
    _queue_ctx['paths'] = paths
    _queue_ctx['index'] = index
    if _queue_ctx['cursor'] is not None:            # the queue may have shrunk
        _queue_ctx['cursor'] = min(_queue_ctx['cursor'], len(titles) - 1) if titles else None


def queue_cursor() -> int | None:
    """The position the pane's keys act on; None follows the current item."""
    return _ctx()['cursor']


def set_queue_cursor(pos: int | None) -> None:
    n = len(_ctx()['titles'])
    _ctx()['cursor'] = None if pos is None or not n else max(0, min(n - 1, pos))


def move_queue_cursor(delta: int) -> None:
    """Move the cursor a row, starting from the current item."""
    cur = _ctx()['cursor']
    set_queue_cursor((_ctx()['index'] if cur is None else cur) + delta)


def _queue_metadata(titles: list[str], paths: list[str]) -> list[dict]:
    """Title/artist/album for each queued track: from the in-memory library,
    reading a file's tags only when it isn't in there (a CLI-played file),
    since reading every queued file would stall each track change on a long queue."""
    by_path = {t['path']: t for t in (live_library() or [])}
    meta: list[dict] = []
    for i, title in enumerate(titles):
        item = {
            'title': title or '',
            'artist': '',
            'album': '',
            'album_artist': '',
        }
        if i < len(paths):
            with quietly():
                data = library_entry(paths[i], by_path)
                item['title'] = data.get('title') or item['title']
                item['artist'] = data.get('artist') or ''
                item['album'] = data.get('album') or ''
                item['album_artist'] = data.get('album_artist') or ''
                item['duration'] = data.get('duration') or 0
        meta.append(item)
    return meta


def has_queue() -> bool:
    """Return whether there's more than one track in the queue worth showing."""
    return len(_queue_ctx['titles']) > 1




_QUEUE_RIGHT_MARGIN = 2 * ui.MARGIN_H   # breathing room before the screen edge


_QUEUE_MIN_ROWS = 2         # the header and one track, or the pane isn't drawn at all


# Screen row → (position, first column, last column, which list) of each row
# drawn, so a click on one can play it (the queue) or go to it (the chapters).
# Cleared by each frame (the layout), as both lists may be on screen.
_queue_click_rows: dict[int, tuple[int, int, int, str]] = {}


def queue_click_index(row: int, col: int) -> tuple[str, int] | None:
    """(which list, position) of the row clicked at (row, col), if any."""
    hit = _queue_click_rows.get(row)
    return (hit[3], hit[0]) if hit and hit[1] <= col <= hit[2] else None


def _place_queue(log, top: int, left: int, width: int, rows: int, heading: bool = True,
                 kind: str = 'queue') -> bool:
    """Draw the list `use_list` picked (`kind`: 'queue' or 'chapters') at
    (top, left) within width × rows, or nothing when there isn't room for even
    the header and one row. Returns whether it drew. `heading` False: no
    heading line (a box's title says it: queue_title)."""
    if rows < _QUEUE_MIN_ROWS - (0 if heading else 1) or width < 10:
        return False
    lines = _build_queue_lines(width - (_QUEUE_RIGHT_MARGIN if heading else 0), rows + (0 if heading else 1))
    if not heading:
        lines = lines[1:]
    first = 1 if heading else 0                       # the line the tracks start on
    for qi, line in enumerate(lines):
        log(f"\033[{top + qi};{left}H{line}")
        if qi >= first and qi - first < len(_ctx()['visible']):
            _queue_click_rows[top + qi] = (_ctx()['visible'][qi - first], left,
                                           left + ui.visual_len(line) - 1, kind)
    return True


def queue_title() -> str:
    """The queue's name and where it's at, for its box: "Queue · 3 of 12"."""
    ctx = _ctx()
    total, idx = len(ctx['titles']), ctx['index']
    where = f"{idx + 1} of {total}" if 0 <= idx < total else str(total)
    return f"{ctx['label']} · {where}" if total else ctx['label']


def _queue_window(total: int, current: int | None, rows: int) -> list[int]:
    """Which queue positions fit in `rows`, in priority order: the current
    track; then what comes after it, as far as it goes; then what was played
    before it, nearest first, only once everything after it is showing."""
    if rows <= 0 or total <= 0:
        return []
    if current is None:
        return list(range(min(rows, total)))
    after = min(total - current - 1, rows - 1)
    before = min(current, rows - 1 - after)
    return list(range(current - before, current + after + 1))


_scrolled = [False]          # a row's title is scrolling: the player keeps redrawing (scrolled())


def scrolled() -> bool:
    """Whether the lists drawn since the last ask had a title scrolling; asks
    once (the next frame says afresh)."""
    was, _scrolled[0] = _scrolled[0], False
    return was


def _build_queue_lines(max_w: int, max_rows: int) -> list[str]:
    """The queue as a list like every other: a heading with the position,
    then the current track near the top (see _queue_window), each row its
    title, who and what it's from (when the queue mixes them) and length. What's been played is dim; the playing track sits on the soft
    bar; the queue keys' cursor on the highlight bar."""
    ctx = _ctx()
    titles, idx, meta = ctx['titles'], ctx['index'], ctx['meta']
    if not titles:
        return [f"{C.DIM}{ctx['label']}{C.RESET}",
                f"{C.DIM}{'No chapters' if ctx is _chapter_ctx else 'Nothing queued'}{C.RESET}"]
    total = len(titles)
    current = idx if 0 <= idx < total else None
    pos = f" · {current + 1} of {total}" if current is not None else f" · {total}"
    out = [f"{C.DIM}{ctx['label']}{pos}{C.RESET}"]
    body_rows = max(0, max_rows - len(out))
    if body_rows <= 0:
        return out
    cursor = ctx['cursor']
    visible = _queue_window(total, current, body_rows)
    if cursor is not None and cursor not in visible:              # follow the cursor out
        visible = _queue_window(total, cursor, body_rows)
    ctx['visible'] = visible                         # row i+1 ↔ list position, for clicks

    chapters = ctx is _chapter_ctx
    same_album = _queue_all_same_album(meta)
    compilation = same_album and _queue_is_compilation_without_album_artist(meta)
    detail = not chapters and (_queue_should_show_artist(meta) or _queue_should_show_album(meta))
    cells = []
    for i in visible:
        item = meta[i] if i < len(meta) else {'title': titles[i]}
        row = [item.get('title', '')]
        if detail:
            row.append(ui.strip_ansi(_queue_meta_value(item, same_album, compilation)))
        if not chapters:
            row.append(ui.format_time(int(item['duration'])) if item.get('duration') else "")
        cells.append(row)
    specs = _queue_column_specs(chapters, detail)
    widths = _table_widths(cells, specs, max_w, pointer_w=0, right_margin=0)
    # The row in focus (the cursor's, else the playing one) scrolls a title
    # too long for its room, as a list's highlighted row does.
    focus = cursor if cursor is not None else current
    if focus in visible and widths and ui.visual_len(cells[visible.index(focus)][0]) > widths[0] > 0:
        row = cells[visible.index(focus)]
        row[0] = ui.marquee(row[0], widths[0], time.monotonic())
        _scrolled[0] = True
    for i, row in zip(visible, cells):
        kind = 'prev' if current is not None and i < current else 'current' if i == current else 'next'
        out.append(_render_queue_row(row, specs, widths, kind, max_w, highlight=i == cursor))
    return out


def _normalize_album_name(name: str) -> str:
    return re.sub(r'\s+', ' ', name.strip().casefold())


def _queue_all_same_album(meta: list[dict]) -> bool:
    albums = [_normalize_album_name(item['album']) for item in meta if item.get('album')]
    return len(albums) > 0 and len(set(albums)) == 1


def _queue_is_compilation_without_album_artist(meta: list[dict]) -> bool:
    if any(item.get('album_artist') for item in meta):
        return False
    artists = [item['artist'] for item in meta if item.get('artist')]
    return len(set(artists)) > 1


def _queue_should_show_artist(meta: list[dict]) -> bool:
    """Whether the queue needs an artist. On a one-album queue, only when some
    track's artist differs from the album artist (or it is a compilation with no
    album artist); otherwise when the artists vary or any differs from its album
    artist."""
    if _queue_all_same_album(meta):
        return any(
            item.get('artist') and item.get('album_artist') and item['artist'] != item['album_artist']
            for item in meta
        ) or _queue_is_compilation_without_album_artist(meta)

    artists = [item['artist'] for item in meta if item.get('artist')]
    if len(set(artists)) > 1:
        return True
    for item in meta:
        if item.get('artist') and item.get('album_artist') and item['artist'] != item['album_artist']:
            return True
    return False


def _queue_meta_value(item: dict, same_album: bool = False, compilation_without_album_artist: bool = False) -> str:
    """The detail beside a queued title. On a one-album queue, the artist only when
    it differs from the album artist (always, on a compilation with no album
    artist); otherwise artist · album."""
    if same_album or compilation_without_album_artist:
        artist = item.get('artist')
        album_artist = item.get('album_artist')
        if not artist:
            return ''
        if compilation_without_album_artist:
            return format_tag_values(artist)
        if not album_artist or artist != album_artist:
            return format_tag_values(artist)
        return ''

    pieces = []
    if item.get('artist'):
        pieces.append(format_tag_values(item['artist']))
    if item.get('album'):
        pieces.append(item['album'])
    return ' · '.join(pieces)


def _queue_should_show_album(meta: list[dict]) -> bool:
    if _queue_all_same_album(meta) or _queue_is_compilation_without_album_artist(meta):
        return False
    albums = [item['album'] for item in meta if item.get('album')]
    return len(set(albums)) > 1


def _queue_column_specs(chapters: bool, detail: bool) -> list[Column]:
    """The queue's columns: the title, who and what it's from (first to go),
    the length against the right edge (the next to go)."""
    if chapters:
        return [Column(flex=True, min_width=10, gap=0)]
    specs = [Column(flex=True, min_width=10, gap=0)]
    if detail:
        specs.append(Column(flex=True, min_width=8, priority=1, gap=3))
    return specs + [Column(align='right', pin=True, priority=2, gap=2)]


def _render_queue_row(cells: list, specs: list[Column], widths: list[int], kind: str,
                      width: int, highlight: bool = False) -> str:
    """One queue row `width` wide: the title as bright as the row matters
    (dim once played, bold while playing), everything else dim; the playing
    row on the soft bar, the cursor's on the highlight bar."""
    title_at = 0
    left, right = "", ""
    for i, (spec, w) in enumerate(zip(specs, widths)):
        if w < 0:
            continue
        raw = cells[i] if i < len(cells) else ""
        # Cut as plain text, so the "…" takes the cell's style (dim, or bold
        # on the playing row) with the rest.
        text = ui.truncate_text(ui.strip_ansi(raw), w) if ui.visual_len(raw) > w else raw
        text = text.rjust(w) if spec.align == 'right' else text + " " * max(0, w - ui.visual_len(text))
        style = (C.DIM if kind == 'prev' or i != title_at
                 else C.BOLD if kind == 'current' else "")
        piece = " " * (spec.gap if left or right else 0) + (f"{style}{text}{C.RESET}" if style else text)
        if spec.pin:
            right += piece
        else:
            left += piece
    line = left + " " * max(0, width - ui.visual_len(left) - ui.visual_len(right)) + right
    if highlight:
        return ui.on_bar(line, width)
    if kind == 'current':
        return ui.on_bar(line, width, C.BAR_DIM)
    return line
