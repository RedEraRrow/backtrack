"""The player's queue pane: the list around the current track, its layout,
and the rows a click can play."""
from __future__ import annotations
import re
from src.music_library import get_metadata, format_tag_values, live_library
from src.utils import ui_utils
from src.utils.prompt_core import Column, _table_widths
from src.utils.ui_utils import Colors as C
from src.utils.log import quietly


# Up-next context for the queue view: list of display titles + current index.
_queue_ctx: dict = {'titles': [], 'paths': [], 'index': 0, 'meta': [], 'visible': []}


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
                data = by_path.get(paths[i]) or get_metadata(paths[i])
                item['title'] = data.get('title') or item['title']
                item['artist'] = data.get('artist') or ''
                item['album'] = data.get('album') or ''
                item['album_artist'] = data.get('album_artist') or ''
        meta.append(item)
    return meta


def has_queue() -> bool:
    """Return whether there's more than one track in the queue worth showing."""
    return len(_queue_ctx['titles']) > 1


_QUEUE_PLAYED_ABOVE = 2     # played tracks kept above the current one, for context


_QUEUE_RIGHT_MARGIN = 2 * ui_utils.MARGIN_H   # breathing room before the screen edge


_QUEUE_MIN_ROWS = 2         # the header and one track, or the pane isn't drawn at all


# Screen row → (queue position, first column, last column) of each track row
# drawn, so a click on one can play it.
_queue_click_rows: dict[int, tuple[int, int, int]] = {}


def queue_click_index(row: int, col: int) -> int | None:
    """The queue position of the track row clicked at (row, col), if any."""
    hit = _queue_click_rows.get(row)
    return hit[0] if hit and hit[1] <= col <= hit[2] else None


def _place_queue(log, top: int, left: int, width: int, rows: int) -> bool:
    """Draw the queue at (top, left) within width × rows, or nothing when there
    isn't room for even the header and one track. Returns whether it drew."""
    _queue_click_rows.clear()
    if rows < _QUEUE_MIN_ROWS or width < 10:
        return False
    lines = _build_queue_lines(width - _QUEUE_RIGHT_MARGIN, rows)
    for qi, line in enumerate(lines):
        log(f"\033[{top + qi};{left}H{line}")
        if qi and qi - 1 < len(_queue_ctx['visible']):
            _queue_click_rows[top + qi] = (_queue_ctx['visible'][qi - 1], left,
                                           left + ui_utils.visual_len(line) - 1)
    return True


def _queue_window(total: int, current: int | None, rows: int) -> list[int]:
    """Which queue positions fit in `rows`: the current track near the top with
    up to _QUEUE_PLAYED_ABOVE played ones above it, then what's next; when the
    end of the queue leaves room, more of what was played fills it."""
    if rows <= 0 or total <= 0:
        return []
    if current is None:
        return list(range(min(rows, total)))
    start = max(0, current - _QUEUE_PLAYED_ABOVE)
    start = max(0, min(start, total - rows))   # use spare rows at the end for history
    return list(range(start, min(total, start + rows)))


def _build_queue_lines(max_w: int, max_rows: int) -> list[str]:
    """Render the play queue: a header with the position, then the current
    track near the top (see _queue_window)."""
    titles = _queue_ctx['titles']
    idx = _queue_ctx['index']
    meta = _queue_ctx['meta']
    if not titles:
        return [f"{C.DIM}(queue empty){C.RESET}"]

    total = len(titles)
    current = idx if 0 <= idx < total else None
    pos = f"  {current + 1} of {total}" if current is not None else f"  {total}"
    out = [f"{C.DIM}QUEUE{pos}{C.RESET}"]
    body_rows = max(0, max_rows - len(out))
    if body_rows <= 0:
        return out
    visible_indices = _queue_window(total, current, body_rows)
    _queue_ctx['visible'] = visible_indices          # row i+1 ↔ queue position, for clicks

    show_artist = _queue_should_show_artist(meta)
    show_album = _queue_should_show_album(meta)
    cols = ['meta'] if show_artist or show_album else []

    rows: list[str] = []
    same_album = _queue_all_same_album(meta)
    same_album_compilation = same_album and _queue_is_compilation_without_album_artist(meta)
    rows_cells = []
    row_kinds = []
    prefixes = []
    for item_idx in visible_indices:
        item = meta[item_idx] if item_idx < len(meta) else {'title': titles[item_idx], 'artist': '', 'album': '', 'album_artist': ''}
        prefix = f"{C.ACCENT}▶ {C.RESET}" if item_idx == current else "  "
        if current is None:
            row_kind = 'next'
        elif item_idx < current:
            row_kind = 'prev'
        elif item_idx == current:
            row_kind = 'current'
        else:
            row_kind = 'next'
        rows_cells.append([
            item.get('title', ''),
            ui_utils.strip_ansi(_queue_meta_value(item, same_album, same_album_compilation))
        ])
        row_kinds.append(row_kind)
        prefixes.append(prefix)

    specs = _queue_column_specs(cols)
    # The ▶ / blank marker takes 2 columns ahead of every row: leave room for
    # it, or each row runs 2 past the pane and wraps into the next screen row.
    widths = _table_widths(rows_cells, specs, max_w, pointer_w=2, right_margin=0)

    for item_idx, row_kind, prefix in zip(visible_indices, row_kinds, prefixes):
        item = meta[item_idx] if item_idx < len(meta) else {'title': titles[item_idx], 'artist': '', 'album': '', 'album_artist': ''}
        meta_text = _queue_meta_value(item, same_album, same_album_compilation)
        line = _render_queue_row(item, cols, specs, widths, row_kind, prefix, meta_text)
        rows.append(line)

    out.extend(rows)
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
    artist); otherwise artist - album."""
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
        album = item['album']
        pieces.append(f"{C.DIM}{C.ITALIC}{album}{C.RESET}")
    return ' - '.join(pieces)


def _queue_should_show_album(meta: list[dict]) -> bool:
    if _queue_all_same_album(meta) or _queue_is_compilation_without_album_artist(meta):
        return False
    albums = [item['album'] for item in meta if item.get('album')]
    return len(set(albums)) > 1


def _queue_column_specs(columns: list[str]) -> list[Column]:
    if not columns:
        return [Column(style='normal', align='left', flex=True, min_width=10, max_frac=1.0, gap=0)]

    return [
        Column(style='normal', align='left', flex=False, min_width=6, max_width=40, max_frac=0.65, gap=0),
        Column(style='normal', align='left', flex=True, min_width=10, max_width=None, max_frac=1.0, priority=1),
    ]


def _render_queue_row(item: dict, columns: list[str], specs: list[Column], widths: list[int], row_kind: str, prefix: str, meta_text: str = '') -> str:
    title = item.get('title', '')
    values = [title, meta_text] if columns else [title]

    if row_kind == 'current':
        title_style = f"{C.BOLD}{C.WHITE}"
        other_style = f"{C.BOLD}{C.DIM}"
    elif row_kind == 'prev':
        title_style = C.DIM
        other_style = C.DIM
    else:
        title_style = C.WHITE
        other_style = C.DIM

    row = prefix
    first = True
    for i, width in enumerate(widths):
        if width < 0:
            continue
        raw = values[i] if i < len(values) else ''
        if ui_utils.visual_len(raw) > width:
            if width <= 1:
                text = ui_utils.clip_ansi(raw, width)
            else:
                clipped = ui_utils.clip_ansi(raw, max(0, width - 1))
                if clipped.endswith(C.RESET):
                    clipped = clipped[:-len(C.RESET)]
                text = clipped + '…'
        else:
            text = raw
        text = text + ' ' * max(0, width - ui_utils.visual_len(text))
        styled = f"{title_style if i == 0 else other_style}{text}{C.RESET}"
        if first:
            row += styled
            first = False
        else:
            row += ' ' * specs[i].gap + styled
    return row
