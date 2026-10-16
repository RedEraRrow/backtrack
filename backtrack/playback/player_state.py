"""The player screen's shared state: what it shows and how (_ui_state), and
what the last frame drew (_frame). Here so the drawing, the panels and the
transport can each read it without importing one another."""
from __future__ import annotations


# The boxed player's frame (_draw_boxed_ui): whether the last frame was boxed,
# its boxes (top, bottom, left, right; screen rows and columns) and their
# titles, the transport row (`prog_row`: the controls, the progress bar and
# the times, drawn by update_progress_ui and write_controls, not the frame),
# the column its controls start at, and the bar's (first column, width).
_frame: dict = {'boxed': False, 'boxes': [], 'titles': {}, 'prog_row': None,
                'icons_col': None, 'bar': None}


_ui_state = {
    'show_metadata': True,      # from player_show_metadata; `m` flips and saves it
    'show_tabs': True,          # from player_show_tabs; `f` flips and saves it
    'debug': False,
    # The panels shown (see PANELS), and where: for each shape of window
    # ('wide': in columns beside the player; 'tall': under it), the panels in
    # order, each on a side ('left' / 'right'). From player_panels; `w` picks
    # them, its Arrange moves them. show_* follow it (set_panels).
    'panels': {'wide': [], 'tall': []},
    'arranging': None,          # the panel being moved (Arrange), or None
    'show_credits': False,
    'show_lyrics': False,
    'show_queue': False,
    'scrolling': False,         # a title line is too long and scrolls (ui.marquee): redraw it
    'chapter_title': '',        # the chapter playing: the title line shows it
    'chapter_pos': '',          # its "Chapter 3 of 20", for the details line
    'book': False,              # the track is an audiobook (speed keys apply)
    # False in a joined window's player view: lyrics are only painted by the
    # window playing the audio, so there the panel offers credits and the queue.
    'lyrics_pane': True,
    'nerd_icons': False,        # from player_nerd_font_icons
}
