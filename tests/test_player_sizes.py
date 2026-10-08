"""The player laid out across window sizes: nothing overlaps or leaves the
screen, the art has equal gaps either side, and within a layout a bigger
window never gets smaller art, beyond the one column even sides can take.
With the tab bar showing, the art starts under it."""
import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from backbone import ui
from backbone.prompt import core as pc
from backtrack.playback import player_art as pa, player_ui as pu
from backtrack.playback.player_geom import geom


class _Audio:
    def getall(self, key):
        return []


class PlayerSizesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.cover = os.path.join(cls.dir.name, "cover.png")
        Image.new("RGB", (500, 500), (90, 90, 90)).save(cls.cover)
        from backtrack.playback import queue_pane
        queue_pane.set_queue_context([f"Track {i}" for i in range(12)], 2,
                                     [f"/m/{i}.mp3" for i in range(12)])

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()

    def _draw(self, cols, rows, help_on, pane, tabs):
        arr = [[pane, 'right']] if pane else []
        pu._ui_state.update(panels={'wide': [list(x) for x in arr], 'tall': [list(x) for x in arr]},
                            arranging=None, show_lyrics=pane == 'lyrics', show_queue=pane == 'queue',
                            show_credits=False)
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(ui, 'tab_rows', lambda: 3 if tabs else 0), \
             patch.object(pc, 'hints_visible', lambda: help_on), \
             patch.object(sys, 'stdout', io.StringIO()):
            prog, ctrl, *_ = pu._draw_default_ui(self.cover, _Audio(), None, (cols, rows))
            hints = len(pu._controls_line(False, False, 100, has_lyrics=False,
                                          has_credits=False)[1].splitlines())
        boxed = pu._frame['boxed']
        if boxed:
            # Measured inside the player's box: the art centred in it.
            player = pu._frame['boxes'][0]
            inner, first = player[3] - player[2] - 3, player[2] + 2
            box, left = inner, (geom.art_left or 0) + 1 - first
            mode = 'side' if pu._frame['side'] else 'stacked'
            top_expected = player[0] + 1
        else:
            inner, left = cols, geom.art_left or 0
            box = min(inner // 2, pu.ART_MAX_WIDTH) if pu._layout_mode(cols) == 'wide' and pane else inner
            mode = pu._layout_mode(cols) if pane else 'standard'   # wide is the split, with a pane
            top_expected = None
        return dict(w=geom.art_width or 0, h=geom.art_height or 0, top=geom.art_top or 0,
                    left=left, box=box, qrows=len(pu._queue_click_rows), beside=bool(boxed and pu._frame.get('beside')),
                    boxed=boxed, inner=inner, top_expected=top_expected,
                    prog=prog, ctrl=ctrl, hints=hints, mode=mode)

    def test_every_size(self):
        with patch.object(pa, 'inline_art_enabled', lambda: False), \
             patch.object(pu, '_meta_left_lines', lambda *a: ["Title", "Artist", "Album"]):
            for help_on, pane, tabs in ((h, p, t) for h in (False, True) for p in (None, 'lyrics', 'queue')
                                    for t in (False, True)):
                grid = {}
                for cols in range(30, 200, 7):
                    for rows in range(12, 80, 5):
                        g = grid[cols, rows] = self._draw(cols, rows, help_on, pane, tabs)
                        where = f"{cols}x{rows} help={help_on} pane={pane} tabs={tabs} {g}"
                        self.assertLessEqual(g['w'], cols, where)
                        self.assertEqual(bool(g['w']), bool(g['h']), where)
                        if g['h']:
                            self.assertLess(g['top'] + g['h'] - 1, g['prog'], where)
                        if g['h'] and not g['beside']:
                            gaps = g['left'], g['box'] - g['left'] - g['w']
                            # even sides (boxed: within a column, the box being the art's own width)
                            self.assertLessEqual(abs(gaps[0] - gaps[1]), 1 if g['boxed'] else 0, where)
                        # Boxed, the progress bar shares the transport row with the controls.
                        (self.assertEqual if g['boxed'] else self.assertLess)(g['prog'], g['ctrl'], where)
                        self.assertGreaterEqual(g['ctrl'], 1, where)
                        if g['h'] and g['boxed']:
                            self.assertGreaterEqual(g['top'], g['top_expected'], where)    # inside its box
                        elif g['h']:
                            # the art sits at the top: only the space under
                            # everything (or a panel) grows with the window.
                            # Full-width art starts on the first row; art with
                            # room beside it (the volume bar) keeps a margin,
                            # and the tab bar is that margin.
                            full_width = g['mode'] == 'standard' and g['w'] == g['inner']
                            above = max(3 if tabs else 0, 0 if full_width else ui.MARGIN_V)
                            self.assertEqual(g['top'], 1 + above, where)
                        if pane == 'queue' and g['mode'] in ('standard', 'stacked') and g['h']:
                            # its minimum is kept before the art has any room
                            self.assertGreaterEqual(g['qrows'], 1, where)
                for (cols, rows), g in grid.items():
                    for nxt in ((cols + 7, rows), (cols, rows + 5)):
                        h = grid.get(nxt)
                        if h and h['mode'] == g['mode'] and h['hints'] == g['hints'] and h['boxed'] == g['boxed'] and g['h']:
                            self.assertGreaterEqual(h['w'], g['w'] - 1, f"{cols}x{rows} -> {nxt} {pane}")


if __name__ == '__main__':
    unittest.main()
