"""Tests for chapter handling (src/trim/trim.py, section 3.3): the pure
classify/rebase/clamp transforms, and the CHAP/CTOC read/write round-trip.
Fixture mirrors section 9's guidance: one chapter survives, one is
destroyed, one straddles the in-point.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

from mutagen.id3 import ID3, CHAP, CTOC, CTOCFlags, TIT2  # type: ignore[reportPrivateImportUsage]

from src.trim import trim

# A 60s track, cut to keep [20_000ms, 50_000ms):
#   intro   0     - 15_000   -> destroyed (entirely before the cut)
#   middle  15_000 - 25_000  -> straddles the in-point (20_000)
#   body    25_000 - 45_000  -> kept, needs rebasing by -20_000
CUT_START_MS = 20_000
CUT_END_MS = 50_000


class ClassifyChaptersTest(unittest.TestCase):
    def test_three_chapter_fixture(self):
        chapters = [
            ('intro', 0, 15_000, 'Intro'),
            ('middle', 15_000, 25_000, 'Middle'),
            ('body', 25_000, 45_000, 'Body'),
        ]
        result = trim.classify_chapters(chapters, CUT_START_MS, CUT_END_MS)
        by_id = {c[0]: c[4] for c in result}
        self.assertEqual(by_id['intro'], 'destroyed')
        self.assertEqual(by_id['middle'], 'straddles')
        self.assertEqual(by_id['body'], 'kept')

    def test_chapter_entirely_after_cut_is_destroyed(self):
        result = trim.classify_chapter(55_000, 60_000, CUT_START_MS, CUT_END_MS)
        self.assertEqual(result, 'destroyed')

    def test_chapter_straddling_out_point(self):
        result = trim.classify_chapter(45_000, 55_000, CUT_START_MS, CUT_END_MS)
        self.assertEqual(result, 'straddles')

    def test_exact_boundary_match_is_kept(self):
        result = trim.classify_chapter(CUT_START_MS, CUT_END_MS, CUT_START_MS, CUT_END_MS)
        self.assertEqual(result, 'kept')


class RebaseAndClampTest(unittest.TestCase):
    def test_rebase_shifts_by_cut_start(self):
        chapter = ('body', 25_000, 45_000, 'Body')
        self.assertEqual(trim.rebase_chapter(chapter, CUT_START_MS), ('body', 5_000, 25_000, 'Body'))

    def test_clamp_straddling_in_point(self):
        chapter = ('middle', 15_000, 25_000, 'Middle')
        # Clamped to the cut boundary (20_000), then rebased -> starts at 0.
        self.assertEqual(trim.clamp_chapter(chapter, CUT_START_MS, CUT_END_MS), ('middle', 0, 5_000, 'Middle'))

    def test_clamp_straddling_out_point(self):
        chapter = ('outro', 45_000, 55_000, 'Outro')
        clamped = trim.clamp_chapter(chapter, CUT_START_MS, CUT_END_MS)
        # end clamped to CUT_END_MS (50_000), then rebased by -CUT_START_MS.
        self.assertEqual(clamped, ('outro', 25_000, 30_000, 'Outro'))


class RebuildCtocChildrenTest(unittest.TestCase):
    def test_filters_and_preserves_order(self):
        order = ['intro', 'middle', 'body', 'outro']
        survivors = {'middle', 'outro'}
        self.assertEqual(trim.rebuild_ctoc_children(order, survivors), ['middle', 'outro'])

    def test_empty_when_nothing_survives(self):
        self.assertEqual(trim.rebuild_ctoc_children(['a', 'b'], set()), [])


class ChapterReadWriteRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "t.mp3")
        open(self.path, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fixture(self):
        audio = ID3()
        audio.add(CHAP(element_id='intro', start_time=0, end_time=15_000,
                       start_offset=0xffffffff, end_offset=0xffffffff,
                       sub_frames=[TIT2(encoding=3, text=['Intro'])]))
        audio.add(CHAP(element_id='middle', start_time=15_000, end_time=25_000,
                       start_offset=0xffffffff, end_offset=0xffffffff,
                       sub_frames=[TIT2(encoding=3, text=['Middle'])]))
        audio.add(CHAP(element_id='body', start_time=25_000, end_time=45_000,
                       start_offset=0xffffffff, end_offset=0xffffffff,
                       sub_frames=[TIT2(encoding=3, text=['Body'])]))
        audio.add(CTOC(element_id='toc', flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                       child_element_ids=['intro', 'middle', 'body'], sub_frames=[]))
        audio.save(self.path, v2_version=3)

    def test_read_chapters_round_trips(self):
        self._write_fixture()
        chapters, child_order, flags = trim.read_chapters(self.path)
        self.assertEqual(set(c[0] for c in chapters), {'intro', 'middle', 'body'})
        by_id = {c[0]: c for c in chapters}
        self.assertEqual(by_id['middle'], ('middle', 15_000, 25_000, 'Middle'))
        self.assertEqual(child_order, ['intro', 'middle', 'body'])
        self.assertTrue(flags & CTOCFlags.TOP_LEVEL)
        self.assertTrue(flags & CTOCFlags.ORDERED)

    def test_no_chapters_returns_empty(self):
        audio = ID3()
        audio.save(self.path, v2_version=3)
        chapters, child_order, flags = trim.read_chapters(self.path)
        self.assertEqual(chapters, [])
        self.assertIsNone(child_order)
        self.assertIsNone(flags)

    def test_write_chapters_replaces_and_rebuilds_ctoc(self):
        self._write_fixture()
        # Simulate the resolved decision for the three-chapter cut: intro
        # (destroyed) dropped, middle (straddled) clamped, body (kept) rebased.
        survivors = [
            trim.clamp_chapter(('middle', 15_000, 25_000, 'Middle'), CUT_START_MS, CUT_END_MS),
            trim.rebase_chapter(('body', 25_000, 45_000, 'Body'), CUT_START_MS),
        ]
        new_order = trim.rebuild_ctoc_children(['intro', 'middle', 'body'], {'middle', 'body'})
        trim.write_chapters(self.path, survivors, new_order, CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED)

        chapters, child_order, flags = trim.read_chapters(self.path)
        self.assertEqual(set(c[0] for c in chapters), {'middle', 'body'})
        by_id = {c[0]: c for c in chapters}
        self.assertEqual(by_id['middle'], ('middle', 0, 5_000, 'Middle'))
        self.assertEqual(by_id['body'], ('body', 5_000, 25_000, 'Body'))
        self.assertEqual(child_order, ['middle', 'body'])

    def test_write_chapters_empty_removes_everything(self):
        self._write_fixture()
        trim.write_chapters(self.path, [], None, None)
        chapters, child_order, flags = trim.read_chapters(self.path)
        self.assertEqual(chapters, [])
        self.assertIsNone(child_order)

    def test_write_chapters_survivors_with_no_child_order_drops_ctoc(self):
        self._write_fixture()
        survivors = [trim.rebase_chapter(('body', 25_000, 45_000, 'Body'), CUT_START_MS)]
        trim.write_chapters(self.path, survivors, [], None)
        chapters, child_order, flags = trim.read_chapters(self.path)
        self.assertEqual(len(chapters), 1)
        self.assertIsNone(child_order)   # no CTOC written — nothing to reference


@unittest.skipUnless(trim.HAS_FFMPEG, "ffmpeg not installed")
class CommitTrimWithChaptersTest(unittest.TestCase):
    """End-to-end: commit_trim's `chapters` kwarg actually lands on the
    written output, not just the pure transforms in isolation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.backup_dir = os.path.join(self.tmp, "backups")
        self._real_load_config = trim.load_config
        trim.load_config = lambda: {"trim_backup_dir": self.backup_dir, "trim_ffmpeg_path": ""}

        self.path = os.path.join(self.tmp, "t.mp3")
        subprocess.run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=300:duration=60",
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k", self.path,
        ], check=True, capture_output=True)

        audio = ID3(self.path)
        audio.add(CHAP(element_id='intro', start_time=0, end_time=15_000,
                       start_offset=0xffffffff, end_offset=0xffffffff,
                       sub_frames=[TIT2(encoding=3, text=['Intro'])]))
        audio.add(CHAP(element_id='middle', start_time=15_000, end_time=25_000,
                       start_offset=0xffffffff, end_offset=0xffffffff,
                       sub_frames=[TIT2(encoding=3, text=['Middle'])]))
        audio.add(CHAP(element_id='body', start_time=25_000, end_time=45_000,
                       start_offset=0xffffffff, end_offset=0xffffffff,
                       sub_frames=[TIT2(encoding=3, text=['Body'])]))
        audio.add(CTOC(element_id='toc', flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                       child_element_ids=['intro', 'middle', 'body'], sub_frames=[]))
        audio.save(self.path, v2_version=3)

    def tearDown(self):
        trim.load_config = self._real_load_config
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_resolved_chapters_land_on_the_output(self):
        classified = trim.classify_chapters(
            trim.read_chapters(self.path)[0], CUT_START_MS, CUT_END_MS)
        survivors = []
        for element_id, start_ms, end_ms, title, cls in classified:
            c = (element_id, start_ms, end_ms, title)
            if cls == 'destroyed':
                continue
            if cls == 'straddles':
                survivors.append(trim.clamp_chapter(c, CUT_START_MS, CUT_END_MS))
            else:
                survivors.append(trim.rebase_chapter(c, CUT_START_MS))
        new_order = trim.rebuild_ctoc_children(['intro', 'middle', 'body'],
                                               {c[0] for c in survivors})
        chapters = (survivors, new_order, CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED)

        result = trim.commit_trim(self.path, CUT_START_MS / 1000.0, CUT_END_MS / 1000.0,
                                  chapters=chapters)
        self.assertTrue(result.ok, result.error)

        out_chapters, out_order, _flags = trim.read_chapters(self.path)
        by_id = {c[0]: c for c in out_chapters}
        self.assertNotIn('intro', by_id)          # destroyed
        self.assertEqual(by_id['middle'][1:3], (0, 5_000))    # clamped + rebased
        self.assertEqual(by_id['body'][1:3], (5_000, 25_000))  # rebased
        self.assertEqual(out_order, ['middle', 'body'])


if __name__ == "__main__":
    unittest.main()
