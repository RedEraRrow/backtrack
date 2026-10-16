"""Lyrics in time with what's heard: the playback clock carried on smoothly
between VLC's quarter-second readings, MP3s read through a demuxer whose seeks
land where they say, and lines that change on their timestamp when asked to."""
import unittest
from unittest import mock

from backtrack.lyrics.lyric_pane import Beat, LINE, Timeline
from backtrack.playback import libvlc
from backtrack.playback import session as sess


class _MP:
    def __init__(self):
        self.ms, self.state = 10_000, sess._VLC_STATE_PLAYING

    def get_time(self):
        return self.ms

    def get_state(self):
        return self.state


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
        self.assertAlmostEqual(self.s.elapsed(), 10.2)           # VLC hasn't moved on: the clock has
        self.s.mp.ms = 10_400                                    # a reading 0.2 s ahead of the clock
        self.assertAlmostEqual(self.s.elapsed(), 10.2 + 0.2 * sess.tune.CLOCK_EASE)

    def test_a_jump_or_a_pause_is_taken_at_once(self):
        self.s.elapsed()
        self.s.mp.ms = 60_000                                    # a seek
        self.assertEqual(self.s.elapsed(), 60.0)
        self.now[0] += 1.0
        self.s.mp.state = sess._VLC_STATE_PAUSED
        self.assertEqual(self.s.elapsed(), 60.0)                 # paused: no running on


class DemuxTest(unittest.TestCase):
    def test_an_mp3_is_read_by_ffmpegs_demuxer(self):
        added = []
        media = mock.Mock(add_option=added.append)
        inst = mock.Mock(media_new=lambda path: media)
        libvlc.media_for(inst, "/m/song.MP3")
        self.assertEqual(added, [':demux=avformat'])
        added.clear()
        libvlc.media_for(inst, "/m/song.m4a")
        self.assertEqual(added, [])


class RunUpTest(unittest.TestCase):
    def test_no_run_up_changes_on_the_timestamp(self):
        beats = [Beat(start=1.0, end=3.0, kind=LINE, text="a"), Beat(start=3.0, end=5.0, kind=LINE, text="b")]
        self.assertEqual(Timeline(beats, 10.0, lead=0.0).beats[1].start, 3.0)
        self.assertAlmostEqual(Timeline(beats, 10.0, lead=0.35).beats[1].start, 2.65)


if __name__ == '__main__':
    unittest.main()
