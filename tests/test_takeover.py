"""DAC Takeover: music gets the output to itself, untouched; audiobooks
never. And the Info panel's Output line says what reaches the output."""
import unittest
from unittest import mock

from mutagen.id3 import ID3, EQU2, RVA2  # type: ignore[reportPrivateImportUsage]

from backtrack.playback import session as sess


def _tags(path):
    audio = ID3()
    audio.add(EQU2(method=1, desc='', adjustments=[(1000, 3.0)]))
    audio.add(RVA2(desc='', channel=1, gain=-6.0, peak=0.0))
    return audio


class _MP:
    current_ao, af, volume, audio_device = 'coreaudio_exclusive', [], 100, 'auto'
    audio_params = {'samplerate': 44100}
    audio_out_params = {'samplerate': 44100, 'format': 'float', 'channel-count': 2}


class TakeoverTest(unittest.TestCase):
    def options(self, path, takeover, book=False):
        with mock.patch.object(sess, '_takeover_on', lambda: takeover), \
             mock.patch.object(sess, 'player_tags', _tags), \
             mock.patch.object(sess, 'is_audiobook', lambda p: book), \
             mock.patch.object(sess.coreaudio, 'remember_rate', lambda f, uid="": None):
            s = sess.PlaybackSession()
            s.mp = _MP()
            return s._track_options(path)

    def test_music_takes_over_untouched_and_books_never(self):
        self.assertEqual(self.options("/m/a.flac", True), "af=%0%,speed=%1%1,audio-exclusive=%3%yes,volume=%3%100")
        self.assertIn("equalizer", self.options("/m/a.flac", False))                # off: as ever
        self.assertNotIn("exclusive", self.options("/b/a.m4b", True, book=True))     # a book: shared

    def test_the_output_line(self):
        s = sess.PlaybackSession()
        s.mp, s.file_path = _MP(), "/m/a.flac"
        with mock.patch.object(sess.coreaudio, 'is_bluetooth', lambda d: False):
            self.assertEqual(s.output_note(), "PCM · 44.1 kHz · 32-bit float · stereo")
        with mock.patch.object(sess.coreaudio, 'is_bluetooth', lambda d: True):
            self.assertEqual(s.output_note(), "PCM · 44.1 kHz · 32-bit float · stereo → Bluetooth")
        s.file_path = None
        self.assertEqual(s.output_note(), "")                                   # nothing playing

if __name__ == '__main__':
    unittest.main()
