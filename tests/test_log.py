"""The diagnostics log: silent unless switched on, and on a resize it names the
rows of the old frame that were wider than the new window."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.utils import log as logmod, prompt_core as pc, ui_utils


class DiagnosticsLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._path = patch.object(logmod, "log_path", lambda: self.tmp / "backtrack.log")
        self._path.start()

    def tearDown(self):
        logmod.configure(False)
        self._path.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _resize(self, rows):
        pc._screen.clear(); pc._screen_size[0] = None
        with patch.object(ui_utils, 'get_terminal_size', lambda *a: (80, 24)):
            for r, text in rows.items():
                pc.screen_row_paint(r, text)
        with patch.object(ui_utils, 'get_terminal_size', lambda *a: (60, 24)):
            pc.screen_row_paint(1, "new frame")

    def test_off_writes_nothing(self):
        logmod.configure(False)
        self._resize({1: "─" * 78})
        self.assertFalse((self.tmp / "backtrack.log").exists())

    def test_resize_names_the_rows_wider_than_the_new_window(self):
        logmod.configure(True)
        self._resize({1: "─" * 78, 2: "short", 5: "x" * 61})
        text = (self.tmp / "backtrack.log").read_text()
        self.assertIn("resize 80x24 -> 60x24", text)
        self.assertIn("2 old rows wider than the new width", text)
        self.assertIn("[1, 5]", text)


if __name__ == "__main__":
    unittest.main()
