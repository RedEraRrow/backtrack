"""A player redraw must leave the progress bar's row alone: update_progress_ui
owns it, and a frame that painted it blank made the bar flicker (standard and
minimal layouts did)."""
import io
import sys
import unittest

from mutagen.id3 import ID3, TIT2

from src.playback import playback_ui as ui
from src.utils import ui_utils


def _quiet(fn, *a, **k):
    real, sys.stdout = sys.stdout, io.StringIO()
    try:
        r = fn(*a, **k)
        return r, sys.stdout.getvalue()
    finally:
        sys.stdout = real


class ProgressRowTest(unittest.TestCase):
    def test_redraw_skips_the_progress_row(self):
        audio = ID3(); audio.add(TIT2(encoding=3, text='Title'))
        for size in [(40, 20), (60, 30), (140, 40)]:        # minimal, standard, wide
            _quiet(ui_utils.clear_screen)
            (prog_row, *_), _ = _quiet(ui.draw_full_ui, '/none.mp3', audio, None, size)
            _quiet(ui.update_progress_ui, prog_row, 10, 100, size[0])
            _, again = _quiet(ui.draw_full_ui, '/none.mp3', audio, None, size)
            self.assertNotIn(f"\033[{prog_row};1H", again, size)


if __name__ == "__main__":
    unittest.main()
