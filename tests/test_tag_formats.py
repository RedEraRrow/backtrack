"""Every tag kind read the same way: MP3/WAV/AIFF (ID3), m4a (MP4 atoms) and
FLAC/Ogg/Opus (Vorbis comments) into the library's fields, their cover art,
and the guard that keeps ID3 out of files that can't take it."""
import base64
import os
import shutil
import tempfile
import unittest

import mutagen
from mutagen.flac import Picture
from mutagen.id3 import (ID3, APIC, TIT2, TPE1, TPE2, TALB, TCON, TCOM, TEXT, TDRC, TIT3, TBPM, TSOT, TSOP,
                         TSO2, TSOA, TSOC, TRCK, TPOS, MVIN, MVNM, TSST, TIT1, TCMP, TMCL, TIPL, TCOP)
from mutagen.mp4 import MP4, MP4FreeForm

from _media import make_audio, needs_ffmpeg, png_bytes
from backtrack.id3 import tag_formats as tf
from backtrack.music_library import get_metadata, track_title

_SKIP = ('path', 'cached_mtime', 'duration', 'meta_version',
         'format', 'bitrate', 'sample_rate', 'bit_depth', 'channels')     # tested in FormatTest
_SORTS = {'Title Sort Order': 'ts', 'Performer Sort Order': 'ps', 'Album Artist Sort Order': 'aas',
          'Album Sort Order': 'als', 'Composer Sort Order': 'cs'}
# What every kind should read back from the same tags written its own way.
_COMMON = {'title': 'T', 'artist': 'A1; A2', 'album_artist': 'AA', 'compilation': True, 'album': 'Al',
           'track': '3', 'total_tracks': '12', 'disc': '1', 'total_discs': '2', 'genre': 'G1; G2',
           'composer': 'C', 'lyricist': 'Ly', 'year': '2001', 'grouping': 'Gr', 'work': 'W',
           'movement_name': 'Mv', 'movement_number': '2', 'play_count': 0, 'bpm': '120', 'copyright': '℗ Co',
           'chapters': [], **_SORTS}


def _read(path):
    d = get_metadata(path)
    for k in _SKIP:
        d.pop(k)
    return d


def _tag_id3(path):
    tags = tf.load_id3(path)
    for fr in [TIT2(text=['T']), TPE1(text=['A1', 'A2']), TPE2(text=['AA']), TALB(text=['Al']),
               TCON(text=['G1', 'G2']), TCOM(text=['C']), TEXT(text=['Ly']), TDRC(text=['2001']),
               TIT3(text=['W']), TBPM(text=['120']), TSOT(text=['ts']), TSOP(text=['ps']), TSO2(text=['aas']),
               TSOA(text=['als']), TSOC(text=['cs']), TRCK(text=['3/12']), TPOS(text=['1/2']),
               MVIN(text=['2/4']), MVNM(text=['Mv']), TSST(text=['Sub']), TIT1(text=['Gr']), TCMP(text=['1']),
               TMCL(people=[['violin', 'V Name']]), TIPL(people=[['producer', 'P Name']]), TCOP(text=['℗ Co'])]:
        tags.add(fr)
    tf.save_id3(tags, path)


@needs_ffmpeg
class ReadEveryKindTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_mp3_and_its_chunk_cousins(self):
        want = {**_COMMON, 'disc_subtitle': 'Sub', 'total_movements': '4',
                'people': 'V Name (violin), P Name (producer)',
                'credits': [['V Name', 'violin'], ['P Name', 'producer']]}
        for ext in ('mp3', 'wav', 'aiff', 'aif'):
            path = make_audio(self.dir, ext)
            _tag_id3(path)
            self.assertEqual(_read(path), want, ext)

    def test_mp4(self):
        """Exactly what the MP4 reader gave before the readers were merged."""
        path = make_audio(self.dir, 'm4a')
        f = MP4(path)
        f.update({'\xa9nam': ['T'], '\xa9ART': ['A1', 'A2'], 'aART': ['AA'], '\xa9alb': ['Al'],
                  '\xa9gen': ['G1', 'G2'], '\xa9wrt': ['C'], '\xa9day': ['2001'], '\xa9wrk': ['W'],
                  'tmpo': [120], 'sonm': ['ts'], 'soar': ['ps'], 'soaa': ['aas'], 'soal': ['als'],
                  'soco': ['cs'], '----:com.apple.iTunes:LYRICIST': [MP4FreeForm(b'Ly')],
                  'trkn': [(3, 12)], 'disk': [(1, 2)], '\xa9mvi': [2], '\xa9mvn': ['Mv'],
                  '\xa9grp': ['Gr'], 'cpil': True, 'cprt': ['℗ Co']})
        f.save()
        self.assertEqual(_read(path), {**_COMMON, 'disc_subtitle': '', 'total_movements': '0',
                                       'people': '', 'credits': []})

    def test_vorbis(self):
        for ext in ('flac', 'ogg', 'oga', 'opus'):
            path = make_audio(self.dir, ext)
            f = mutagen.File(path)
            f.tags.update({'TITLE': ['T'], 'ARTIST': ['A1', 'A2'], 'ALBUM ARTIST': ['AA'], 'ALBUM': ['Al'],
                           'GENRE': ['G1', 'G2'], 'COMPOSER': ['C'], 'LYRICIST': ['Ly'], 'DATE': ['2001'],
                           'WORK': ['W'], 'BPM': ['120'], 'TITLESORT': ['ts'], 'ARTISTSORT': ['ps'],
                           'ALBUMARTISTSORT': ['aas'], 'ALBUMSORT': ['als'], 'COMPOSERSORT': ['cs'],
                           'TRACKNUMBER': ['3'], 'TRACKTOTAL': ['12'], 'DISCNUMBER': ['1/2'],
                           'MOVEMENT': ['2'], 'MOVEMENTTOTAL': ['4'], 'MOVEMENTNAME': ['Mv'],
                           'DISCSUBTITLE': ['Sub'], 'GROUPING': ['Gr'], 'COMPILATION': ['1'], 'COPYRIGHT': ['℗ Co'],
                           'PERFORMER': ['V Name (violin)', 'Plain Name'], 'CONDUCTOR': ['Cond'],
                           'CHAPTER001': ['00:00:00.000'], 'CHAPTER001NAME': ['Intro'],
                           'CHAPTER002': ['00:01:02.500'], 'CHAPTER002NAME': ['Middle']})
            f.save()
            self.assertEqual(_read(path), {
                **_COMMON, 'disc_subtitle': 'Sub', 'total_movements': '4',
                'credits': [['V Name', 'violin'], ['Plain Name', ''], ['Cond', 'conductor']],
                'people': 'V Name (violin), Plain Name, Cond (conductor)',
                'chapters': [[0.0, 'Intro'], [62.5, 'Middle']]}, ext)
            self.assertEqual(track_title(path, read_tags=True), 'T', ext)

    def test_untagged_files_read_as_defaults(self):
        for ext in ('mp3', 'wav', 'aiff', 'flac', 'ogg', 'opus', 'm4a'):
            path = make_audio(self.dir, ext)
            d = _read(path)
            self.assertEqual((d['title'], d['artist']), ('t', 'Unknown Artist'), ext)


@needs_ffmpeg
class FormatTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_kind(self):
        from backtrack.music_library import format_label
        want = {'mp3': ('MP3', 0), 'wav': ('WAV', 16), 'aiff': ('AIFF', 16), 'flac': ('FLAC', 16),
                'ogg': ('Vorbis', 0), 'opus': ('Opus', 0), 'm4a': ('AAC', 0)}
        for ext, (codec, bits) in want.items():
            m = get_metadata(make_audio(self.dir, ext, name=ext))
            self.assertEqual((m['format'], m['bit_depth']), (codec, bits), ext)
            self.assertTrue(format_label(m).startswith(codec), ext)
        self.assertEqual(format_label({'format': 'FLAC', 'sample_rate': 96000, 'bit_depth': 24, 'channels': 2,
                                       'bitrate': 2304}), "FLAC 24/96 · stereo · 2304 kbps")
        self.assertEqual(format_label({'format': 'Opus', 'channels': 1}), "Opus · mono")
        self.assertEqual(format_label({}), "")


@needs_ffmpeg
class CoverTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.png = png_bytes()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _picture(self, desc='', ptype=3):
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = ptype, 'image/png', desc, self.png
        return pic

    def test_every_kind(self):
        wav = make_audio(self.dir, 'wav')
        tags = tf.load_id3(wav)
        tags.add(APIC(encoding=3, mime='image/png', type=3, desc='', data=self.png))
        tf.save_id3(tags, wav)
        flac = make_audio(self.dir, 'flac')
        f = mutagen.File(flac)
        f.add_picture(self._picture())
        f.save()
        ogg = make_audio(self.dir, 'opus')
        f = mutagen.File(ogg)
        f.tags['METADATA_BLOCK_PICTURE'] = [base64.b64encode(self._picture().write()).decode()]
        f.save()
        for path in (wav, flac, ogg):
            self.assertEqual(tf.embedded_cover(path), self.png, path)
        self.assertIsNone(tf.embedded_cover(make_audio(self.dir, 'ogg')))

    def test_preferred_picture(self):
        flac = make_audio(self.dir, 'flac')
        f = mutagen.File(flac)
        f.add_picture(self._picture())
        booklet = self._picture('Booklet', 6)
        booklet.data = b'booklet'
        f.add_picture(booklet)
        f.save()
        self.assertEqual(tf.embedded_cover(flac, preferred_desc='booklet'), b'booklet')
        self.assertEqual(tf.embedded_cover(flac, preferred_type=6), b'booklet')
        self.assertEqual(tf.embedded_cover(flac), self.png)


@needs_ffmpeg
class Id3GuardTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_bare_id3_never_lands_on_a_wav(self):
        wav = make_audio(self.dir, 'wav')
        before = open(wav, 'rb').read()
        bare = ID3()
        bare.add(TIT2(text=['x']))
        with self.assertRaises(ValueError):
            tf.save_id3(bare, wav)
        self.assertEqual(open(wav, 'rb').read(), before)

    def test_mp4_and_vorbis_refuse_id3(self):
        for ext in ('m4a', 'flac'):
            with self.assertRaises(ValueError):
                tf.save_id3(ID3(), make_audio(self.dir, ext))

    def test_frame_ops_reach_a_wav_through_its_chunk(self):
        from backtrack.id3.tag_handler import apply_bulk_operation_to_files
        for ext in ('wav', 'aiff'):
            path = make_audio(self.dir, ext)
            self.assertEqual(apply_bulk_operation_to_files([path], 'set', ['TCOM'], 'Bach'), (1, 0), ext)
            self.assertEqual(get_metadata(path)['composer'], 'Bach', ext)
            self.assertEqual(mutagen.File(path).info.length > 0.2, True, ext)   # still audio

    def test_kinds(self):
        self.assertEqual([tf.kind(f"x.{e}") for e in ('MP3', 'wav', 'aif', 'm4b', 'opus', 'oga', 'aac', 'txt')],
                         ['id3', 'id3', 'id3', 'mp4', 'vorbis', 'vorbis', 'unsupported', 'unsupported'])
        self.assertEqual([tf.is_mp3(f"x.{e}") for e in ('mp3', 'wav')], [True, False])


if __name__ == "__main__":
    unittest.main()
