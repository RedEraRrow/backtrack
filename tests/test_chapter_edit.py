"""Chapter editing: the edits on their own, and writing them to every kind of file."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from _media import HAVE_FFMPEG, needs_ffmpeg, png_bytes
from backtrack import chapter_edit as ce
from backtrack.id3 import tag_writer as tw
from backtrack.music_library import get_metadata

_CH = [[0.0, 'Prologue'], [3.25, 'The Boy'], [7.5, '']]


class EditsTest(unittest.TestCase):
    def test_rename_move_add_delete(self):
        self.assertEqual(ce.rename(_CH, 2, ' Coda '), [*_CH[:2], [7.5, 'Coda']])
        self.assertEqual(ce.move(_CH, 1, 4.0, 10.0)[1], [4.0, 'The Boy'])
        added, i = ce.add(_CH, 5.0, 'Middle', 10.0)
        self.assertEqual((added[i], i, len(added)), ([5.0, 'Middle'], 2, 4))
        self.assertEqual(ce.delete(_CH, 1), [_CH[0], _CH[2]])

    def test_order_is_kept(self):
        for bad in (0.0, 7.5, 9.0):
            with self.assertRaises(ValueError):
                ce.move(_CH, 1, bad, 10.0)
        with self.assertRaises(ValueError):
            ce.add(_CH, 3.25, 'Same place', 10.0)
        with self.assertRaises(ValueError):
            ce.add(_CH, 12.0, 'Past the end', 10.0)

    def test_times(self):
        self.assertEqual([ce.parse_time(t, 100) for t in ('1:53:38', '2:05.5', '61', '+1.5', '-0.25')],
                         [6818.0, 125.5, 61.0, 101.5, 99.75])
        with self.assertRaises(ValueError):
            ce.parse_time('soon')
        self.assertEqual([ce.format_time(t) for t in (5.25, 6818.0)], ['0:05.250', '1:53:38.000'])


@needs_ffmpeg
class WriteEveryKindTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.cfg, ignore_errors=True)

    def _file(self, ext):
        path = os.path.join(self.dir, f"t.{ext}")
        codec = {'m4a': ['-c:a', 'aac'], 'm4b': ['-c:a', 'aac', '-f', 'ipod']}.get(ext, [])
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=10", *codec, path], check=True)
        tw.write_fields(path, {'title': 'T', 'album': 'Al'}, {'title', 'album'})
        return path

    def test_round_trip(self):
        from backtrack.trim import engine
        with mock.patch.object(engine, '_backup_dir', lambda: engine.Path(self.cfg)):
            for ext in ('mp3', 'wav', 'aiff', 'flac', 'ogg', 'opus', 'm4a', 'm4b'):
                path = self._file(ext)
                if ext.startswith('m4'):
                    tw.write_cover(path, png_bytes(), 'image/png')
                ce.write(path, _CH, 10.0)
                got = get_metadata(path)
                self.assertEqual(got['chapters'], _CH, ext)
                self.assertEqual((got['title'], got['album']), ('T', 'Al'), ext)    # tags survive
                if ext.startswith('m4'):
                    self.assertTrue(tw.has_cover(path), ext)
                ce.write(path, ce.delete(_CH, 1), 10.0)
                self.assertEqual(get_metadata(path)['chapters'], [_CH[0], _CH[2]], ext)
            # Each MP4 rewrite left the version before it in the backup store.
            with open(os.path.join(self.cfg, "manifest.json")) as f:
                entries = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(sorted(e['reason'] for e in entries), ['chapters'] * 4)


if __name__ == "__main__":
    unittest.main()
