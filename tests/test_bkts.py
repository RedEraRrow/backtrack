"""A track's script and transcript packed as one .bkts: found first, read
through the same finders as loose files, and written back into by the editor."""
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

from backtrack.lyrics import bkts
from backtrack.lyrics import formats as ly


class BktsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = tempfile.mkdtemp()
        self._p = mock.patch.object(bkts, '_cache_root', lambda: __import__('pathlib').Path(self.cache))
        self._p.start()
        self.track = os.path.join(self.dir, 'ep.mp3')
        open(self.track, 'w').close()

    def tearDown(self):
        self._p.stop()
        shutil.rmtree(self.dir)
        shutil.rmtree(self.cache)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text)
        return path

    def test_packed_pair_is_found_before_loose_files(self):
        md = self.write('ep.md', '**A**: Hello.\n')
        js = self.write('ep.json', json.dumps({'segments': [{'text': 'Hello.', 'start': 0, 'end': 1}]}))
        self.assertEqual(ly._find_markdown_for_audio(self.track), md)
        archive = bkts.pack(self.track, md, js)
        self.assertEqual(sorted(zipfile.ZipFile(archive).namelist()), ['script.md', 'transcript.json'])
        inside = ly._find_markdown_for_audio(self.track)
        self.assertNotEqual(inside, md)
        self.assertEqual(open(inside).read(), '**A**: Hello.\n')
        self.assertEqual(bkts.label(inside), 'ep.bkts')
        self.assertEqual(ly._find_timing_files_for_audio(self.track)[1], bkts.member(self.track, bkts.TRANSCRIPT))
        self.assertEqual(ly._find_markdown_for_audio(self.track, packed=False), md)

    def test_a_write_goes_back_into_the_archive_and_survives_a_reread(self):
        md = self.write('ep.md', 'x')
        js = self.write('ep.json', '{"segments": []}')
        archive = bkts.pack(self.track, md, js)
        inside = bkts.member(self.track, bkts.TRANSCRIPT)
        bkts.save(inside, '{"segments": [1]}')
        working = os.path.join(os.path.dirname(inside), bkts.WORKING)
        bkts.save(working, '{"w": 1}')
        with zipfile.ZipFile(archive) as zf:
            self.assertEqual(zf.read('transcript.json'), b'{"segments": [1]}')
            self.assertEqual(zf.read('transcript.sync.json'), b'{"w": 1}')
            self.assertEqual(zf.read('script.md'), b'x')
        self.assertEqual(bkts.member(self.track, bkts.TRANSCRIPT), inside)      # no needless re-unpack

    def test_a_changed_archive_is_unpacked_again(self):
        md = self.write('ep.md', 'old')
        js = self.write('ep.json', '{"segments": []}')
        bkts.pack(self.track, md, js)
        self.assertEqual(open(bkts.member(self.track, bkts.SCRIPT)).read(), 'old')
        self.write('ep.md', 'newer')
        bkts.pack(self.track, md, js)
        self.assertEqual(open(bkts.member(self.track, bkts.SCRIPT)).read(), 'newer')

    def test_a_loose_file_is_saved_as_itself(self):
        path = self.write('ep.json', '{}')
        bkts.save(path, '{"a": 1}')
        self.assertEqual(open(path).read(), '{"a": 1}')
        self.assertIsNone(bkts.source_of(path))


if __name__ == '__main__':
    unittest.main()
