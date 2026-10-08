"""The player in a tiny window: the transport and nothing else, boxed from 3
rows, the row giving things up in turn (the bar, the length, the time, the
glyphs) rather than cutting any of them short."""
import io
import sys
import unittest
from unittest.mock import patch

from backbone import ui
from backtrack.playback import player_art as pa
from backtrack.playback import player_ui as pu


class _Audio:
    def getall(self, key):
        return []

    def get(self, key):
        return None


def _draw(cols, rows, elapsed=61.0, duration=754.0):
    """The player drawn at cols × rows, then its progress: (the transport row as
    painted, plain; the frame)."""
    out = io.StringIO()
    with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
         patch.object(ui, 'get_terminal_width', lambda *a: cols), \
         patch.object(ui, 'get_terminal_height', lambda *a: rows), \
         patch.object(pa, 'inline_art_enabled', lambda: False), \
         patch.object(pu, '_meta_left_lines', lambda *a: ["Title", "Artist"]), \
         patch.object(sys, 'stdout', out):
        from backbone.prompt import core
        core.screen_invalidate()
        prog = pu._draw_default_ui("", _Audio(), pa.IDLE_ART, (cols, rows))[0]
        pu.update_progress_ui(prog, elapsed, duration, cols)
        row = "".join(ch or "" for _st, ch in core._cells.get(prog, []))
    return row, dict(pu._frame)


class TinyPlayerTest(unittest.TestCase):
    def setUp(self):
        self.icons = patch.dict(pu._ui_state, nerd_icons=False)      # whatever this machine's setting
        self.icons.start()

    def tearDown(self):
        self.icons.stop()

    def test_three_to_five_rows_box_the_transport_alone(self):
        for rows in (3, 4, 5):
            row, frame = _draw(60, rows)
            self.assertEqual(len(frame['boxes']), 1, rows)                  # the transport's, nothing else
            top, bottom = frame['boxes'][0][:2]
            self.assertEqual(bottom - top, 2)
            self.assertIn("1:01 / 12:34", row)

    def test_one_or_two_rows_drop_the_box(self):
        for rows in (1, 2):
            row, frame = _draw(60, rows)
            self.assertEqual(frame['boxes'], [], rows)
            self.assertIn("⏸", row)

    def test_narrowing_gives_things_up_in_turn_never_cut(self):
        seen = []
        for cols in range(70, 7, -1):
            row, frame = _draw(cols, 4)
            self.assertNotIn("…", row, cols)
            text = row.strip(" │╭╮╰╯─")
            stage = ("bar" if "[" in text else "both times" if "/" in text else "position" if ":" in text
                     else f"{frame['icons_n']} glyphs")
            if not seen or seen[-1] != stage:
                seen.append(stage)
        self.assertEqual(seen, ["bar", "both times", "position", "3 glyphs", "2 glyphs", "1 glyphs"])


if __name__ == "__main__":
    unittest.main()
