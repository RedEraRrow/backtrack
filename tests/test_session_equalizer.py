"""Tests for _apply_equalizer's RVA2 preamp support (src/playback/session.py,
section 5.4.3) — without this, an RVA2 gain tag (what the trimmer's
ReplayGain operation writes) has no audible effect in backtrack's own
playback. A fake player stands in for vlc.MediaPlayer; vlc.AudioEqualizer
itself is the real libvlc object."""
import unittest

from mutagen.id3 import ID3, EQU2, RVA2  # type: ignore[reportPrivateImportUsage]

from src.playback import session


class FakePlayer:
    def __init__(self):
        self.eq = None

    def set_equalizer(self, eq):
        self.eq = eq
        return 0


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
        count = 0
        import vlc
        band_count = vlc.libvlc_audio_equalizer_get_band_count()
        self.assertTrue(any(mp.eq.get_amp_at_index(i) != 0.0 for i in range(band_count)))

    def test_no_frames_is_not_applied(self):
        audio = ID3()
        mp = FakePlayer()
        self.assertFalse(session._apply_equalizer(mp, audio))
        self.assertIsNone(mp.eq)

    def test_preamp_clamped_to_limit(self):
        from src import tuning as tune
        audio = ID3()
        audio.add(RVA2(desc='', channel=1, gain=60.0, peak=0.0))  # RVA2's own valid range, still over the eq limit
        mp = FakePlayer()
        self.assertTrue(session._apply_equalizer(mp, audio))
        self.assertLessEqual(mp.eq.get_preamp(), tune.EQ_GAIN_LIMIT_DB + 0.01)


if __name__ == "__main__":
    unittest.main()
