"""The full player: queue rows fit their pane, the queue keeps the current
track in view, a long queue isn't re-read from disk on every track change,
hint bars stay empty until switched on while the i help toggle still shows,
a boxed header keeps the toggle inside its corners, and the shared painter
repaints everything after a terminal resize."""
import unittest
from unittest.mock import patch

from backtrack.playback import player_ui
from backtrack.playback import queue_pane as qp
from backbone import ui
from backbone.prompt import core as pc


class QueueTest(unittest.TestCase):
    def _set(self, n, index):
        titles = [f"Track {i} with a fairly long title to force clipping" for i in range(n)]
        lib = [{'path': f'/m/{i}.mp3', 'title': titles[i], 'artist': f'Artist {i % 3}',
                'album': f'Album {i % 2}', 'album_artist': ''} for i in range(n)]
        with patch.object(qp, 'live_library', lambda: lib), \
             patch.object(qp, 'get_metadata', side_effect=AssertionError("read from disk")):
            qp.set_queue_context(titles, index, [t['path'] for t in lib])

    def test_rows_fit_the_pane_including_the_marker(self):
        self._set(30, 5)
        for line in qp._build_queue_lines(40, 12):
            self.assertLessEqual(ui.visual_len(line), 40, ui.strip_ansi(line))

    def test_current_track_near_the_top_with_position(self):
        self._set(40, 9)
        lines = [ui.strip_ansi(l) for l in qp._build_queue_lines(80, 8)]
        self.assertIn("10 of 40", lines[0])
        self.assertTrue(lines[3].startswith("▶"))           # 2 played tracks above it
        self.assertIn("Track 7", lines[1])

    def test_not_drawn_without_room_for_the_header_and_a_track(self):
        self._set(10, 0)
        out = []
        self.assertFalse(qp._place_queue(out.append, 20, 3, 60, 1))
        self.assertEqual(out, [])
        self.assertIsNone(qp.queue_click_index(20, 5))

    def test_margin_on_the_right_and_rows_are_clickable(self):
        self._set(10, 4)
        out = []
        self.assertTrue(qp._place_queue(out.append, 10, 3, 50, 6))
        for line in out:
            body = line.split('H', 1)[1]
            self.assertLessEqual(ui.visual_len(body), 50 - qp._QUEUE_RIGHT_MARGIN)
        self.assertIsNone(qp.queue_click_index(10, 5))       # the header row
        first = qp._queue_ctx['visible'][0]
        self.assertEqual(qp.queue_click_index(11, 3), first)
        self.assertIsNone(qp.queue_click_index(11, 2))        # left of the pane

    def test_end_of_queue_fills_with_history(self):
        self.assertEqual(qp._queue_window(10, 9, 5), [5, 6, 7, 8, 9])
        self.assertEqual(qp._queue_window(10, 0, 5), [0, 1, 2, 3, 4])

    def test_next_track_reuses_the_details(self):
        self._set(5, 0)
        meta = qp._queue_ctx['meta']
        qp.set_queue_context(list(qp._queue_ctx['titles']), 1, list(qp._queue_ctx['paths']))
        self.assertIs(qp._queue_ctx['meta'], meta)


class PlayerHelpHintTest(unittest.TestCase):
    def test_hints_off_still_leave_i_help_in_the_players_bar(self):
        pc._hints_on[0] = False
        _status, hints = player_ui._controls_line(False, False, 50, "")
        self.assertIn("[i] help", ui.strip_ansi(hints))

    def test_hints_on_show_hide_help_among_the_rest(self):
        pc._hints_on[0] = True
        try:
            _status, hints = player_ui._controls_line(False, False, 50, "")
            self.assertIn("hide help", ui.strip_ansi(hints))
            self.assertIn("prev/next", ui.strip_ansi(hints))
        finally:
            pc._hints_on[0] = False


class UniversalHintsTest(unittest.TestCase):
    def setUp(self):
        # toggle_hints remembers the switch in a file: keep it out of the real config.
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.mkdtemp()
        self._file = patch.object(pc, '_hints_file', lambda: Path(self._tmp) / "hints_on")
        self._file.start()

    def tearDown(self):
        import shutil
        self._file.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)
        pc._hints_on[0] = False

    def test_every_hint_bar_is_empty_until_switched_on(self):
        pc._hints_on[0] = False
        self.assertEqual(pc._hint(("x", "shuffle"), ("q", "quit app")), "")
        pc._hints_on[0] = True
        self.assertIn("shuffle", ui.strip_ansi(pc._hint(("x", "shuffle"))))

    def test_top_line_carries_a_clickable_corner_and_i_only_where_free(self):
        from backbone import prompt
        with patch.object(ui, 'get_terminal_width', lambda: 60):
            cells: dict = {}
            out = prompt.append_chrome(["  Artists"], [("x", "shuffle")], cells, pin=False, i_key=True)
            self.assertTrue(ui.strip_ansi(out[0]).rstrip().endswith("[i] help"))
            row = 1 + ui.MARGIN_V
            toggles = [k for k, v in cells.items() if v == pc.HINTS_CLICK]
            plain = ui.strip_ansi(out[0])
            self.assertEqual(toggles, [(row, plain.index("[i]") + 2)])        # just the `i`
            self.assertIs(prompt.consume_chrome('i', cells), prompt.CHROME_REDRAW)
            self.assertTrue(pc.hints_visible())
            typed: dict = {}
            out = prompt.append_chrome(["  Name"], [], typed, pin=False)      # a text field
            self.assertIsNone(prompt.consume_chrome('i', typed))              # i stays a letter
            col = ui.strip_ansi(out[0]).index("[i]") + 2
            self.assertIs(prompt.consume_chrome(f"MOUSE_CLICK:0:{row}:{col}", typed),
                          prompt.CHROME_REDRAW)                              # but clicking it works


class BoxedHeaderTest(unittest.TestCase):
    def test_toggle_sits_inside_the_box_and_the_corners_survive(self):
        from backbone import prompt
        for width in (60, 100):
            with patch.object(ui, 'get_terminal_width', lambda w=width: w):
                cells: dict = {}
                lines = prompt.rounded_header("A Very Long Episode Title Indeed", " · Some Artist",
                                              "[MP3]  3:21  4.2 MB")
                out = prompt.append_chrome(list(lines), [], cells, pin=False, i_key=True)
            plain = [ui.strip_ansi(l) for l in out]
            self.assertTrue(plain[0].rstrip().endswith("╮"), plain[0])        # untouched top border
            self.assertTrue(plain[1].rstrip().endswith("[i] help │"), plain[1])
            for line in plain[:3]:
                self.assertLessEqual(ui.visual_len(line), width)
            col = plain[1].index("[i]") + 2
            self.assertEqual([k for k, v in cells.items() if v == pc.HINTS_CLICK],
                             [(2 + ui.MARGIN_V, col)])                   # its `i`, on the title row


class PainterResizeTest(unittest.TestCase):
    def test_a_resize_wipes_and_repaints_unchanged_rows(self):
        with patch.object(ui, 'get_terminal_size', lambda *a: (80, 24)):
            pc.screen_row_paint(1, "same")
            self.assertEqual(pc.screen_row_paint(1, "same"), "")        # unchanged: skipped
        with patch.object(ui, 'get_terminal_size', lambda *a: (100, 30)):
            out = pc.screen_row_paint(1, "same")
        self.assertTrue(out.startswith("\033[H\033[2J"))               # wiped, then repainted
        self.assertIn("same", out)


if __name__ == "__main__":
    unittest.main()
