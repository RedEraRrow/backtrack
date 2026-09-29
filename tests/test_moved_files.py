"""Files moved or renamed behind the library's back: dropped from what is acted
on, the library re-synced, and one plain message instead of an error."""
import os
import tempfile
import unittest
from unittest.mock import patch

from src import config, music_library as ml
from src.utils import ui_utils


class DropMovedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'Music')
        os.mkdir(self.root)
        self.a, self.b = (os.path.join(self.root, n) for n in ('a.mp3', 'b.mp3'))
        for p in (self.a, self.b):
            with open(p, 'wb') as f:
                f.write(b'\xff\xfb\x90\x00' * 64)
        self.library = [ml.get_metadata(self.a), ml.get_metadata(self.b)]
        self._cfg = config.load_config().get('music_directories')
        config.update_config({'music_directories': [self.root], 'music_directory': self.root})
        ml._sync_state['library'] = self.library
        self.status = []
        self._p = [patch.object(ui_utils, 'show_status', lambda m, **k: self.status.append(m)),
                   patch.object(ml, 'save_library_cache', lambda *a, **k: None)]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        ml._sync_state['library'] = None
        config.update_config({'music_directories': self._cfg or []})
        self.tmp.cleanup()

    def test_nothing_moved_is_silent(self):
        self.assertEqual(ml.drop_moved([self.a, self.b]), [self.a, self.b])
        self.assertEqual(self.status, [])

    def test_a_renamed_file_is_dropped_and_the_library_follows_it(self):
        c = os.path.join(self.root, 'c.mp3')
        os.rename(self.b, c)
        self.assertEqual(ml.drop_moved([self.a, self.b]), [self.a])
        self.assertEqual(sorted(t['path'] for t in self.library), [self.a, c])
        self.assertEqual(self.status, ["“b.mp3” was moved or renamed — library updated."])

    def test_a_renamed_library_folder_says_so(self):
        os.rename(self.root, self.root + ' old')
        self.assertEqual(ml.drop_moved([self.a]), [])
        self.assertIn("Library folder “Music” was moved or renamed", self.status[-1])
        self.assertEqual(len(self.library), 2)      # an absent folder isn't wiped


class QueueSkipsMovedTest(unittest.TestCase):
    def test_advance_skips_a_track_renamed_after_queueing(self):
        from src.playback.session import PlaybackSession
        s = PlaybackSession()
        s.queue, s.titles, s.index = ['/q/1.mp3', '/q/gone.mp3', '/q/3.mp3'], ['1', 'gone', '3'], 0
        loaded = []
        with patch('src.playback.session.drop_moved', lambda ps: [p for p in ps if 'gone' not in p]), \
             patch.object(PlaybackSession, '_load', lambda self, p: loaded.append(p) or True):
            s.next(manual=False)
        self.assertEqual(loaded, ['/q/3.mp3'])
        self.assertEqual((s.queue, s.titles, s.index), (['/q/1.mp3', '/q/3.mp3'], ['1', '3'], 1))


if __name__ == "__main__":
    unittest.main()
