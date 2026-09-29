"""The player's lyric timeline: when each beat hands over to the next."""
import unittest

from src import tuning as tune
from src.lyrics.lyric_pane import DIRECTION, LINE, Beat, Timeline, handover

LEAD = tune.LYRIC_LEAD_IN_S


class HandoverTest(unittest.TestCase):
    def test_the_run_up_comes_out_of_silence_first(self):
        self.assertAlmostEqual(handover(0.0, 2.0, 10.0, False), 10.0 - LEAD)

    def test_with_no_silence_it_comes_out_of_the_line_but_never_more_than_half(self):
        self.assertAlmostEqual(handover(0.0, 4.0, 4.0, False), 4.0 - min(LEAD, 2.0))
        short = LEAD / 2                                   # a line shorter than the run-up
        self.assertAlmostEqual(handover(0.0, short, short, False), short / 2)

    def test_a_direction_keeps_its_whole_beat(self):
        self.assertAlmostEqual(handover(0.0, 4.0, 4.0, True), 4.0)

    def test_never_earlier_than_the_floor(self):
        self.assertEqual(handover(0.0, 1.0, 1.0, False, floor=0.9), 0.9)


class TimelineTest(unittest.TestCase):
    def test_beats_tile_the_track(self):
        tl = Timeline([Beat(start=1.0, end=2.0, kind=LINE), Beat(start=3.0, end=3.5, kind=DIRECTION),
                       Beat(start=3.5, end=6.0, kind=LINE)], duration=10.0)
        self.assertEqual(tl.beats[0].start, 0.0)
        self.assertEqual(tl.beats[-1].end, 10.0)
        for a, b in zip(tl.beats, tl.beats[1:]):
            self.assertEqual(a.end, b.start)
        self.assertEqual(tl.beats[1].end, 3.5)             # the direction gave nothing up


if __name__ == "__main__":
    unittest.main()
