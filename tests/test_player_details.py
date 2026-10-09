"""The player's details line gives up the track number in steps when short of room."""
import unittest
from unittest.mock import patch

from mutagen.id3 import TCON, TDRC, TIT2, TPE1, TALB, TRCK

from backbone import ui
from backtrack.playback import player_ui as pu

_TAGS = {f.FrameID: f for f in (TIT2(text="Episode 1"), TPE1(text="James Cary"), TALB(text="Be Lucky"),
                               TDRC(text="2016"), TCON(text="Comedy"), TRCK(text="1/6"))}


def _details(width: int) -> str:
    with patch.dict(pu._ui_state, show_metadata=True, chapter_title=None, chapter_pos=None, debug=False):
        return ui.strip_ansi(pu._meta_left_lines(_TAGS, "x.mp3", width)[-1])


class DetailsLineTest(unittest.TestCase):
    def test_track_word_goes_then_the_track(self):
        self.assertEqual(_details(40), "2016 · Comedy · Track 1 of 6")
        self.assertEqual(_details(22), "2016 · Comedy · 1 of 6")
        self.assertEqual(_details(16), "2016 · Comedy")
        self.assertEqual(_details(8), "2016 · …")


if __name__ == "__main__":
    unittest.main()
