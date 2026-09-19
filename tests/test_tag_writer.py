"""Headless tests for src/id3/tag_writer.py."""
import os
import shutil
import tempfile
import unittest

from mutagen.id3 import ID3, TLEN, TDLY  # type: ignore[reportPrivateImportUsage]

from src.id3 import tag_writer as tw


class StaleLengthTagsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mp3(self, tlen=None, tdly=None) -> str:
        path = os.path.join(self.tmp, "t.mp3")
        open(path, "wb").close()
        audio = ID3()
        if tlen is not None:
            audio.add(TLEN(encoding=3, text=[str(tlen)]))
        if tdly is not None:
            audio.add(TDLY(encoding=3, text=[str(tdly)]))
        audio.save(path, v2_version=3)
        return path

    def test_no_tags(self):
        self.assertEqual(tw.stale_length_tags(self._mp3()), (False, False))

    def test_tlen_present(self):
        self.assertEqual(tw.stale_length_tags(self._mp3(tlen=123456)), (True, False))

    def test_tdly_zero_is_not_stale(self):
        self.assertEqual(tw.stale_length_tags(self._mp3(tdly=0)), (False, False))

    def test_tdly_nonzero_is_stale(self):
        self.assertEqual(tw.stale_length_tags(self._mp3(tdly=500)), (False, True))

    def test_no_id3_header(self):
        path = os.path.join(self.tmp, "blank.mp3")
        open(path, "wb").close()
        self.assertEqual(tw.stale_length_tags(path), (False, False))

    def test_non_mp3_format(self):
        path = os.path.join(self.tmp, "t.m4a")
        open(path, "wb").close()
        self.assertEqual(tw.stale_length_tags(path), (False, False))


if __name__ == "__main__":
    unittest.main()
