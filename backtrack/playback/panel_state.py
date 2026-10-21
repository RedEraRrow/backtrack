"""The player's panels: which there are, which are on and where each goes
(per window shape), the track's part in them (what it has for each, its
sound, its booklet page), and Arrange's keys. The drawing is player_ui's."""
from __future__ import annotations

import time

from backbone import keys
from backtrack.config import save_setting
from backtrack.playback.player_geom import geom
from backtrack.playback.player_state import _frame, _ui_state
from backtrack.playback import queue_pane
from backtrack.playback.queue_pane import has_queue, set_queue_cursor, use_list


def _get_people(audio, tag_key: str) -> list[tuple[str, str]]:
    """Flatten an ID3 involved-people-list frame (e.g. TMCL/TIPL) into (role,
    name) pairs, each as the tag has it: shown as typed, compared lowercased."""
    return [
        (role.strip(), name.strip())
        for frame in audio.getall(tag_key)
        for role, name in frame.people
    ]


# The panels the player can show beside (or under) itself, in the order `w` lists them.
PANELS = (('lyrics', "Lyrics"), ('people', "People"), ('queue', "Queue"), ('chapters', "Chapters"),
          ('visualiser', "Visualiser"), ('credits', "Credits"), ('info', "Info"), ('notes', "Notes"), ('booklet', "Booklet"))


# Where a panel can go: full-width bands, the columns beside the player, and
# its own column above and below its art and details.
PLACES = ('top', 'left', 'above', 'below', 'right', 'bottom')


_VERTICAL = ('top', 'above', 'below', 'bottom')                # ↑ / ↓ move a panel along these


_LIVE_PANELS = ('visualiser',)                       # repainted every tick (panels.LIVE)


# This track, as the newer panels need it (set_track_context): its length,
# its sound once the visualiser shows, its booklet page.
_track_ctx: dict = {'path': '', 'duration': 0.0, 'sound': None, 'page': 0, 'started': 0.0}


def set_track_context(path: str, duration: float) -> None:
    """What the panels need of the track now playing (the player, on each load)."""
    if path != _track_ctx['path']:
        _track_ctx.update(page=0, started=time.time())    # when this play began, for Info's last play before it
    _track_ctx.update(path=path, duration=duration, sound=None)


def live_context() -> dict:
    """What the visualiser draws from (panels.Live.paint)."""
    from backtrack.playback import sound
    snd = _track_ctx['sound']
    if snd is None and 'visualiser' in shown_panels():
        snd = _track_ctx['sound'] = sound.for_track(_track_ctx['path'], _track_ctx['duration'])
    if snd is not None and snd.done:                 # this one's ready: the next in the queue, so it's seamless
        paths, at = queue_pane._queue_ctx['paths'], queue_pane._queue_ctx['index']
        nxt = paths[at + 1] if 0 <= at < len(paths) - 1 else None
        if nxt and nxt != _track_ctx.get('prefetched'):
            _track_ctx['prefetched'] = nxt
            from backtrack.music_library import library_entry
            sound.prefetch(nxt, float((library_entry(nxt) or {}).get('duration') or 0.0))
    return {'sound': snd, 'style': _ui_state.get('visualiser', 'bars')}


def people_of(audio) -> tuple[list, list]:
    """The People panel's (cast, crew): TMCL and TIPL, and with Settings →
    People: all credit tags, the composer, lyricist and the rest from their
    own tags too, among the crew."""
    from backtrack.playback import panels
    cast, crew = _get_people(audio, 'TMCL'), _get_people(audio, 'TIPL')
    if _ui_state.get('people_all', True):
        have = {(r.lower(), n.lower()) for r, n in cast + crew}
        crew += [(r, n) for r, n in panels.tag_credits(audio) if (r.lower(), n.lower()) not in have]
    return cast, crew


def credits_repeat(audio) -> bool:
    """Whether the Credits panel would only repeat People: People takes the
    credit tags (the setting) and has no TMCL or TIPL people of its own."""
    return bool(_ui_state.get('people_all', True) and audio is not None
                and not (_get_people(audio, 'TMCL') or _get_people(audio, 'TIPL')))


def track_has(audio, file_path: str, has_cast: bool, has_lyrics: bool) -> dict:
    """Every panel, and whether this track has anything for it: one answer for
    the layout, `w`'s list and whether the player is boxed. Credits that would
    only repeat People count as nothing."""
    from backtrack.playback import panels
    has = {'lyrics': has_lyrics, 'people': has_cast, 'queue': has_queue(),
           'chapters': bool(_chapter_titles()), **panels.has(audio, file_path)}
    if has['credits'] and credits_repeat(audio):
        has['credits'] = False
    return has


def panel_notes(audio) -> dict:
    """Why a panel this track has data for is greyed in `w`'s list."""
    from backtrack.playback import panels
    return {'credits': "repeat data"} if audio is not None and panels.tag_credits(audio) and credits_repeat(audio) else {}


def cycle_visualiser() -> str:
    """The visualiser's next style (bars, oscilloscope, meters), saved."""
    from backtrack.playback.panels import VIS_STYLES
    style = VIS_STYLES[(VIS_STYLES.index(_ui_state.get('visualiser', 'bars')) + 1) % len(VIS_STYLES)]
    _ui_state['visualiser'] = style
    save_setting('player_visualiser', style)
    return style


def turn_page(step: int) -> bool:
    """The booklet's next (1) or previous (-1) page; False when it's showing none."""
    from backtrack.playback import panels
    if 'booklet' not in (_frame.get('panel_order') or []):
        return False
    n = len(panels.booklet_pages(_track_ctx['path']))
    _track_ctx['page'] = (_track_ctx['page'] + step) % max(1, n)
    return True


def page_click(row: int, col: int) -> int | None:
    """A click on the booklet: 1 on its right half, -1 on its left, else None."""
    box = (_frame.get('panel_boxes') or {}).get('booklet')
    if box and box[0] < row < box[1] and box[2] < col < box[3]:
        return 1 if col > (box[2] + box[3]) // 2 else -1
    return None


def shown_panels() -> list[str]:
    """The panels switched on, whatever this track has."""
    return [k for k, _side in _ui_state['panels']['wide']]


def set_panels(kinds: list[str]) -> None:
    """Show these panels: one switched on joins both shapes' arrangements at
    the end, on the right; one switched off leaves both. Saved."""
    for shape in ('wide', 'tall'):
        kept = [[k, side] for k, side in _ui_state['panels'][shape] if k in kinds]
        kept += [[k, 'right'] for k in kinds if k not in [x[0] for x in kept]]
        _ui_state['panels'][shape] = kept
    _panels_changed()


def _panels_changed() -> None:
    """Bring the flags the rest of the player reads in line, pick the list
    the queue keys work on, and remember the arrangement."""
    kinds = shown_panels()
    _ui_state['show_lyrics'] = 'lyrics' in kinds
    _ui_state['show_credits'] = 'people' in kinds
    _ui_state['show_queue'] = 'queue' in kinds or 'chapters' in kinds
    use_list('chapters' if chapters_listed() else 'queue')
    if not _ui_state['show_queue']:
        set_queue_cursor(None)                   # a reopened panel starts on the current item
    save_setting('player_panels', _ui_state['panels'])


def lyrics_laid_out() -> bool:
    """Whether the last frame gave the lyrics a box (they're switched on, the
    track has them, and there was room): the lyric pane paints there."""
    return _ui_state['show_lyrics'] and bool(geom.lyric_width)


def chapters_listed() -> bool:
    """Whether the queue keys and clicks work on the chapters (no queue panel)."""
    return 'chapters' in shown_panels() and 'queue' not in shown_panels()


def arrange_start() -> None:
    """Start moving the panels (Arrange): the next frame picks the first one
    on screen (none on screen: nothing to arrange)."""
    _ui_state['arranging'] = _ARRANGE_FIRST


_ARRANGE_FIRST = '__first__'       # Arrange asked for: the first panel the next frame draws


# While arranging the panels: only these keys (and the panels key, which ends it).
keys.define("player_arrange", "Player: arranging panels", [
    ("next", ("TAB",), "the next panel"),
    ("prev", ("BACKTAB",), "the previous panel"),
    ("up", ("UP",), "move it up, on into the place above"),
    ("down", ("DOWN",), "move it down, on into the place below"),
    ("left", ("LEFT",), "move it to the left"),
    ("right", ("RIGHT",), "move it to the right"),
    ("done", ("ENTER", "ESC"), "done arranging"),
], within=())


def arrange_key(key: str) -> bool:
    """A key while arranging (player_arrange): next / previous panel. Up and
    down move it within its place, and past the end of one into the next, top
    band → above the player → below it → bottom band (from a column, out into
    the band at that end). Left and right move it between the left column,
    the player's own column and the right one. Done, or the panels key, to
    finish (saved). The arrangement moved is this window shape's. Whether it
    was one."""
    kind = _ui_state['arranging']
    if kind is None:
        return False
    act = keys.action(key, 'player_arrange')
    order = _frame.get('panel_order') or []
    arr = _ui_state['panels'][_frame.get('shape') or 'wide']
    at = next((i for i, (k, _s) in enumerate(arr) if k == kind), None)
    if act == 'player_arrange.done' or keys.pressed(key, 'player.panel') or at is None or kind not in order:
        _ui_state['arranging'] = None
        _panels_changed()
    elif act in ('player_arrange.next', 'player_arrange.prev'):
        _ui_state['arranging'] = order[(order.index(kind) + (1 if act == 'player_arrange.next' else -1)) % len(order)]
    elif act in ('player_arrange.up', 'player_arrange.down'):
        place, up = arr[at][1], act == 'player_arrange.up'
        same = [i for i, (_k, s) in enumerate(arr) if s == place]
        j = same.index(at) + (-1 if up else 1)
        if 0 <= j < len(same):
            arr[at], arr[same[j]] = arr[same[j]], arr[at]
        else:                                                    # past the end of its place: into the next
            nxt = ('top' if up else 'bottom') if place in ('left', 'right') else \
                _VERTICAL[max(0, min(len(_VERTICAL) - 1, _VERTICAL.index(place) + (-1 if up else 1)))]
            if nxt != place:
                arr.pop(at)
                # Moving up it comes in last of its new place; moving down, first.
                arr.insert(len(arr) if up else 0, [kind, nxt])
    elif act in ('player_arrange.left', 'player_arrange.right'):
        place, right = arr[at][1], act == 'player_arrange.right'
        if place in ('above', 'below', 'top', 'bottom'):
            arr[at][1] = 'right' if right else 'left'
        elif (place == 'left') == right:              # from a column towards the player
            arr[at][1] = 'below'
    else:
        return True                              # every other key waits while arranging
    return True


def _chapter_titles() -> list:
    from backtrack.playback.queue_pane import _chapter_ctx
    return _chapter_ctx['titles']
