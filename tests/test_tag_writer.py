"""Headless tests for backtrack/id3/tag_writer.py."""
import os
import shutil
import tempfile
import unittest

from mutagen.id3 import ID3, TLEN, TDLY  # type: ignore[reportPrivateImportUsage]

from _media import make_audio, needs_ffmpeg, png_bytes
from backtrack.id3 import file_namer
from backtrack.id3 import tag_writer as tw
from backtrack.music_library import get_metadata


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


@needs_ffmpeg
class EveryKindRoundTripTest(unittest.TestCase):
    """Each field the bulk tools write, written to every kind of file and read back."""
    KINDS = ('mp3', 'wav', 'aiff', 'flac', 'ogg', 'oga', 'opus', 'm4a')
    VALUES = {'title': 'Song', 'artist': 'The Band', 'album_artist': 'The Band', 'album': 'Album',
              'track': '3', 'total_tracks': '12', 'disc': '1', 'total_discs': '2',
              'disc_subtitle': 'Live', 'year': '1999', 'compilation': True,
              'artist_sort': 'Band, The', 'album_sort': ''}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fields(self):
        apply = set(tw.FIELDS) | {'sort'}
        for ext in self.KINDS:
            path = make_audio(self.tmp, ext, name=ext)
            self.assertEqual(tw.present_fields(path)['title'], False, ext)
            res = tw.write_fields(path, self.VALUES, apply)
            self.assertIsNone(res.error, ext)
            subtitle = ext != 'm4a'                  # MP4 has no disc-subtitle atom
            self.assertEqual('disc_subtitle' in tw.writable_fields(path), subtitle, ext)
            got = get_metadata(path)
            self.assertEqual((got['title'], got['artist'], got['album'], got['track'], got['total_tracks'],
                              got['disc'], got['total_discs'], got['year'], got['compilation'],
                              got['Performer Sort Order']),
                             ('Song', 'The Band', 'Album', '3', '12', '1', '2', '1999', True, 'Band, The'), ext)
            self.assertEqual(got['disc_subtitle'], 'Live' if subtitle else '', ext)
            self.assertEqual(tw.read_number_pairs(path),
                             {'track': '3', 'total_tracks': '12', 'disc': '1', 'total_discs': '2'}, ext)
            self.assertTrue(all(tw.present_fields(path)[f] for f in ('title', 'track', 'year')), ext)
            # Fill blanks only leaves what's there.
            again = tw.write_fields(path, {**self.VALUES, 'title': 'Other'}, {'title'})
            self.assertEqual(again.skipped_existing, ['title'], ext)
            tokens = file_namer.read_tokens(path)
            self.assertEqual((tokens['title'], tokens['track'], tokens['year']), ('Song', '03', '1999'), ext)
            cleared = tw.clear_fields(path, ['title', 'track'])
            self.assertEqual(cleared.written, ['title', 'track'], ext)
            got = get_metadata(path)
            self.assertEqual((got['title'], got['track'], got['album']), (ext, '0', 'Album'), ext)

    def test_cover(self):
        png = png_bytes()
        for ext in self.KINDS:
            path = make_audio(self.tmp, ext, name=ext)
            self.assertFalse(tw.has_cover(path), ext)
            self.assertEqual(tw.write_cover(path, png, 'image/png').written, ['cover'], ext)
            self.assertTrue(tw.has_cover(path), ext)
            self.assertEqual(tw.write_cover(path, b'x', 'image/png').skipped_existing, ['cover'], ext)
            self.assertEqual(tw.remove_cover(path).written, ['cover'], ext)
            self.assertFalse(tw.has_cover(path), ext)

    def test_aac_has_nowhere_to_keep_tags(self):
        self.assertTrue(tw.write_fields(os.path.join(self.tmp, 'x.aac'), self.VALUES, {'title'}).unsupported)


if __name__ == "__main__":
    unittest.main()
