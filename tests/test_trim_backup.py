"""Tests for the trim engine's backup/undo store (section 6 of the trim spec).
Synthetic fixtures generated at test time; skipped when ffmpeg is absent.
`trim.load_config` is monkeypatched to an isolated backup dir so tests never
touch the real config/backup store.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from src.trim import trim


def _make_fixture(path: str, sample_rate: int = 44100) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"sine=frequency=300:duration=6:sample_rate={sample_rate}",
        "-ar", str(sample_rate), "-ac", "2", "-c:a", "libmp3lame", "-b:a", "64k", path,
    ], check=True, capture_output=True)


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.tmp, "backups")
        self._real_load_config = trim.load_config
        trim.load_config = lambda: {"trim_backup_dir": self.backup_dir, "trim_ffmpeg_path": ""}

    def tearDown(self):
        trim.load_config = self._real_load_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _track(self, name="t.mp3") -> str:
        path = os.path.join(self.tmp, name)
        _make_fixture(path)
        return path

    def test_restore_recovers_original_byte_for_byte(self):
        path = self._track()
        with open(path, "rb") as f:
            original_bytes = f.read()

        r = trim.commit_trim(path, in_s=1.0, out_s=4.0)
        self.assertTrue(r.ok, r.error)
        with open(path, "rb") as f:
            trimmed_bytes = f.read()
        self.assertNotEqual(trimmed_bytes, original_bytes)  # the file really changed

        entries = trim.backup_chain(path)
        self.assertEqual(len(entries), 1)
        restore = trim.restore_backup(entries[0]["id"])
        self.assertTrue(restore.ok, restore.error)

        with open(path, "rb") as f:
            restored_bytes = f.read()
        self.assertEqual(restored_bytes, original_bytes)

    def test_second_trim_leaves_first_backup_reachable(self):
        path = self._track()
        with open(path, "rb") as f:
            original_bytes = f.read()

        r1 = trim.commit_trim(path, in_s=1.0, out_s=5.0)
        self.assertTrue(r1.ok, r1.error)
        r2 = trim.commit_trim(path, in_s=0.5, out_s=3.0)
        self.assertTrue(r2.ok, r2.error)

        chain = trim.backup_chain(path)
        self.assertEqual(len(chain), 2)
        self.assertIsNone(chain[0]["parent_id"])
        self.assertEqual(chain[1]["parent_id"], chain[0]["id"])

        # The oldest entry is still the true, untrimmed original.
        restore = trim.restore_backup(chain[0]["id"])
        self.assertTrue(restore.ok, restore.error)
        with open(path, "rb") as f:
            restored_bytes = f.read()
        self.assertEqual(restored_bytes, original_bytes)

    def test_commit_trim_writes_in_place(self):
        path = self._track()
        from mutagen.mp3 import MP3
        orig_length = MP3(path).info.length
        r = trim.commit_trim(path, in_s=1.0, out_s=4.0)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.out_path, path)
        new_length = MP3(path).info.length
        self.assertLess(new_length, orig_length)

    def test_list_backups_reports_size(self):
        path = self._track()
        r = trim.commit_trim(path, in_s=1.0, out_s=4.0)
        self.assertTrue(r.ok, r.error)
        rows = trim.list_backups()
        self.assertEqual(len(rows), 1)
        self.assertGreater(rows[0]["size_bytes"], 0)

    def test_prune_removes_entry_and_file(self):
        path = self._track()
        r = trim.commit_trim(path, in_s=1.0, out_s=4.0)
        self.assertTrue(r.ok, r.error)
        entry_id = trim.backup_chain(path)[0]["id"]
        backup_file = trim._backup_dir() / f"{entry_id}.mp3"
        self.assertTrue(backup_file.exists())

        removed = trim.prune_backups({entry_id})
        self.assertEqual(removed, 1)
        self.assertFalse(backup_file.exists())
        self.assertEqual(trim.backup_chain(path), [])

    def test_restore_unknown_entry_errors(self):
        r = trim.restore_backup("does-not-exist")
        self.assertFalse(r.ok)

    def test_commit_trim_aborts_on_bad_range(self):
        path = self._track()
        with open(path, "rb") as f:
            original_bytes = f.read()
        r = trim.commit_trim(path, in_s=4.0, out_s=1.0)   # out before in
        self.assertFalse(r.ok)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), original_bytes)     # untouched
        self.assertEqual(trim.backup_chain(path), [])        # nothing backed up


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class LearnedStingSourceTest(unittest.TestCase):
    """`learned_sting_source` (section 4.4's "learn from correctly trimmed
    tracks"): the manifest entry to learn a shared sting from — a real prior
    trim in the same folder, not the current file's own history."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.tmp, "backups")
        self._real_load_config = trim.load_config
        trim.load_config = lambda: {"trim_backup_dir": self.backup_dir, "trim_ffmpeg_path": ""}

    def tearDown(self):
        trim.load_config = self._real_load_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _track(self, name="t.mp3", duration=10) -> str:
        path = os.path.join(self.tmp, name)
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"sine=frequency=300:duration={duration}",
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "64k", path,
        ], check=True, capture_output=True)
        return path

    def test_none_when_no_history(self):
        self.assertIsNone(trim.learned_sting_source(self.tmp, "head"))

    def test_finds_a_head_cut_in_the_same_folder(self):
        path = self._track()
        r = trim.commit_trim(path, in_s=2.0, out_s=8.0)
        self.assertTrue(r.ok, r.error)
        src = trim.learned_sting_source(self.tmp, "head")
        self.assertIsNotNone(src)
        self.assertTrue(os.path.exists(src["backup_path"]))
        self.assertGreater(src["snapped_in_s"], 1.5)

    def test_none_when_the_cut_side_had_nothing_removed(self):
        path = self._track()
        r = trim.commit_trim(path, in_s=0.0, out_s=8.0)  # nothing cut from the head
        self.assertTrue(r.ok, r.error)
        self.assertIsNone(trim.learned_sting_source(self.tmp, "head"))

    def test_ignores_entries_in_a_different_folder(self):
        other_dir = os.path.join(self.tmp, "other")
        os.makedirs(other_dir)
        path = os.path.join(other_dir, "t.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=10",
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "64k", path,
        ], check=True, capture_output=True)
        r = trim.commit_trim(path, in_s=2.0, out_s=8.0)
        self.assertTrue(r.ok, r.error)
        self.assertIsNone(trim.learned_sting_source(self.tmp, "head"))

    def test_excludes_given_paths(self):
        path = self._track()
        r = trim.commit_trim(path, in_s=2.0, out_s=8.0)
        self.assertTrue(r.ok, r.error)
        self.assertIsNone(trim.learned_sting_source(self.tmp, "head", exclude_paths={path}))

    def test_picks_the_most_recent_entry(self):
        old = self._track("old.mp3")
        trim.commit_trim(old, in_s=1.0, out_s=8.0)
        import time
        time.sleep(0.01)
        new = self._track("new.mp3")
        trim.commit_trim(new, in_s=3.0, out_s=8.0)
        src = trim.learned_sting_source(self.tmp, "head")
        self.assertEqual(src["original_path"], os.path.abspath(new))


if __name__ == "__main__":
    unittest.main()
