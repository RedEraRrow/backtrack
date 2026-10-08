"""Tests for _apply_equalizer's RVA2 preamp support (backtrack/playback/session.py).
Without it, an RVA2 gain tag (what the trimmer's
ReplayGain operation writes) has no audible effect in backtrack's own
playback. A fake player stands in for vlc.MediaPlayer; vlc.AudioEqualizer
itself is the real libvlc object."""
import unittest

from mutagen.id3 import ID3, EQU2, RVA2  # type: ignore[reportPrivateImportUsage]

from backtrack.playback import session
from backtrack.playback.libvlc import vlc


class FakePlayer:
    def __init__(self):
        self.eq = None

    def set_equalizer(self, eq):
        self.eq = eq
        return 0


@unittest.skipUnless(vlc, "VLC (libvlc) not installed")
class ApplyEqualizerRVA2Test(unittest.TestCase):
    def test_rva2_master_channel_becomes_preamp(self):
        audio = ID3()
        audio.add(RVA2(desc='', channel=1, gain=-3.3, peak=0.0))
        mp = FakePlayer()
        self.assertTrue(session._apply_equalizer(mp, audio))
        self.assertAlmostEqual(mp.eq.get_preamp(), -3.3, places=1)

    def test_non_master_channel_is_ignored(self):
        audio = ID3()
        audio.add(RVA2(desc='', channel=2, gain=-3.3, peak=0.0))  # front-right, not master
        mp = FakePlayer()
        self.assertFalse(session._apply_equalizer(mp, audio))

    def test_equ2_and_rva2_combine(self):
        audio = ID3()
        audio.add(EQU2(method=1, desc='', adjustments=[(1000, 6.0)]))
        audio.add(RVA2(desc='', channel=1, gain=2.0, peak=0.0))
        mp = FakePlayer()
        self.assertTrue(session._apply_equalizer(mp, audio))
        self.assertAlmostEqual(mp.eq.get_preamp(), 2.0, places=1)
        # At least one band should have picked up the +6dB adjustment.
        import vlc
        band_count = vlc.libvlc_audio_equalizer_get_band_count()
        self.assertTrue(any(mp.eq.get_amp_at_index(i) != 0.0 for i in range(band_count)))

    def test_no_frames_is_not_applied(self):
        audio = ID3()
        mp = FakePlayer()
        self.assertFalse(session._apply_equalizer(mp, audio))
        self.assertIsNone(mp.eq)

    def test_preamp_clamped_to_limit(self):
        from backtrack import tuning as tune
        audio = ID3()
        audio.add(RVA2(desc='', channel=1, gain=60.0, peak=0.0))  # RVA2's own valid range, still over the eq limit
        mp = FakePlayer()
        self.assertTrue(session._apply_equalizer(mp, audio))
        self.assertLessEqual(mp.eq.get_preamp(), tune.EQ_GAIN_LIMIT_DB + 0.01)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(vlc, "VLC (libvlc) not installed")
class ReplayGainPreampTest(unittest.TestCase):
    def test_replaygain_text_alone_is_the_preamp(self):
        from mutagen.id3 import TXXX
        audio = ID3()
        audio.add(TXXX(encoding=3, desc='replaygain_track_gain', text=['-6.20 dB']))
        mp = FakePlayer()
        self.assertTrue(session._apply_equalizer(mp, audio))
        self.assertAlmostEqual(mp.eq.get_preamp(), -6.2, places=1)

    def test_rva2_wins_over_the_text(self):
        from mutagen.id3 import TXXX
        audio = ID3()
        audio.add(TXXX(encoding=3, desc='REPLAYGAIN_TRACK_GAIN', text=['-6.20 dB']))
        audio.add(RVA2(desc='', channel=1, gain=-2.0, peak=0.0))
        mp = FakePlayer()
        session._apply_equalizer(mp, audio)
        self.assertAlmostEqual(mp.eq.get_preamp(), -2.0, places=1)


class OtherFormatsInThePlayerTest(unittest.TestCase):
    """The player's view of non-ID3 tags, and gain written to every kind."""
    def setUp(self):
        import tempfile
        from _media import HAVE_FFMPEG
        if not HAVE_FFMPEG:
            self.skipTest("needs ffmpeg")
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_flac_shows_like_an_mp3(self):
        import mutagen
        from _media import make_audio
        path = make_audio(self.tmp, 'flac')
        f = mutagen.File(path)
        f.tags.update({'TITLE': ['Song'], 'ARTIST': ['A; B'], 'DISCNUMBER': ['2'], 'DISCTOTAL': ['3'],
                       'TRACKNUMBER': ['4'], 'DISCSUBTITLE': ['Live'], 'WORK': ['Mass'],
                       'PERFORMER': ['Ann (soprano)'], 'REPLAYGAIN_TRACK_GAIN': ['-4.00 dB'],
                       'LYRICS': ['[00:01.00]One\n[00:02.50]Two']})
        f.save()
        view = session.player_tags(path)
        self.assertEqual(str(view['TIT2']), 'Song')
        self.assertEqual(view['TPE1'].text, ['A', 'B'])
        self.assertEqual((str(view['TPOS']), str(view['TRCK']), str(view['TSST']), str(view['TIT1'])),
                         ('2/3', '4', 'Live', 'Mass'))
        self.assertEqual(view['TMCL'].people, [['soprano', 'Ann']])
        self.assertEqual(view.getall('SYLT')[0].text, [('One', 1000), ('Two', 2500)])
        self.assertEqual(view.getall('TXXX')[0].text, ['-4.00 dB'])

    def test_untimed_lyrics_are_plain(self):
        from mutagen.mp4 import MP4
        from _media import make_audio
        path = make_audio(self.tmp, 'm4a')
        f = MP4(path)
        f['\xa9lyr'] = ['Line one\nLine two']
        f.save()
        self.assertEqual(session.player_tags(path).getall('USLT')[0].text, 'Line one\nLine two')

    def test_gain_lands_in_every_kind(self):
        from _media import make_audio
        from backtrack.id3 import tag_writer as tw
        from backtrack.id3.tag_formats import open_tags, texts
        for ext in ('mp3', 'wav', 'flac', 'opus', 'm4a'):
            path = make_audio(self.tmp, ext, name=ext)
            tw.write_replaygain(path, -5.5, 0.912)
            tags = open_tags(path)[0]
            self.assertEqual((texts(tags, 'replaygain_track_gain'), texts(tags, 'replaygain_track_peak')),
                             (['-5.50 dB'], ['0.912000']), ext)
            self.assertEqual(bool(tags.getall('RVA2')) if ext in ('mp3', 'wav') else None,
                             True if ext in ('mp3', 'wav') else None, ext)
