"""Headless tests for the pure half of src/trim/trim_editor.py: marks, undo,
join-audition windows, sibling context, segmented-editor apply. No terminal
or VLC — those need a live pass per docs/DEVELOPER.md's own testing note."""
import os
import shutil
import subprocess
import tempfile
import unittest

from src.trim import trim_editor as te

FRAME = 1152 / 44100   # a 44.1kHz MPEG-1 frame, ~26.12ms


class MarksTest(unittest.TestCase):
    def test_set_in_snaps_and_records_undo(self):
        m = te.Marks()
        undo = te.set_in(m, FRAME * 3.5, FRAME)
        self.assertAlmostEqual(m.in_snapped, FRAME * 3, places=9)
        self.assertEqual(undo, ('in', None, None))

    def test_set_out_snaps_outward(self):
        m = te.Marks()
        te.set_out(m, FRAME * 3.5, FRAME)
        self.assertAlmostEqual(m.out_snapped, FRAME * 4, places=9)

    def test_clear_in_resets_and_undo_restores(self):
        m = te.Marks()
        te.set_in(m, 10.0, FRAME)
        undo = te.clear_in(m)
        self.assertIsNone(m.in_snapped)
        te.apply_undo(m, undo)
        self.assertIsNotNone(m.in_snapped)

    def test_undo_restores_prior_value(self):
        m = te.Marks()
        u1 = te.set_in(m, 10.0, FRAME)
        u2 = te.set_in(m, 20.0, FRAME)
        te.apply_undo(m, u2)
        self.assertAlmostEqual(m.in_requested, 10.0)
        te.apply_undo(m, u1)
        self.assertIsNone(m.in_requested)

    def test_nudge_in_moves_by_exactly_one_frame(self):
        m = te.Marks()
        te.set_in(m, FRAME * 5, FRAME)
        te.nudge_in(m, FRAME, FRAME)
        self.assertAlmostEqual(m.in_snapped, FRAME * 6, places=9)

    def test_nudge_out_never_exceeds_track_length(self):
        m = te.Marks()
        te.set_out(m, 100.0, FRAME)
        te.nudge_out(m, 50.0, FRAME, track_length_s=110.0)
        self.assertLessEqual(m.out_snapped, 110.0)

    def test_resulting_duration_none_until_both_set(self):
        m = te.Marks()
        self.assertIsNone(te.resulting_duration(m))
        te.set_in(m, 1.0, FRAME)
        self.assertIsNone(te.resulting_duration(m))
        te.set_out(m, 5.0, FRAME)
        self.assertIsNotNone(te.resulting_duration(m))

    def test_resulting_duration_none_when_out_before_in(self):
        m = te.Marks()
        te.set_in(m, 5.0, FRAME)
        te.set_out(m, 1.0, FRAME)
        self.assertIsNone(te.resulting_duration(m))

    def test_removed_head_and_tail(self):
        m = te.Marks()
        te.set_in(m, 10.0, FRAME)
        te.set_out(m, 90.0, FRAME)
        self.assertAlmostEqual(te.removed_head(m), m.in_snapped)
        self.assertAlmostEqual(te.removed_tail(m, 100.0), 100.0 - m.out_snapped)

    def test_removed_head_zero_when_unset(self):
        self.assertEqual(te.removed_head(te.Marks()), 0.0)


class JoinClipsTest(unittest.TestCase):
    def test_both_marks_give_two_clips(self):
        m = te.Marks()
        te.set_in(m, 10.0, FRAME)
        te.set_out(m, 90.0, FRAME)
        clips = te.join_clips(m, track_length_s=100.0, lead_s=1.0, tail_s=1.0)
        self.assertEqual(len(clips), 2)
        (lo1, hi1, label1), (lo2, hi2, label2) = clips
        self.assertEqual(label1, 'in')
        self.assertAlmostEqual(hi1, m.in_snapped)
        self.assertAlmostEqual(lo1, m.in_snapped - 1.0)
        self.assertEqual(label2, 'out')
        self.assertAlmostEqual(lo2, m.out_snapped)
        self.assertAlmostEqual(hi2, m.out_snapped + 1.0)

    def test_missing_mark_drops_its_clip(self):
        m = te.Marks()
        te.set_in(m, 10.0, FRAME)
        clips = te.join_clips(m, track_length_s=100.0)
        self.assertEqual([c[2] for c in clips], ['in'])

    def test_no_marks_gives_no_clips(self):
        self.assertEqual(te.join_clips(te.Marks(), track_length_s=100.0), [])

    def test_tail_clip_clamped_to_track_length(self):
        m = te.Marks()
        te.set_out(m, 99.5, FRAME)
        clips = te.join_clips(m, track_length_s=100.0, tail_s=2.0)
        self.assertEqual(clips[0][1], 100.0)

    def test_in_clip_never_goes_negative(self):
        m = te.Marks()
        te.set_in(m, 0.3, FRAME)
        clips = te.join_clips(m, track_length_s=100.0, lead_s=1.0)
        self.assertGreaterEqual(clips[0][0], 0.0)


class SiblingDurationsTest(unittest.TestCase):
    def test_matches_by_album_and_artist(self):
        library = [
            {'path': '/a/1.mp3', 'album': 'X', 'artist': 'A', 'duration': 1800.0},
            {'path': '/a/2.mp3', 'album': 'X', 'artist': 'A', 'duration': 1800.0},
            {'path': '/a/3.mp3', 'album': 'X', 'artist': 'A', 'duration': 1650.0},
            {'path': '/b/1.mp3', 'album': 'Y', 'artist': 'B', 'duration': 900.0},
        ]
        self.assertEqual(sorted(te.sibling_durations(library, '/a/1.mp3')), [1650.0, 1800.0])

    def test_unknown_path_gives_no_siblings(self):
        self.assertEqual(te.sibling_durations([], '/nope.mp3'), [])


class EditFieldsTest(unittest.TestCase):
    def test_seed_reflects_current_marks(self):
        m = te.Marks()
        te.set_in(m, 65.5, FRAME)   # 1:05.500
        edit, orig = te._edit_seed(m)
        self.assertEqual("".join(edit['fields']['sm']), "01")
        self.assertEqual("".join(edit['fields']['ss']), "05")
        self.assertEqual("".join(edit['fields']['sms']), "500")

    def test_apply_only_commits_changed_bound(self):
        m = te.Marks()
        te.set_in(m, 10.0, FRAME)
        te.set_out(m, 90.0, FRAME)   # seeds em/es/ems as 01:30.000
        edit, orig = te._edit_seed(m)
        edit['fields']['em'] = list("02")   # change the "end minutes" field only
        undos = te._edit_apply(m, edit, orig, FRAME)
        self.assertEqual(len(undos), 1)
        self.assertAlmostEqual(m.out_requested, 150.0)   # 02:30.000
        self.assertAlmostEqual(m.in_requested, 10.0)     # untouched

    def test_apply_with_no_changes_commits_nothing(self):
        m = te.Marks()
        te.set_in(m, 10.0, FRAME)
        edit, orig = te._edit_seed(m)
        undos = te._edit_apply(m, edit, orig, FRAME)
        self.assertEqual(undos, [])


class EditFieldKeyTest(unittest.TestCase):
    def test_digit_fills_left_and_tab_advances(self):
        edit = {'fields': {k: [] for k in te._EDIT_ORDER}, 'fi': 0, 'pos': 0}
        te._edit_field_key(edit, '5')
        self.assertEqual(edit['fields']['sm'], ['5'])
        te._edit_field_key(edit, 'TAB')
        self.assertEqual(edit['fi'], 1)

    def test_up_down_spins_value(self):
        edit = {'fields': {k: [] for k in te._EDIT_ORDER}, 'fi': 1, 'pos': 0}
        edit['fields']['ss'] = list("30")
        te._edit_field_key(edit, 'UP')
        self.assertEqual("".join(edit['fields']['ss']), "31")


def _make_history_episode(path: str, lead_in_s: float, lead_tremolo_f: float, programme_tremolo_f: float) -> None:
    """lead-in + the shared 3-tone sting + a programme tail — see
    test_trim_bulk.py's identical helper for why lead-in/programme are both
    tremolo-modulated (not flat). Unlike that helper, the lead-in's own
    tremolo rate also varies per episode here: this test asserts on the exact
    correlated offset (not just which direction wins), and a lead-in that
    resembles another episode's lead-in too closely creates a second,
    near-tied correlation peak near position 0 that can beat the real one by
    the barest numerical margin — flaky in exactly the way find_sting's own
    peak-over-runner-up score is meant to catch for the real sting, just not
    reliably at this fixture's coarser scale."""
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=150:duration={lead_in_s}",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=300:duration=20",
        "-filter_complex",
        f"[0]tremolo=f={lead_tremolo_f}:d=0.9[z0];"
        "[1]volume=1.0[a];[2]volume=0.25[b];[3]volume=1.0[c];"
        f"[4]tremolo=f={programme_tremolo_f}:d=0.9[z4];"
        "[z0][a][b][c][z4]concat=n=5:v=0:a=1",
        "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", path,
    ], check=True, capture_output=True)


@unittest.skipUnless(te.trim.HAS_FFMPEG, "ffmpeg not installed")
class StingSuggestionTest(unittest.TestCase):
    """`_sting_suggestion` (section 4.4): single-track parity with the bulk
    conveyor's "learn from an earlier trim" — a lone edit shouldn't have to
    rediscover or mark by hand a sting some other track in the same folder
    was already correctly trimmed against."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.tmp, "backups")
        self._real_load_config = te.trim.load_config
        te.trim.load_config = lambda: {"trim_backup_dir": self.backup_dir, "trim_ffmpeg_path": ""}

    def tearDown(self):
        te.trim.load_config = self._real_load_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_none_without_history(self):
        new_ep = os.path.join(self.tmp, "new.mp3")
        _make_history_episode(new_ep, lead_in_s=6.0, lead_tremolo_f=4.9, programme_tremolo_f=3.7)
        self.assertIsNone(te._sting_suggestion(new_ep, 'head', 90.0, 0.3))

    def test_suggests_in_point_from_a_dropped_sting_in_the_same_folder(self):
        old_ep = os.path.join(self.tmp, "old.mp3")
        _make_history_episode(old_ep, lead_in_s=2.0, lead_tremolo_f=1.3, programme_tremolo_f=2.1)
        r = te.trim.commit_trim(old_ep, in_s=5.0, out_s=20.0)
        self.assertTrue(r.ok, r.error)

        new_ep = os.path.join(self.tmp, "new.mp3")
        _make_history_episode(new_ep, lead_in_s=6.0, lead_tremolo_f=4.9, programme_tremolo_f=3.7)

        result = te._sting_suggestion(new_ep, 'head', 90.0, 0.3)
        self.assertIsNotNone(result)
        mark_s, score = result
        self.assertGreater(score, 0.3)
        self.assertLess(abs(mark_s - 9.0), 0.5)  # the sting's own end: 6.0 lead-in + 3.0 sting, dropped


if __name__ == "__main__":
    unittest.main()
