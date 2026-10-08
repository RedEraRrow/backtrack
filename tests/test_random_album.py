"""Random album: one album, whole and in order, picked from a list or the library."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from backtrack import music_library as ml
from backtrack.playback import session as sess


def _track(path, album, n):
    return {'path': path, 'album': album, 'album_artist': 'X', 'track': str(n), 'disc': '1', 'title': f't{n}'}


class RandomAlbumTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lib = []
        for album, n in (('A', 3), ('B', 2)):
            for i in range(n, 0, -1):                       # out of order on purpose
                p = os.path.join(self.dir, f"{album}{i}.mp3")
                open(p, "wb").close()
                self.lib.append(_track(p, album, i))
        self.paths = {t['path'][-6:-4]: t['path'] for t in self.lib}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_whole_album_in_order(self):
        for _ in range(10):
            got = ml.random_album([self.paths['A2'], self.paths['B1']], self.lib)
            self.assertIn(got, ([self.paths[k] for k in ('A1', 'A2', 'A3')],
                                [self.paths[k] for k in ('B1', 'B2')]))

    def test_avoids_the_album_that_just_played(self):
        for _ in range(10):
            self.assertEqual(ml.random_album(list(self.paths.values()), self.lib, avoid=self.paths['A1']),
                             [self.paths['B1'], self.paths['B2']])

    def test_never_a_book(self):
        with mock.patch.object(ml, 'is_audiobook', lambda p, cfg=None: '/A' in p):
            for _ in range(10):
                self.assertEqual(ml.random_album(list(self.paths.values()), self.lib)[0], self.paths['B1'])
        with mock.patch.object(ml, 'is_audiobook', lambda p, cfg=None: True):
            self.assertEqual(ml.random_album(list(self.paths.values()), self.lib), [])

    def test_the_queue_ending_starts_one(self):
        class S(sess.PlaybackSession):
            def _load(self, path, start_at=0.0):
                self.file_path, self.mp = path, object()
                return True

            def _title_for(self, path):
                return path

        s = S()
        s.queue, s.titles, s.index, s.file_path = [self.paths['A3']], ['A3'], 0, self.paths['A3']
        with mock.patch.object(ml, '_live_config', lambda: {'queue_end': 'random_album'}), \
             mock.patch.object(ml, 'live_library', lambda: self.lib):
            self.assertEqual(s.next(manual=False), self.paths['B1'])
        self.assertEqual(s.queue, [self.paths['B1'], self.paths['B2']])


if __name__ == "__main__":
    unittest.main()
