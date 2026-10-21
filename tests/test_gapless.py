"""Gapless: the session lines up the track that follows with mpv, follows mpv
when it moves on by itself, and moves on itself only when mpv can't."""
import os
import shutil
import tempfile
import unittest

from backtrack.playback import session as sess


class _MP:
    """An mpv stand-in: loadfile hands out entry ids; the rest is state."""
    def __init__(self):
        self.calls, self.ids = [], 0
        self.idle_active, self.duration, self.volume, self.pause = False, 0, 100, False

    def command(self, name, *args):
        self.calls.append((name, *args))
        if name == 'loadfile':
            self.ids += 1
            return {'playlist_entry_id': self.ids}
        return None

    def wait_for_property(self, name, timeout=None):
        pass


class GaplessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = []
        for n in "abc":
            p = os.path.join(self.tmp, f"{n}.mp3")
            open(p, 'wb').close()
            self.paths.append(p)
        self.s = sess.PlaybackSession()
        self.s.mp = _MP()
        self.s._save_queue = lambda: None
        self.s._ensure_advertised = lambda: None              # no session socket for other windows
        self.s.start(self.paths[0], queue=list(self.paths))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def appended(self):
        return [c[1] for c in self.s.mp.calls if c[0] == 'loadfile' and c[2] == 'append']

    def test_the_next_track_is_lined_up_and_followed(self):
        self.assertEqual(self.appended(), [self.paths[1]])
        self.assertIsNone(self.s.tick())                       # still on a
        self.s._started_id = self.s._next[0]                   # mpv moved on to b by itself
        self.assertEqual(self.s.tick(), 'changed')
        self.assertEqual((self.s.index, self.s.file_path), (1, self.paths[1]))
        self.assertEqual(self.appended()[-1], self.paths[2])   # and c lined up behind it

    def test_an_end_with_one_lined_up_waits_for_mpv_unless_it_stopped(self):
        self.s._ended_id = self.s._entry_id                    # a played out
        self.assertIsNone(self.s.tick())                       # mpv goes on to b itself
        self.s.mp.idle_active = True                           # it didn't: the session moves on
        self.assertEqual(self.s.tick(), 'changed')
        self.assertEqual(self.s.index, 1)

    def test_an_edit_changes_what_is_lined_up(self):
        self.s.edit('move', pos=2, delta=-1, path=self.paths[2])
        self.assertEqual(self.appended()[-1], self.paths[2])

    def test_what_follows(self):
        s = self.s
        s.index = 2
        self.assertIsNone(s._next_index(manual=False))         # the end, repeat off
        s.mode = sess.REPEAT_ALL
        self.assertEqual(s._next_index(manual=False), 0)
        s.mode = sess.REPEAT_ONE
        self.assertEqual(s._next_index(manual=False), 2)
        self.assertIsNone(s._next_index(manual=True))          # a skip passes repeat-one by: the end

    def test_file_options_keep_a_filter_graph_whole(self):
        self.assertEqual(sess._file_options(af="lavfi=[a=1,b=2]", speed="1.5"), "af=%15%lavfi=[a=1,b=2],speed=%3%1.5")


if __name__ == '__main__':
    unittest.main()
