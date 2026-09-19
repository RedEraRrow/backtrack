"""Tests for the inline blocking-loop progress line (src/utils/ui_utils.py):
a per-track ffmpeg scan (trim commit, loudness measurement) has no other
redraw happening, so this is the only thing that keeps it from looking hung."""
import io
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src.utils import ui_utils


class PrintInlineProgressTest(unittest.TestCase):
    def test_writes_carriage_return_and_clears_to_end_of_line(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ui_utils.print_inline_progress("Measuring 1/3: track.mp3", 0.5)
        out = buf.getvalue()
        self.assertTrue(out.startswith("\r"))
        self.assertTrue(out.endswith("\033[K"))
        self.assertIn("track.mp3", ui_utils.strip_ansi(out))

    def test_truncates_message_to_fit_terminal_width(self):
        buf = io.StringIO()
        long_name = "x" * 200 + ".mp3"
        with patch.object(ui_utils, "get_terminal_width", return_value=40):
            with redirect_stdout(buf):
                ui_utils.print_inline_progress(f"Measuring 1/1: {long_name}", 0.0)
        plain = ui_utils.strip_ansi(buf.getvalue())
        self.assertLessEqual(ui_utils.visual_len(buf.getvalue()), 40)
        self.assertIn("…", plain)

    def test_inset_by_margin_and_centred_when_short(self):
        buf = io.StringIO()
        with patch.object(ui_utils, "get_terminal_width", return_value=80):
            with redirect_stdout(buf):
                ui_utils.print_inline_progress("short", 0.0)
        plain = ui_utils.strip_ansi(buf.getvalue()).lstrip("\r")
        leading_spaces = len(plain) - len(plain.lstrip(" "))
        self.assertGreaterEqual(leading_spaces, ui_utils.MARGIN_H)
        # centred: pad should be roughly (width - content) // 2, not just the minimum
        self.assertGreater(leading_spaces, ui_utils.MARGIN_H)

    def test_never_pads_below_margin_when_content_fills_width(self):
        buf = io.StringIO()
        long_name = "x" * 200 + ".mp3"
        with patch.object(ui_utils, "get_terminal_width", return_value=40):
            with redirect_stdout(buf):
                ui_utils.print_inline_progress(f"Measuring 1/1: {long_name}", 0.0)
        plain = ui_utils.strip_ansi(buf.getvalue()).lstrip("\r")
        leading_spaces = len(plain) - len(plain.lstrip(" "))
        self.assertGreaterEqual(leading_spaces, ui_utils.MARGIN_H)

    def test_clear_inline_progress_writes_clear_sequence(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ui_utils.clear_inline_progress()
        self.assertEqual(buf.getvalue(), "\r\033[K")


if __name__ == "__main__":
    unittest.main()
