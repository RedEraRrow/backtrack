"""The player laid out across window sizes: nothing overlaps or leaves the
screen, the art has equal gaps either side, and within a layout a bigger
window never gets smaller art, beyond the one column even sides can take."""
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

    def _draw(self, cols, rows, help_on, pane):
        pu._ui_state.update(show_lyrics=pane == 'lyrics', show_queue=pane == 'queue',
                            show_credits=False)
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(pc, 'hints_visible', lambda: help_on), \
             patch.object(sys, 'stdout', io.StringIO()):
            prog, ctrl, *_ = pu._draw_default_ui(self.cover, _Audio(), None, (cols, rows))
            hints = len(pu._controls_line(False, False, 100, "", has_lyrics=False,
                                          has_credits=False)[1].splitlines())
        box = min(cols // 2, pu.ART_MAX_WIDTH) if pu._layout_mode(cols) == 'wide' and pane else cols
        return dict(w=geom.art_width or 0, h=geom.art_height or 0, top=geom.art_top or 0,
                    left=geom.art_left or 0, box=box, qrows=len(pu._queue_click_rows),
                    prog=prog, ctrl=ctrl, hints=hints,
                    mode=pu._layout_mode(cols) if pane else 'standard')   # wide is the split, with a pane

    def test_every_size(self):
        with patch.object(pa, 'inline_art_enabled', lambda: False), \
             patch.object(pu, '_meta_left_lines', lambda *a: ["Title", "Artist", "Album"]):
            for help_on in (False, True):
                for pane in (None, 'lyrics', 'queue'):
                    grid = {}
                    for cols in range(30, 200, 7):
                        for rows in range(12, 80, 5):
                            g = grid[cols, rows] = self._draw(cols, rows, help_on, pane)
                            where = f"{cols}x{rows} help={help_on} pane={pane} {g}"
                            self.assertLessEqual(g['w'], cols, where)
                            self.assertEqual(bool(g['w']), bool(g['h']), where)
                            if g['h']:
                                self.assertLess(g['top'] + g['h'] - 1, g['prog'], where)
                                self.assertEqual(g['left'], g['box'] - g['left'] - g['w'], where)
                            self.assertLess(g['prog'], g['ctrl'], where)
                            self.assertGreaterEqual(g['ctrl'], 1, where)
                            if g['h']:
                                # the art sits at the top: only the space under
                                # everything (or a panel) grows with the window
                                self.assertEqual(g['top'], 1 if g['mode'] == 'standard' else 1 + ui.MARGIN_V, where)
                            if pane == 'queue' and g['mode'] == 'standard' and g['h']:
                                # its minimum is kept before the art has any room
                                self.assertGreaterEqual(g['qrows'], 1, where)
                    for (cols, rows), g in grid.items():
                        for nxt in ((cols + 7, rows), (cols, rows + 5)):
                            h = grid.get(nxt)
                            if h and h['mode'] == g['mode'] and h['hints'] == g['hints'] and g['h']:
                                self.assertGreaterEqual(h['w'], g['w'] - 1, f"{cols}x{rows} -> {nxt} {pane}")


if __name__ == '__main__':
    unittest.main()
