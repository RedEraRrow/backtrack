"""Lyrics in time with what's heard: the playback clock carried on smoothly
between mpv's uneven readings, and lines that change on their timestamp when
asked to."""
import unittest
from unittest import mock

from backtrack.lyrics.lyric_pane import Beat, LINE, Timeline
from backtrack.playback import session as sess


class _MP:
    def __init__(self):
        self.time_pos, self.core_idle = 10.0, False


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.s = sess.PlaybackSession()
        self.s.mp = _MP()
        self.now = [100.0]
        self._p = mock.patch.object(sess.time, 'monotonic', lambda: self.now[0])
        self._p.start()

    def tearDown(self):
        self._p.stop()

    def test_runs_on_between_readings_and_eases_to_them(self):
        self.assertEqual(self.s.elapsed(), 10.0)                 # the first reading: as it is
        self.now[0] += 0.2
        self.assertAlmostEqual(self.s.elapsed(), 10.2)           # mpv hasn't moved on: the clock has
        self.s.mp.time_pos = 10.4                                # a reading 0.2 s ahead of the clock
        self.assertAlmostEqual(self.s.elapsed(), 10.2 + 0.2 * sess.tune.CLOCK_EASE)

    def test_a_jump_or_a_pause_is_taken_at_once(self):
        self.s.elapsed()
        self.s.mp.time_pos = 60.0                                # a seek
        self.assertEqual(self.s.elapsed(), 60.0)
        self.now[0] += 1.0
        self.s.mp.core_idle = True
        self.assertEqual(self.s.elapsed(), 60.0)                 # paused: no running on


class RunUpTest(unittest.TestCase):
    def test_no_run_up_changes_on_the_timestamp(self):
        beats = [Beat(start=1.0, end=3.0, kind=LINE, text="a"), Beat(start=3.0, end=5.0, kind=LINE, text="b")]
        self.assertEqual(Timeline(beats, 10.0, lead=0.0).beats[1].start, 3.0)
        self.assertAlmostEqual(Timeline(beats, 10.0, lead=0.35).beats[1].start, 2.65)


if __name__ == '__main__':
    unittest.main()
