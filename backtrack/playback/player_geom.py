"""Where the player's last frame put things: set by the layout as it draws,
read by whatever needs a position afterwards (the art image, the volume bar,
the progress bar, clicks, the lyric pane). One object shared by the modules
that draw the player, so none of them has to reach into another for it."""


class _Geom:
    def __init__(self) -> None:
        # Artwork: visible width, left pad (columns) where it starts, 1-based
        # top row and height in rows (the volume bar runs down its right side).
        self.art_width: int | None = None
        self.art_left: int | None = None
        self.art_top: int | None = None
        self.art_height: int | None = None
        # Whether the blank row under the art is drawn: the layout gives it up
        # to make the art a column wider (even sides); the volume label sits there.
        self.art_gap: bool = True
        # Explicit 1-based column where the volume bar is drawn (None = no room).
        self.vol_bar_col: int | None = None
        # Right pane in the wide layout: 1-based start column and width.
        self.right_left: int | None = None
        self.right_width: int | None = None
        # The row the lyric pane holds its current line on, or None (see
        # lyric_pane.Geometry.centre): the art's middle, in the wide split.
        self.lyric_centre: int | None = None
        # The lyric pane's own column when it isn't the right pane: 1-based
        # start and width (the standard layout insets it to float).
        self.lyric_left: int | None = None
        self.lyric_width: int | None = None
        # Progress bar from the last update_progress_ui: its row, the 1-based
        # column of its first cell (just past the '[' cap) and its width, so a
        # click on it becomes a seek.
        self.prog_row: int | None = None
        self.prog_col: int | None = None
        self.prog_w: int = 0

    def reset_frame(self) -> None:
        """Clear what each frame lays out afresh (all of it), so a layout that
        doesn't set one (the single column has no side pane) can't inherit the last
        frame's. The progress bar keeps its own, set by update_progress_ui."""
        self.art_width = self.art_left = self.art_top = self.art_height = None
        self.art_gap = True
        self.vol_bar_col = self.right_left = self.right_width = None
        self.lyric_centre = None
        self.lyric_left = self.lyric_width = None


geom = _Geom()
