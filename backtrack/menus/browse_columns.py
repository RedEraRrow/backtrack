"""The column browser's last column (select(preview=…)): the highlighted row's
picture (cover, artist portrait), its details, then what opening it lists.
Browse and Directory build theirs from these."""
from __future__ import annotations

from backbone import prompt, ui
from backbone.ui import Colors as C
from backtrack.music_library import (
    album_credit, chapter_label, format_label, format_tag_values, to_num, track_title, year_of,
)
from backtrack.album_art import artist_image, round_portrait
from backtrack.playback.player_art import _SHAPES, AVATAR_ART, IDLE_ART, _art_fit, fill_art, image_cells

_ART_MIN_H = 3
_INFO_ROWS = 7           # the details' lines: name, credit, years, size, length, format, copyright


def art_box(w: int, h: int) -> tuple[int, int]:
    """The picture's box in a details box w × h: (rows, columns), square on
    screen, as big as the box allows with the details' lines under it; (0, 0)
    when that's too small to be worth it. It depends on the box alone, so it
    keeps one size whatever is highlighted."""
    aspect = ui.cell_aspect()
    full = round(w / aspect)                    # a square as wide as the box
    rows = min(full, h - 1 - _INFO_ROWS)
    if rows < _ART_MIN_H:
        return 0, 0
    # The box's whole width when there's height for it (the cover is cropped
    # to fill, so a column either way doesn't show); else as wide as the rows allow.
    return rows, w if rows == full else min(w, round(rows * aspect))


def _picture(art_path, art_w: int, art_h: int) -> tuple[list, tuple | None]:
    """The picture in a box art_w × art_h: (its lines, the image escape that
    goes over them, if it's shown as an image). A picture fills the box with
    no gap (cropped about its centre to it); a placeholder sits centred."""
    shape = art_path if art_path in _SHAPES else None
    image = image_cells(art_path, art_w, art_h) if not shape else None
    if image:
        return image[0], image[1]
    fit = fill_art(art_path, art_w, art_h) if not shape else []
    if not fit:                                              # a placeholder, or no picture to show
        fit = _art_fit("", art_w, art_h, shape or IDLE_ART)[1]
    fw = max((ui.visual_len(a) for a in fit), default=0)
    top = (art_h - len(fit)) // 2
    left = " " * ((art_w - fw) // 2)
    lines = [""] * top + [left + a for a in fit] + [""] * (art_h - top - len(fit))
    return [ln + " " * max(0, art_w - ui.visual_len(ln)) for ln in lines], None


def _details_drawer(art_path, info: list):
    """Draws the details in a space w × h: the picture over the facts, or in
    a wide, short strip, beside them."""
    def draw(w: int, h: int) -> prompt.Pane:
        facts = ([f"{C.BOLD}{info[0]}{C.RESET}"] if info else []) + [f"{C.DIM}{x}{C.RESET}" for x in info[1:]]
        if w >= 4 * h:                                    # a strip: the picture at the left
            art_h = h if art_path and h >= _ART_MIN_H else 0
            art_w = min(round(art_h * ui.cell_aspect()), w // 3) if art_h else 0
            art_h = min(art_h, round(art_w / ui.cell_aspect()))     # still square: never cut
            art_w = art_w if art_h >= _ART_MIN_H else 0
            art, esc = _picture(art_path, art_w, art_h) if art_w else ([], None)
            room = w - art_w - (2 if art_w else 0)
            lines = [((art[k] if k < len(art) else " " * art_w) + "  " if art_w else "")
                     + (ui.truncate_text(facts[k], room) if k < len(facts) else "")
                     for k in range(min(h, max(len(facts), art_h)))]
            pics = [(0, 0, art_h, (art_path, art_w, art_h), esc, art_w)] if esc else []
            return prompt.Pane(lines, pics)
        art_h, art_w = art_box(w, h) if art_path else (0, 0)
        art, esc = _picture(art_path, art_w, art_h) if art_h else ([], None)
        pad = " " * max(0, (w - art_w) // 2)
        lines = [pad + a for a in art] + ([""] if art else [])
        lines += [ui.truncate_text(x, w) for x in facts] + [""] * (_INFO_ROWS - len(facts))   # one height
        pics = [(0, len(pad), art_h, (art_path, art_w, art_h), esc, art_w)] if esc else []
        return prompt.Pane(lines[:h], pics)
    return draw


def preview(details: tuple | None, contents_title: str = "", contents: list | None = None) -> prompt.Preview:
    """The column browser's preview: `details` (its picture: a file with a
    cover, an image file, AVATAR_ART or None; its lines, the first the name)
    and `contents`, what opening the row lists. The picture is a real image
    where the terminal and the setting allow, else text art."""
    contents = [str(x) for x in contents or []]
    art_path, info = details or (None, [])
    info = [str(x) for x in info[:_INFO_ROWS]]
    want = max((ui.visual_len(x) for x in info + contents), default=0)
    return prompt.Preview(_details_drawer(art_path, info) if details else None, contents_title, contents, want,
                          details_rows=_details_rows if details and art_path else None)


def _details_rows(width: int) -> int:
    """The rows the details want at `width`: a square picture as wide as the
    box, a blank, the facts."""
    return round(width / ui.cell_aspect()) + 1 + _INFO_ROWS


def track_details(t: dict) -> tuple:
    """A track's details: name, who, album and year, where on it, length, format."""
    year = year_of(t.get('date') or t.get('year'))
    album = " · ".join(x for x in (t.get('album') or '', str(year) if year else '') if x)
    track, disc = int(to_num(t.get('track'))), int(to_num(t.get('disc')))
    where = " · ".join(x for x in (f"Track {track}" if track else '', f"Disc {disc}" if disc > 1 else '') if x)
    dur = ui.format_time(int(t['duration'])) if t.get('duration') else ''
    lines = [track_title(t['path'], t), format_tag_values(t.get('artist') or ''), album, where, dur,
             format_label(t), t.get('copyright') or '']
    return t['path'], [x for x in lines if x]


def chapter_details(t: dict, i: int) -> tuple:
    """A book's chapter: its name, where it runs, the book and its format."""
    chapters = t['chapters']
    start = chapters[i][0]
    end = chapters[i + 1][0] if i + 1 < len(chapters) else (t.get('duration') or start)
    lines = [chapter_label(chapters, i)[0], f"{ui.format_time(int(start))} – {ui.format_time(int(end))}",
             t.get('album') or track_title(t['path'], t), format_tag_values(album_credit([t])), format_label(t),
             t.get('copyright') or '']
    return t['path'], [x for x in lines if x]


def tracks_details(name: str, tracks: list, person: bool = False, credit: bool = False) -> tuple:
    """An album or a group: its name, years, size, length and format, and with
    `credit` (an album: a genre or a year has no one artist) who it's by.
    A `person`'s (an artist's) picture is their own, not a cover: their
    artist image, round, else AVATAR_ART."""
    if not tracks:
        return (AVATAR_ART if person else None), [name]
    years = sorted({y for y in (year_of(t.get('date') or t.get('year')) for t in tracks) if y})
    span = (f"{years[0]}" if len(years) == 1 else f"{years[0]}–{years[-1]}") if years else ""
    albums = len({(t.get('album'), t.get('album_artist')) for t in tracks})
    size = ui.plural(len(tracks), "track") + (f" · {ui.plural(albums, 'album')}" if albums > 1 else "")
    total = sum(t.get('duration') or 0 for t in tracks)
    formats = {format_label({**t, 'bitrate': 0}) for t in tracks} - {""}   # VBR differs per track
    rights = {t.get('copyright') or "" for t in tracks}
    lines = [name, format_tag_values(album_credit(tracks)) if credit else "", span, size,
             ui.format_time(int(total)) if total else "",
             formats.pop() if len(formats) == 1 else ("mixed formats" if formats else ""),
             rights.pop() if len(rights) == 1 else ""]   # an album's one copyright, not a group's many
    picture = artist_image([t['path'] for t in tracks]) if person else None
    art = ((picture and round_portrait(picture)) or AVATAR_ART) if person else tracks[0]['path']
    return art, [x for x in lines if x]
