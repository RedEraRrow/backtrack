"""The key/value tag model behind the editor for m4a and FLAC/Ogg/Opus files."""
import shutil
import tempfile
import unittest

import mutagen
from mutagen.mp4 import MP4, MP4FreeForm

from _media import make_audio, needs_ffmpeg, png_bytes
from backtrack.id3 import tag_formats as tf
from backtrack.id3 import tag_writer as tw


@needs_ffmpeg
class KeyValueTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_vorbis(self):
        for ext in ('flac', 'ogg', 'opus'):
            path = make_audio(self.dir, ext)
            tw.write_cover(path, png_bytes(), 'image/png')
            tags, save = tf.open_tags(path)
            tf.kv_set(tags, 'genre', ['Jazz', 'Blues'])
            tf.kv_set(tags, 'ALBUMARTIST', ['A'])
            save()
            items = dict(tf.kv_items(tf.open_tags(path)[0]))
            self.assertEqual((items['GENRE'], items['ALBUMARTIST']), (['Jazz', 'Blues'], ['A']), ext)
            self.assertFalse(any('PICTURE' in k for k in items), ext)          # the cover has its own row
            self.assertEqual(tf.label_for('vorbis', 'ALBUMARTIST'), 'Album artist')
            tags, save = tf.open_tags(path)
            tf.kv_delete(tags, 'GENRE')
            save()
            self.assertNotIn('GENRE', dict(tf.kv_items(tf.open_tags(path)[0])), ext)
            self.assertTrue(tw.has_cover(path), ext)

    def test_mp4_values_keep_their_types(self):
        path = make_audio(self.dir, 'm4a')
        f = MP4(path)
        f.update({'trkn': [(3, 12)], 'tmpo': [120], 'cpil': True,
                  '----:com.apple.iTunes:LYRICIST': [MP4FreeForm(b'Ly')]})
        f.save()
        tags, save = tf.open_tags(path)
        items = dict(tf.kv_items(tags))
        self.assertEqual((items['trkn'], items['tmpo'], items['cpil'], items['----:com.apple.iTunes:LYRICIST']),
                         (['3/12'], ['120'], ['1'], ['Ly']))
        tf.kv_set(tags, 'trkn', ['4/10'])
        tf.kv_set(tags, 'tmpo', ['99'])
        tf.kv_set(tags, 'cpil', ['0'])
        tf.kv_set(tags, '----:com.apple.iTunes:LYRICIST', ['New'])
        save()
        f = MP4(path)
        self.assertEqual((f['trkn'], f['tmpo'], f['cpil'], bytes(f['----:com.apple.iTunes:LYRICIST'][0])),
                         ([(4, 10)], [99], False, b'New'))
        with self.assertRaises(ValueError):
            tf.kv_set(f.tags, 'trkn', ['three'])
        self.assertEqual(tf.label_for('mp4', 'trkn'), 'Track')

    def test_cli_tag_read_lists_a_flac(self):
        import io
        from contextlib import redirect_stdout
        from backtrack.cli_commands import _tag_rows
        path = make_audio(self.dir, 'flac')
        f = mutagen.File(path)
        f.tags['TITLE'] = ['Song']
        f.save()
        rows = {r['tag']: r for r in _tag_rows(path)}
        self.assertEqual((rows['TITLE']['name'], rows['TITLE']['value']), ('Title', 'Song'))


if __name__ == "__main__":
    unittest.main()
