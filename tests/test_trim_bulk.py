"""Tests for bulk candidate detection (src/trim/trim_bulk.py, section 5.1)
and sting seeding (section 5.2.1/5.2.2). Candidate detection is pure — no
file access, no ffmpeg; sting seeding needs real fixtures and is skipped
when ffmpeg is absent."""
import os
import shutil
import subprocess
import tempfile
import unittest

from src.trim import trim
from src.trim import trim_bulk as tb


def _track(path, artist, album, duration):
    return {'path': path, 'artist': artist, 'album': album, 'duration': duration}


class RoundDurationClusterTest(unittest.TestCase):
    def test_uniform_group_flagged_when_sibling_series_varies(self):
        # Series A: three episodes padded to exactly 32:00 (the spec's example).
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 32 * 60),
            _track('/a/2.mp3', 'Show', 'Series A', 32 * 60),
            _track('/a/3.mp3', 'Show', 'Series A', 32 * 60),
            # Series B by the same show, varying naturally — proves Series A's
            # uniformity isn't just "that's how long the show runs".
            _track('/b/1.mp3', 'Show', 'Series B', 27 * 60 + 24),
            _track('/b/2.mp3', 'Show', 'Series B', 28 * 60 + 12),
            _track('/b/3.mp3', 'Show', 'Series B', 27 * 60 + 50),
        ]
        results = tb.round_duration_candidates(library)
        flagged_albums = {r['album'] for r in results}
        self.assertIn('Series A', flagged_albums)
        self.assertNotIn('Series B', flagged_albums)
        series_a = next(r for r in results if r['album'] == 'Series A')
        self.assertEqual(series_a['count'], 3)
        self.assertEqual(set(series_a['paths']), {'/a/1.mp3', '/a/2.mp3', '/a/3.mp3'})

    def test_genuinely_consistent_show_is_not_a_false_positive(self):
        # Every series this show has ever made is uniformly 28:00 — that's
        # just the format, not padding (section 5.1's stated false positive).
        library = [
            _track('/a/1.mp3', 'Consistent Show', 'Series A', 28 * 60),
            _track('/a/2.mp3', 'Consistent Show', 'Series A', 28 * 60),
            _track('/b/1.mp3', 'Consistent Show', 'Series B', 28 * 60),
            _track('/b/2.mp3', 'Consistent Show', 'Series B', 28 * 60),
        ]
        results = tb.round_duration_candidates(library)
        self.assertEqual(results, [])

    def test_subset_cluster_within_a_varying_group(self):
        # Two episodes share an exact round duration while the rest of the
        # SAME group varies — a cluster inside one group, no cross-group check needed.
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 30 * 60),
            _track('/a/2.mp3', 'Show', 'Series A', 30 * 60),
            _track('/a/3.mp3', 'Show', 'Series A', 27 * 60 + 40),
            _track('/a/4.mp3', 'Show', 'Series A', 28 * 60 + 5),
        ]
        results = tb.round_duration_candidates(library)
        self.assertEqual(len(results), 1)
        self.assertEqual(set(results[0]['paths']), {'/a/1.mp3', '/a/2.mp3'})

    def test_non_round_uniform_duration_not_flagged(self):
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 27 * 60 + 13),
            _track('/a/2.mp3', 'Show', 'Series A', 27 * 60 + 13),
            _track('/b/1.mp3', 'Show', 'Series B', 20 * 60),
            _track('/b/2.mp3', 'Show', 'Series B', 40 * 60),
        ]
        self.assertEqual(tb.round_duration_candidates(library), [])

    def test_single_track_group_ignored(self):
        library = [_track('/a/1.mp3', 'Show', 'Series A', 30 * 60)]
        self.assertEqual(tb.round_duration_candidates(library), [])

    def test_non_mp3_excluded(self):
        library = [
            _track('/a/1.m4a', 'Show', 'Series A', 32 * 60),
            _track('/a/2.m4a', 'Show', 'Series A', 32 * 60),
            _track('/b/1.mp3', 'Show', 'Series B', 27 * 60),
            _track('/b/2.mp3', 'Show', 'Series B', 29 * 60),
        ]
        self.assertEqual(tb.round_duration_candidates(library), [])


class DurationOutlierTest(unittest.TestCase):
    def test_one_long_episode_flagged(self):
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 27 * 60 + 24),
            _track('/a/2.mp3', 'Show', 'Series A', 28 * 60 + 12),
            _track('/a/3.mp3', 'Show', 'Series A', 27 * 60 + 50),
            _track('/a/4.mp3', 'Show', 'Series A', 29 * 60 + 36),
        ]
        results = tb.duration_outliers(library)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['path'], '/a/4.mp3')

    def test_small_groups_not_scanned(self):
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 27 * 60),
            _track('/a/2.mp3', 'Show', 'Series A', 40 * 60),
        ]
        self.assertEqual(tb.duration_outliers(library), [])

    def test_no_outlier_when_all_similar(self):
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 27 * 60),
            _track('/a/2.mp3', 'Show', 'Series A', 27 * 60 + 10),
            _track('/a/3.mp3', 'Show', 'Series A', 27 * 60 + 20),
        ]
        self.assertEqual(tb.duration_outliers(library), [])


class GroupDurationMismatchTest(unittest.TestCase):
    def test_flags_the_longer_running_series(self):
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 32 * 60),
            _track('/a/2.mp3', 'Show', 'Series A', 32 * 60),
            _track('/b/1.mp3', 'Show', 'Series B', 20 * 60),
            _track('/b/2.mp3', 'Show', 'Series B', 20 * 60),
        ]
        results = tb.group_duration_mismatch(library)
        albums = {r['album'] for r in results}
        self.assertIn('Series A', albums)
        self.assertNotIn('Series B', albums)

    def test_single_album_artist_skipped(self):
        library = [
            _track('/a/1.mp3', 'Show', 'Series A', 32 * 60),
            _track('/a/2.mp3', 'Show', 'Series A', 32 * 60),
        ]
        self.assertEqual(tb.group_duration_mismatch(library), [])


class GroupByAlbumTest(unittest.TestCase):
    def test_skips_missing_duration_and_path(self):
        library = [
            {'path': '/a/1.mp3', 'artist': 'X', 'album': 'Y', 'duration': 0},
            {'path': '', 'artist': 'X', 'album': 'Y', 'duration': 100},
            {'path': '/a/2.mp3', 'artist': 'X', 'album': 'Y', 'duration': 100},
        ]
        groups = tb._group_by_album(library)
        self.assertEqual(len(groups[('X', 'Y')]), 1)


def _make_sting(path: str) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
        "-filter_complex", "concat=n=3:v=0:a=1",
        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
    ], check=True, capture_output=True)


def _make_episode(path: str, lead_in_s: float, programme_freq: int = 300) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=150:duration={lead_in_s}",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
        "-f", "lavfi", "-i", f"sine=frequency={programme_freq}:duration=20",
        "-filter_complex", "concat=n=5:v=0:a=1",
        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
    ], check=True, capture_output=True)


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class SeedBySting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sting_path = os.path.join(self.tmp, "sting.mp3")
        _make_sting(self.sting_path)
        self.sting_dur = 3.0
        self.reference_pcm = trim.decode_mono_pcm(self.sting_path, 0, self.sting_dur)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_anchor_keeps_sting_in_output(self):
        ep = os.path.join(self.tmp, "ep.mp3")
        _make_episode(ep, lead_in_s=2.0)
        results = tb.seed_by_sting(self.reference_pcm, self.sting_dur, 'start', 0.0, [ep])
        self.assertIsNotNone(results[ep])
        in_point, score = results[ep]
        self.assertGreater(score, 0.3)
        self.assertLess(abs(in_point - 2.0), 0.1)   # the sting's own start

    def test_end_anchor_drops_sting_from_output(self):
        ep = os.path.join(self.tmp, "ep.mp3")
        _make_episode(ep, lead_in_s=2.0)
        results = tb.seed_by_sting(self.reference_pcm, self.sting_dur, 'end', 0.0, [ep])
        self.assertIsNotNone(results[ep])
        in_point, score = results[ep]
        self.assertGreater(score, 0.3)
        self.assertLess(abs(in_point - (2.0 + self.sting_dur)), 0.1)  # the sting's own end

    def test_low_scoring_track_left_unseeded(self):
        no_sting = os.path.join(self.tmp, "no_sting.mp3")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "lavfi", "-i", "sine=frequency=222:duration=25",
                        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", no_sting],
                       check=True, capture_output=True)
        results = tb.seed_by_sting(self.reference_pcm, self.sting_dur, 'start', 0.0, [no_sting])
        self.assertIsNone(results[no_sting])

    def test_each_track_gets_its_own_offset_in_one_batch_call(self):
        """The sting is the same-ish sound on every episode, but its position
        varies episode to episode (different continuity, different padding) —
        one call over the whole group must find each track's own position
        independently, not reuse one offset across all of them."""
        eps = {}
        for name, lead_in in [("ep_a.mp3", 1.0), ("ep_b.mp3", 6.0), ("ep_c.mp3", 13.0)]:
            path = os.path.join(self.tmp, name)
            _make_episode(path, lead_in)
            eps[path] = lead_in

        results = tb.seed_by_sting(self.reference_pcm, self.sting_dur, 'start', 0.0, list(eps))

        found_offsets = []
        for path, true_lead_in in eps.items():
            self.assertIsNotNone(results[path], f"{path} should have matched")
            in_point, score = results[path]
            self.assertGreater(score, 0.3)
            self.assertLess(abs(in_point - true_lead_in), 0.1)
            found_offsets.append(round(in_point, 1))

        # Distinct positions, not the same offset copied to every track.
        self.assertEqual(len(set(found_offsets)), 3)


def _make_closing_episode(path: str, tail_pad_s: float, programme_freq: int = 300) -> None:
    """programme + the sting (a closing theme/beep) + tail padding after it."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency={programme_freq}:duration=20",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
        "-f", "lavfi", "-i", f"sine=frequency=150:duration={tail_pad_s}",
        "-filter_complex", "concat=n=5:v=0:a=1",
        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
    ], check=True, capture_output=True)


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class SeedBySting_Tail(unittest.TestCase):
    """Closing-sting seeding (section 5.2.2's tail case): the out-point
    anchors to the sting's end when it's wanted, or its start when it's not."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sting_path = os.path.join(self.tmp, "sting.mp3")
        _make_sting(self.sting_path)
        self.sting_dur = 3.0
        self.reference_pcm = trim.decode_mono_pcm(self.sting_path, 0, self.sting_dur)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_end_anchor_keeps_closing_theme(self):
        ep = os.path.join(self.tmp, "ep.mp3")
        _make_closing_episode(ep, tail_pad_s=2.0)
        # True sting position: 20s programme, sting from 20-23s, then 2s tail.
        results = tb.seed_by_sting(self.reference_pcm, self.sting_dur, 'end', 0.0,
                                   [ep], region='tail')
        self.assertIsNotNone(results[ep])
        out_point, score = results[ep]
        self.assertGreater(score, 0.3)
        self.assertLess(abs(out_point - 23.0), 0.1)

    def test_start_anchor_drops_closing_beep(self):
        ep = os.path.join(self.tmp, "ep.mp3")
        _make_closing_episode(ep, tail_pad_s=2.0)
        results = tb.seed_by_sting(self.reference_pcm, self.sting_dur, 'start', 0.0,
                                   [ep], region='tail')
        self.assertIsNotNone(results[ep])
        out_point, score = results[ep]
        self.assertGreater(score, 0.3)
        self.assertLess(abs(out_point - 20.0), 0.1)

    def test_window_bounds_tail_measures_from_the_end(self):
        ep = os.path.join(self.tmp, "ep.mp3")
        _make_closing_episode(ep, tail_pad_s=2.0)
        start_s, dur_s = tb._window_bounds(ep, window_s=10.0, region='tail')
        from mutagen.mp3 import MP3
        length = MP3(ep).info.length
        self.assertAlmostEqual(start_s, length - 10.0, places=1)
        self.assertAlmostEqual(start_s + dur_s, length, places=1)


def _make_history_episode(path: str, lead_in_s: float, programme_tremolo_f: float) -> None:
    """lead-in + the shared 3-tone sting + a programme tail — lead-in and
    programme are both tremolo-modulated (not flat) so they carry a genuine,
    non-degenerate envelope shape of their own: a *stationary* tone's log-RMS
    envelope is flat, and two flat regions correlate as a false "perfect
    match" regardless of content (an mp3-quantization-noise artifact, not a
    real one) — exactly the failure mode `_learn_sting_from_history`'s margin
    check guards against, so the fixture must not manufacture it by accident.
    `programme_tremolo_f` differs between episodes so their programme tails
    don't coincidentally resemble each other either."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=150:duration={lead_in_s}",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=20",
        "-filter_complex",
        "[0]tremolo=f=1.3:d=0.9[z0];"
        "[1]volume=1.0[a];[2]volume=0.25[b];[3]volume=1.0[c];"
        f"[4]tremolo=f={programme_tremolo_f}:d=0.9[z4];"
        "[z0][a][b][c][z4]concat=n=5:v=0:a=1",
        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
    ], check=True, capture_output=True)


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class LearnStingFromHistoryTest(unittest.TestCase):
    """`_learn_sting_from_history` (section 4.4): a shared sting learned from
    an already-committed trim in the same folder, direction (kept vs
    dropped) discovered by correlating each way against a fresh track —
    never assumed from which mark field the old commit happened to set."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.tmp, "backups")
        self._real_load_config = trim.load_config
        trim.load_config = lambda: {"trim_backup_dir": self.backup_dir, "trim_ffmpeg_path": ""}

    def tearDown(self):
        trim.load_config = self._real_load_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_none_without_history(self):
        new_ep = os.path.join(self.tmp, "new.mp3")
        _make_history_episode(new_ep, lead_in_s=6.0, programme_tremolo_f=3.7)
        self.assertIsNone(tb._learn_sting_from_history([new_ep], 'head', 90.0, 0.3))

    def test_detects_dropped_sting_from_an_old_commit(self):
        # Old episode: 2s lead-in + 3s sting, committed so the sting is cut
        # away entirely (kept content starts right after it, at 5.0s).
        old_ep = os.path.join(self.tmp, "old.mp3")
        _make_history_episode(old_ep, lead_in_s=2.0, programme_tremolo_f=2.1)
        r = trim.commit_trim(old_ep, in_s=5.0, out_s=20.0)
        self.assertTrue(r.ok, r.error)

        new_ep = os.path.join(self.tmp, "new.mp3")
        _make_history_episode(new_ep, lead_in_s=6.0, programme_tremolo_f=3.7)

        result = tb._learn_sting_from_history([new_ep], 'head', 90.0, 0.3)
        self.assertIsNotNone(result)
        backup_path, start, end, side = result
        self.assertTrue(os.path.exists(backup_path))
        self.assertEqual(side, 'end')
        self.assertLess(abs(start - 2.0), 0.5)
        self.assertLess(abs(end - 5.0), 0.5)

    def test_detects_kept_sting_from_an_old_commit(self):
        # Old episode: 2s lead-in, committed so the sting (2-5s) is retained
        # (kept content starts at the sting's own start, 2.0s).
        old_ep = os.path.join(self.tmp, "old.mp3")
        _make_history_episode(old_ep, lead_in_s=2.0, programme_tremolo_f=2.1)
        r = trim.commit_trim(old_ep, in_s=2.0, out_s=20.0)
        self.assertTrue(r.ok, r.error)

        new_ep = os.path.join(self.tmp, "new.mp3")
        _make_history_episode(new_ep, lead_in_s=6.0, programme_tremolo_f=3.7)

        result = tb._learn_sting_from_history([new_ep], 'head', 90.0, 0.3)
        self.assertIsNotNone(result)
        _backup_path, start, end, side = result
        self.assertEqual(side, 'start')
        self.assertLess(abs(start - 2.0), 0.5)
        self.assertLess(abs(end - 5.0), 0.5)

    def test_ignores_history_from_a_different_folder(self):
        other_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other_dir, ignore_errors=True)
        old_ep = os.path.join(other_dir, "old.mp3")
        _make_history_episode(old_ep, lead_in_s=2.0, programme_tremolo_f=2.1)
        trim.commit_trim(old_ep, in_s=5.0, out_s=20.0)

        new_ep = os.path.join(self.tmp, "new.mp3")
        _make_history_episode(new_ep, lead_in_s=6.0, programme_tremolo_f=3.7)
        self.assertIsNone(tb._learn_sting_from_history([new_ep], 'head', 90.0, 0.3))


if __name__ == "__main__":
    unittest.main()
