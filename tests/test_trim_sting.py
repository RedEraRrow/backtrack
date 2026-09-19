"""Tests for sting matching (src/trim/trim.py, section 4.4). Synthetic
fixtures generated at test time; skipped when ffmpeg is absent.

The matched offset is a *seed*, not a final placement — section 4.4 already
treats the join audition as an approximation the user nudges by ear, and the
same applies here. Empirically (see the tolerance below) the envelope method
lands within a handful of frames, not necessarily one; that's still a huge
head start over marking blind, which is what it's for.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from src.trim import trim

# A few MPEG frames' worth of slack — the envelope hop (10ms) plus the
# inherent fuzziness of locating a transient from a smoothed RMS envelope.
_OFFSET_TOLERANCE_S = 0.08


def _make_sting(path: str, sample_rate: int = 44100) -> None:
    """A short, distinctive 3-tone sequence — stands in for a real sting."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
        "-filter_complex", "concat=n=3:v=0:a=1",
        "-ar", str(sample_rate), "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
    ], check=True, capture_output=True)


def _make_episode(path: str, lead_in_s: float, sting_filter: str,
                  programme_freq: int, sample_rate: int = 44100) -> None:
    """lead-in tone (not silence — see trim.py's median/MAD note) + the sting
    (optionally filtered) + programme content, one concat/encode pass."""
    inputs: list[str] = []
    parts: list[str] = []
    idx = 0
    if lead_in_s > 0:
        inputs += ["-f", "lavfi", "-i", f"sine=frequency=150:duration={lead_in_s}"]
        parts.append(f"[{idx}]"); idx += 1
    inputs += ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    inputs += ["-f", "lavfi", "-i", "sine=frequency=880:duration=1"]
    inputs += ["-f", "lavfi", "-i", "sine=frequency=660:duration=1"]
    parts += [f"[{idx}]", f"[{idx + 1}]", f"[{idx + 2}]"]; idx += 3
    inputs += ["-f", "lavfi", "-i", f"sine=frequency={programme_freq}:duration=20"]
    parts.append(f"[{idx}]")
    filt = "".join(parts) + f"concat=n={len(parts)}:v=0:a=1"
    if sting_filter:
        filt += f",{sting_filter}"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
         "-filter_complex", filt, "-ar", str(sample_rate), "-ac", "2",
         "-c:a", "libmp3lame", "-b:a", "128k", path],
        check=True, capture_output=True)


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class FindStingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sting_path = os.path.join(self.tmp, "sting.mp3")
        _make_sting(self.sting_path)
        self.ref_pcm = trim.decode_mono_pcm(self.sting_path, 0, 3.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _episode(self, name, lead_in_s, sting_filter, programme_freq=300):
        path = os.path.join(self.tmp, name)
        _make_episode(path, lead_in_s, sting_filter, programme_freq)
        return path

    def test_exact_copy_at_start(self):
        ep = self._episode("ep0.mp3", 0.0, "")
        tgt = trim.decode_mono_pcm(ep, 0, 15.0)
        offset, score = trim.find_sting(self.ref_pcm, tgt)
        self.assertLess(abs(offset - 0.0), _OFFSET_TOLERANCE_S)
        self.assertGreater(score, 0.9)

    def test_minus_6db(self):
        ep = self._episode("ep_m6.mp3", 2.0, "volume=-6dB")
        tgt = trim.decode_mono_pcm(ep, 0, 15.0)
        offset, score = trim.find_sting(self.ref_pcm, tgt)
        self.assertLess(abs(offset - 2.0), _OFFSET_TOLERANCE_S)
        self.assertGreater(score, 0.5)

    def test_plus_3db(self):
        ep = self._episode("ep_p3.mp3", 5.0, "volume=+3dB")
        tgt = trim.decode_mono_pcm(ep, 0, 15.0)
        offset, score = trim.find_sting(self.ref_pcm, tgt)
        self.assertLess(abs(offset - 5.0), _OFFSET_TOLERANCE_S)
        self.assertGreater(score, 0.5)

    def test_lowpass(self):
        ep = self._episode("ep_lp.mp3", 1.0, "lowpass=f=2000")
        tgt = trim.decode_mono_pcm(ep, 0, 15.0)
        offset, score = trim.find_sting(self.ref_pcm, tgt)
        self.assertLess(abs(offset - 1.0), _OFFSET_TOLERANCE_S)
        self.assertGreater(score, 0.5)

    def test_track_without_the_sting_scores_low(self):
        """A confidently wrong offset is worse than admitting no match — the
        score must separate a real hit from a track that never had the sting."""
        path = os.path.join(self.tmp, "no_sting.mp3")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=222:duration=25",
                        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path],
                       check=True, capture_output=True)
        tgt = trim.decode_mono_pcm(path, 0, 15.0)
        _, score = trim.find_sting(self.ref_pcm, tgt)
        self.assertLess(score, 0.1)

    def test_too_short_target_is_handled(self):
        tgt = trim.decode_mono_pcm(self.sting_path, 0, 0.5)  # shorter than the reference
        offset, score = trim.find_sting(self.ref_pcm, tgt)
        self.assertEqual((offset, score), (0.0, 0.0))


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class FindCandidateStingsTest(unittest.TestCase):
    """Auto-discovery (section 5.2.1): given two tracks from the same group,
    find the segment they share, without a pre-extracted reference clip."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _episode(self, name, lead_in_s, programme_freq):
        path = os.path.join(self.tmp, name)
        _make_episode(path, lead_in_s, "", programme_freq)
        return path

    def test_finds_the_shared_segment_between_two_episodes(self):
        ep1 = self._episode("ep1.mp3", 3.0, 300)
        ep2 = self._episode("ep2.mp3", 7.0, 310)
        pcm_a = trim.decode_mono_pcm(ep1, 0, 30.0)
        pcm_b = trim.decode_mono_pcm(ep2, 0, 30.0)
        candidates = trim.find_candidate_stings(pcm_a, pcm_b)
        self.assertTrue(candidates)
        best = candidates[0]
        self.assertLess(abs(best['start'] - 3.0), _OFFSET_TOLERANCE_S)

    def test_no_candidates_when_nothing_is_shared(self):
        ep1 = self._episode("ep1.mp3", 3.0, 300)
        path = os.path.join(self.tmp, "unrelated.mp3")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=222:duration=30",
                        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path],
                       check=True, capture_output=True)
        pcm_a = trim.decode_mono_pcm(ep1, 0, 30.0)
        pcm_b = trim.decode_mono_pcm(path, 0, 30.0)
        self.assertEqual(trim.find_candidate_stings(pcm_a, pcm_b), [])


if __name__ == "__main__":
    unittest.main()
