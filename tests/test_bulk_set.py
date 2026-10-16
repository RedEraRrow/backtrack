"""Bulk Set value makes every selected track have the value: the ones that
didn't have the tag (or any tags) get it too, as the confirmation counts them."""
import shutil
import tempfile
import unittest
from unittest import mock

from mutagen.id3 import ID3, TCOP

from _media import make_audio, needs_ffmpeg
from backtrack.id3 import bulk_menu


@needs_ffmpeg
class BulkSetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_set_value_fills_the_tracks_without_it(self):
        has = make_audio(self.tmp, 'mp3', 'has')
        tags = ID3()
        tags.add(TCOP(encoding=3, text=["2001 Café del Mar"]))
        tags.save(has)
        without = make_audio(self.tmp, 'mp3', 'without')        # no tags at all
        answers = iter(["Set value", ["TCOP"], "Enter a value"])
        with mock.patch.object(bulk_menu.prompt, 'select', lambda *a, **k: next(answers)), \
             mock.patch.object(bulk_menu.prompt, 'confirm', lambda *a, **k: True), \
             mock.patch.object(bulk_menu, 'prompt_for_value', lambda *a, **k: "℗ 2001 Café del Mar"), \
             mock.patch.object(bulk_menu, 'refresh_library_entry', lambda *a, **k: None):
            bulk_menu.bulk_id3_manager([], paths=[has, without])
        for path in (has, without):
            self.assertEqual(str(ID3(path)['TCOP']), "℗ 2001 Café del Mar", path)


if __name__ == '__main__':
    unittest.main()
