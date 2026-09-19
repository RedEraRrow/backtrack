"""Tests for loudness measurement (src/trim/trim.py, section 5.4). Synthetic
fixtures generated at test time; skipped when ffmpeg is absent."""
import os
import shutil
import subprocess
import tempfile
import unittest

from src.trim import trim


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class MeasureLoudnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tone(self, name: str, volume_filter: str = "") -> str:
        path = os.path.join(self.tmp, name)
        filt = f"volume={volume_filter}" if volume_filter else "anull"
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=10",
            "-af", filt,
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
        ], check=True, capture_output=True)
        return path

    def test_quiet_copy_measures_lower_by_the_offset(self):
        loud = self._tone("loud.mp3")
        quiet = self._tone("quiet.mp3", "-6dB")

        loud_measured = trim.measure_loudness(loud)
        quiet_measured = trim.measure_loudness(quiet)
        self.assertIsNotNone(loud_measured)
        self.assertIsNotNone(quiet_measured)
        loud_lufs, _ = loud_measured
        quiet_lufs, _ = quiet_measured
        self.assertAlmostEqual(loud_lufs - quiet_lufs, 6.0, delta=0.3)

    def test_gain_is_the_difference_from_target(self):
        path = self._tone("t.mp3")
        result = trim.measure_track_gain(path, target_lufs=-18.0)
        self.assertIsNotNone(result)
        integrated = result['integrated_lufs']
        self.assertAlmostEqual(result['gain_db'], -18.0 - integrated, places=2)

    def test_track_already_at_target_gets_near_zero_gain(self):
        # Measure once to find this fixture's actual level, then target it.
        path = self._tone("t.mp3")
        measured = trim.measure_loudness(path)
        self.assertIsNotNone(measured)
        integrated, _ = measured
        result = trim.measure_track_gain(path, target_lufs=integrated)
        self.assertAlmostEqual(result['gain_db'], 0.0, delta=0.05)

    def test_clip_flagged_when_gain_would_push_peak_over_0dbfs(self):
        path = self._tone("loud.mp3")
        result = trim.measure_track_gain(path, target_lufs=20.0)  # absurdly high, forces clipping
        self.assertTrue(result['clips'])

    def test_no_clip_for_a_conservative_target(self):
        path = self._tone("quiet.mp3", "-20dB")
        result = trim.measure_track_gain(path, target_lufs=-18.0)
        self.assertFalse(result['clips'])


if __name__ == "__main__":
    unittest.main()
