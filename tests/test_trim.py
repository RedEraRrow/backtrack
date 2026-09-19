"""Tests for the trim engine (src/trim/trim.py). Synthetic fixtures generated
at test time; skipped entirely when ffmpeg is absent."""
import os
import shutil
import subprocess
import tempfile
import unittest

from mutagen.id3 import ID3, APIC, TPE1, TXXX, TLEN, TDLY  # type: ignore[reportPrivateImportUsage]
from mutagen.mp3 import MP3

from src.trim import trim

RATES = (22050, 24000, 44100, 48000)


def _make_fixture(path: str, sample_rate: int, bitrate_args: list[str]) -> None:
    """tone / silence / tone / silence / tone, per section 9."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=300:duration=2:sample_rate={sample_rate}",
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo:d=0.5",
        "-f", "lavfi", "-i", f"sine=frequency=600:duration=4:sample_rate={sample_rate}",
        "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl=stereo:d=0.5",
        "-f", "lavfi", "-i", f"sine=frequency=200:duration=2:sample_rate={sample_rate}",
        "-filter_complex", "[0][1][2][3][4]concat=n=5:v=0:a=1",
        "-ar", str(sample_rate), "-ac", "2", "-c:a", "libmp3lame",
        *bitrate_args, path,
    ], check=True, capture_output=True)


def _strip_tags(path: str) -> bytes:
    """Raw bytes with any ID3v2 header removed (v2.4 tags are variable-length,
    so re-read the file rather than assume the original header size)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] != b"ID3":
        return data
    size = ((data[6] & 0x7f) << 21) | ((data[7] & 0x7f) << 14) | \
           ((data[8] & 0x7f) << 7) | (data[9] & 0x7f)
    return data[10 + size:]


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class TrimEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fixture(self, rate=44100, vbr=False, tag=True) -> str:
        path = os.path.join(self.tmp, f"src_{rate}_{'vbr' if vbr else 'cbr'}.mp3")
        bitrate_args = ["-q:a", "4"] if vbr else ["-b:a", "64k"]
        _make_fixture(path, rate, bitrate_args)
        if tag:
            audio = ID3(path)
            audio.add(TPE1(encoding=3, text=["Test Artist"]))
            audio.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=["abc-123"]))
            audio.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=b"\x89PNG\r\n"))
            audio.save(path, v2_version=3)
        return path

    # -- stream copy: output bytes appear verbatim in the source (section 2.1) --
    def test_stream_copy_is_verbatim(self):
        src = self._fixture()
        out = os.path.join(self.tmp, "out.mp3")
        r = trim.cut_stream(src, out, in_s=2.0, out_s=4.0)
        self.assertTrue(r.ok, r.error)
        out_bytes = _strip_tags(out)
        src_bytes = _strip_tags(src)
        mid = out_bytes[len(out_bytes) // 4: len(out_bytes) // 4 + 512]
        self.assertIn(mid, src_bytes)

    # -- duration: out - in, not affected by a -to-style origin bug (section 2.4) --
    def test_duration_matches_cut_length_cbr(self):
        self._assert_duration_matches(vbr=False)

    def test_duration_matches_cut_length_vbr(self):
        self._assert_duration_matches(vbr=True)

    def _assert_duration_matches(self, vbr: bool):
        src = self._fixture(vbr=vbr)
        out = os.path.join(self.tmp, "out.mp3")
        r = trim.cut_stream(src, out, in_s=1.0, out_s=6.0)
        self.assertTrue(r.ok, r.error)
        frame_dur = trim.probe_frame_duration(src)
        expected = r.snapped_out - r.snapped_in
        actual = MP3(out).info.length
        self.assertLess(abs(actual - expected), frame_dur * 1.5)

    # -- format identity: sample rate and channel count survive (section 2.2) --
    def test_format_identity_across_rates(self):
        for rate in RATES:
            with self.subTest(rate=rate):
                src = self._fixture(rate=rate)
                out = os.path.join(self.tmp, f"out_{rate}.mp3")
                r = trim.cut_stream(src, out, in_s=1.0, out_s=5.0)
                self.assertTrue(r.ok, r.error)
                src_info = MP3(src).info
                out_info = MP3(out).info
                self.assertEqual(src_info.sample_rate, out_info.sample_rate)  # type: ignore[reportAttributeAccessIssue]
                self.assertEqual(src_info.channels, out_info.channels)  # type: ignore[reportAttributeAccessIssue]

    # -- tags: everything survives except TLEN/TDLY; provenance is added (section 3.1/3.2/3.5) --
    def test_tags_survive_except_length_frames(self):
        src = self._fixture()
        audio = ID3(src)
        audio.add(TLEN(encoding=3, text=["8000"]))
        audio.add(TDLY(encoding=3, text=["250"]))
        audio.save(src, v2_version=3)

        out = os.path.join(self.tmp, "out.mp3")
        r = trim.cut_stream(src, out, in_s=1.0, out_s=5.0)
        self.assertTrue(r.ok, r.error)

        out_tags = ID3(out)
        self.assertNotIn('TLEN', out_tags)
        self.assertNotIn('TDLY', out_tags)
        self.assertEqual(out_tags.getall('TPE1')[0].text, ["Test Artist"])
        self.assertEqual(out_tags.getall('APIC')[0].data, b"\x89PNG\r\n")
        self.assertIn('TXXX:MusicBrainz Album Id', out_tags)
        prov = out_tags.getall('TXXX:BACKTRACK_TRIM')
        self.assertEqual(len(prov), 1)
        self.assertIn(f"in={r.snapped_in:.3f}", prov[0].text[0])
        self.assertIn(f"out={r.snapped_out:.3f}", prov[0].text[0])

    def test_zero_tdly_is_kept(self):
        src = self._fixture()
        audio = ID3(src)
        audio.add(TDLY(encoding=3, text=["0"]))
        audio.save(src, v2_version=3)

        out = os.path.join(self.tmp, "out.mp3")
        r = trim.cut_stream(src, out, in_s=1.0, out_s=5.0)
        self.assertTrue(r.ok, r.error)
        self.assertIn('TDLY', ID3(out))

    # -- Xing/Info header rewritten for the new length (section 3.4) --
    def test_xing_header_rewritten(self):
        src = self._fixture(vbr=True)
        out = os.path.join(self.tmp, "out.mp3")
        r = trim.cut_stream(src, out, in_s=1.0, out_s=6.0)
        self.assertTrue(r.ok, r.error)
        expected = r.snapped_out - r.snapped_in
        self.assertLess(abs(MP3(out).info.length - expected), trim.probe_frame_duration(src) * 1.5)

    # -- snapping: mid-frame requests snap outward, reported value is the snapped one (section 2.3) --
    def test_snapping_rounds_outward(self):
        src = self._fixture()
        frame_dur = trim.probe_frame_duration(src)
        requested_in = 1.0 + frame_dur * 0.5   # mid-frame
        requested_out = 5.0 + frame_dur * 0.5  # mid-frame
        out = os.path.join(self.tmp, "out.mp3")
        r = trim.cut_stream(src, out, in_s=requested_in, out_s=requested_out)
        self.assertTrue(r.ok, r.error)
        self.assertLessEqual(r.snapped_in, requested_in)
        self.assertGreaterEqual(r.snapped_out, requested_out)

    def test_non_mp3_is_refused(self):
        path = os.path.join(self.tmp, "t.m4a")
        open(path, "wb").close()
        r = trim.cut_stream(path, os.path.join(self.tmp, "out.m4a"), in_s=0, out_s=1)
        self.assertFalse(r.ok)
        self.assertIn("MP3", r.error or "")


class SnapPureTest(unittest.TestCase):
    """No ffmpeg needed — these are plain arithmetic."""

    def test_snap_in_exact_multiple_stays_put(self):
        fd = 1152 / 44100
        self.assertAlmostEqual(trim.snap_in_point(fd * 10, fd), fd * 10, places=9)

    def test_snap_out_exact_multiple_stays_put(self):
        fd = 1152 / 44100
        self.assertAlmostEqual(trim.snap_out_point(fd * 10, fd), fd * 10, places=9)

    def test_snap_in_rounds_down(self):
        fd = 1152 / 44100
        self.assertAlmostEqual(trim.snap_in_point(fd * 10.5, fd), fd * 10, places=9)

    def test_snap_out_rounds_up(self):
        fd = 1152 / 44100
        self.assertAlmostEqual(trim.snap_out_point(fd * 10.5, fd), fd * 11, places=9)


if __name__ == "__main__":
    unittest.main()
