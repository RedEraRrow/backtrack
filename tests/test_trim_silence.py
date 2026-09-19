"""Tests for silence detection (src/trim/trim.py, section 4.3) — the
baseline cut-point suggestion when there's no sting to match against."""
import os
import shutil
import subprocess
import tempfile
import unittest

from src.trim import trim


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class DetectSilenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fixture(self, tone_head=5, silence=2, tone_tail=10) -> str:
        path = os.path.join(self.tmp, "t.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={tone_head}",
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={silence}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={tone_tail}",
            "-filter_complex", "concat=n=3:v=0:a=1",
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
        ], check=True, capture_output=True)
        return path

    def test_finds_the_silent_gap_with_absolute_timestamps(self):
        path = self._fixture(tone_head=5, silence=2, tone_tail=10)
        pairs = trim.detect_silence(path, 0.0, 17.0)
        self.assertEqual(len(pairs), 1)
        start, end = pairs[0]
        self.assertAlmostEqual(start, 5.0, delta=0.1)
        self.assertAlmostEqual(end, 7.0, delta=0.1)

    def test_window_offset_is_reflected_in_absolute_times(self):
        # Scanning only the back half of the file: the silence found inside
        # the window must still be reported in the *file's* timeline.
        path = self._fixture(tone_head=5, silence=2, tone_tail=10)
        pairs = trim.detect_silence(path, 4.0, 13.0)
        self.assertEqual(len(pairs), 1)
        start, end = pairs[0]
        self.assertAlmostEqual(start, 5.0, delta=0.1)
        self.assertAlmostEqual(end, 7.0, delta=0.1)

    def test_no_silence_found_when_none_exists(self):
        path = os.path.join(self.tmp, "no_gap.mp3")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=10",
                        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path],
                       check=True, capture_output=True)
        self.assertEqual(trim.detect_silence(path, 0.0, 10.0), [])

    def test_silence_still_running_at_window_edge_is_closed_there(self):
        path = self._fixture(tone_head=5, silence=10, tone_tail=5)
        # Scan a window that ends inside the silent stretch.
        pairs = trim.detect_silence(path, 0.0, 8.0)
        self.assertEqual(len(pairs), 1)
        start, end = pairs[0]
        self.assertAlmostEqual(start, 5.0, delta=0.1)
        self.assertAlmostEqual(end, 8.0, delta=0.1)  # closed at the window edge, not 15.0


if __name__ == "__main__":
    unittest.main()
