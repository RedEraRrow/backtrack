"""Audiobooks: chapters read from tags, which directories hold books, where each
book got to, chapter-wise prev/next, and the sleep timer."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mutagen.id3 import ID3, CHAP, CTOC, CTOCFlags, TIT2  # type: ignore[reportPrivateImportUsage]

from backtrack import books
from backtrack.music_library import chapter_at, chapter_label, chapters_of, is_audiobook
from backtrack.playback import player_ui
from backtrack.playback import session as sess


def _chap(eid, start_ms, title=None):
    subs = [TIT2(encoding=3, text=[title])] if title else []
    return CHAP(element_id=eid, start_time=start_ms, end_time=start_ms + 1000,
                start_offset=0xffffffff, end_offset=0xffffffff, sub_frames=subs)


class ChaptersOfTest(unittest.TestCase):
    def test_id3_follows_the_ctoc_order(self):
        tags = ID3()
        tags.add(_chap('x', 10_000, 'X'))
        tags.add(_chap('y', 0))
        tags.add(CTOC(element_id='toc', flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
                      child_element_ids=['x', 'y'], sub_frames=[]))
        self.assertEqual(chapters_of(tags), [[10.0, 'X'], [0.0, '']])

    def test_id3_without_a_ctoc_goes_by_start(self):
        tags = ID3()
        tags.add(_chap('x', 10_000, 'X'))
        tags.add(_chap('y', 0, 'Y'))
        self.assertEqual(chapters_of(tags), [[0.0, 'Y'], [10.0, 'X']])

    def test_mp4_chapters(self):
        tags = SimpleNamespace(chapters=[SimpleNamespace(start=0.0, title='Prologue'),
                                         SimpleNamespace(start=240.7031, title='')])
        self.assertEqual(chapters_of(tags), [[0.0, 'Prologue'], [240.703, '']])

    def test_mp4_chapters_from_a_file_with_a_sample_rate_timescale(self):
        """mutagen divides Nero chapter times by the movie timescale; ffmpeg 9
        writes 44100 there, not 1000, so the times need scaling back."""
        class Chapters(list):
            _timescale = 44100
        tags = SimpleNamespace(chapters=Chapters([SimpleNamespace(start=0.0, title='A'),
                                                  SimpleNamespace(start=3250 / 44100, title='B')]))
        self.assertEqual(chapters_of(tags), [[0.0, 'A'], [3.25, 'B']])

    def test_none(self):
        self.assertEqual(chapters_of(ID3()), [])
        self.assertEqual(chapters_of(SimpleNamespace()), [])

    def test_labels(self):
        named = [[0, 'Prologue'], [5, 'The Boy Who Lived'], [9, 'The Vanishing Glass'], [12, 'Epilogue']]
        self.assertEqual([chapter_label(named, i) for i in range(4)],
                         [('Prologue', ''), ('The Boy Who Lived', 'Chapter 1 of 2'),
                          ('The Vanishing Glass', 'Chapter 2 of 2'), ('Epilogue', '')])
        numbered = [[0, 'Prologue'], [240, 'Chapter 1'], [500, 'Chapter Two']]
        self.assertEqual([chapter_label(numbered, i) for i in range(3)],
                         [('Prologue', ''), ('Chapter 1', ''), ('Chapter Two', '')])
        self.assertEqual(chapter_label([[0, ''], [10, '']], 1), ('Chapter 2', ''))

    def test_chapter_at(self):
        chapters = [[0.0, 'a'], [60.0, 'b'], [120.0, 'c']]
        self.assertEqual([chapter_at(chapters, t) for t in (0, 59.9, 60, 500)], [0, 0, 1, 2])
        self.assertEqual(chapter_at([], 10), -1)


class IsAudiobookTest(unittest.TestCase):
    def test_innermost_directory_decides(self):
        cfg = {'music_directories': ['/m', '/m/books'], 'library_media_types': {'/m/books': 'audiobooks'}}
        self.assertTrue(is_audiobook('/m/books/A/B/b.m4b', cfg))
        self.assertFalse(is_audiobook('/m/x/song.mp3', cfg))
        cfg = {'music_directories': ['/b', '/b/music'], 'library_media_types': {'/b': 'audiobooks'}}
        self.assertTrue(is_audiobook('/b/A/b.m4b', cfg))
        self.assertFalse(is_audiobook('/b/music/song.mp3', cfg))

    def test_nothing_set_is_music(self):
        self.assertFalse(is_audiobook('/m/b.m4b', {'music_directories': ['/m']}))


class BookStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.tracks = []
        for n in (1, 2):
            p = os.path.join(self.tmp, f"{n}.mp3")
            open(p, "wb").close()
            self.tracks.append(p)
        entry = {'album': 'Book', 'album_artist': 'Author'}
        self.patches = [
            mock.patch.object(books, '_file', lambda: Path(self.tmp) / "books.json"),
            mock.patch.object(books, 'library_entry', lambda p: entry),
            mock.patch.object(books, 'album_tracks', lambda p, lib: list(self.tracks)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip_shared_by_the_whole_book(self):
        self.assertIsNone(books.resume_point(self.tracks[0]))
        books.remember(self.tracks[0], 30.04, 1.5)
        self.assertEqual(books.resume_point(self.tracks[1]), (self.tracks[0], 30.0))
        self.assertEqual(books.rate_for(self.tracks[1]), 1.5)

    def test_end_of_a_file_moves_on_then_finishes(self):
        books.remember(self.tracks[0], 99, 1.0, at_end=True)
        self.assertEqual(books.resume_point(self.tracks[0]), (self.tracks[1], 0.0))
        books.remember(self.tracks[1], 99, 1.0, at_end=True)
        self.assertIsNone(books.resume_point(self.tracks[0]))

    def test_start_over_keeps_the_speed(self):
        books.remember(self.tracks[1], 50, 1.3)
        books.forget(self.tracks[0])
        self.assertIsNone(books.resume_point(self.tracks[0]))
        self.assertEqual(books.rate_for(self.tracks[0]), 1.3)

    def test_gone_file_starts_over(self):
        books.remember(self.tracks[1], 50, 1.0)
        os.remove(self.tracks[1])
        self.assertIsNone(books.resume_point(self.tracks[0]))


class _MP:
    """A VLC player stand-in: a clock that seeks, a volume and a pause switch."""
    def __init__(self, at=0.0):
        self.ms, self.volumes, self.paused = int(at * 1000), [], False

    def get_time(self):
        return self.ms

    def set_time(self, ms):
        self.ms = ms

    def get_state(self):
        return 'playing'

    def audio_set_volume(self, v):
        self.volumes.append(v)

    def set_pause(self, on):
        self.paused = bool(on)


class _Session(sess.PlaybackSession):
    def _load(self, path, start_at=0.0):
        self.file_path = path
        self.loaded = path
        return True

    def _title_for(self, path):
        return path


class ChapterStepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.paths = []
        for n in (1, 2):
            p = os.path.join(self.tmp, f"{n}.m4b")
            open(p, "wb").close()
            self.paths.append(p)
        self.s = _Session()
        self.s.queue, self.s.titles, self.s.index = list(self.paths), list(self.paths), 0
        self.s.file_path = self.paths[0]
        self.s.chapters = [[0.0, 'a'], [60.0, 'b'], [120.0, 'c']]
        self.s.loaded = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _at(self, t):
        self.s.mp = _MP(t)
        return self.s.mp

    def test_next_goes_by_chapter_then_track(self):
        mp = self._at(70)
        self.s.next()
        self.assertEqual(mp.ms, 120_000)
        self._at(130)
        self.s.next()
        self.assertEqual(self.s.loaded, self.paths[1])

    def test_prev_restarts_the_chapter_then_steps_back(self):
        mp = self._at(70)
        self.s.prev()
        self.assertEqual(mp.ms, 60_000)
        mp = self._at(62)
        self.s.prev()
        self.assertEqual(mp.ms, 0)
        self.s.index = 1
        self._at(2)
        self.s.prev()
        self.assertEqual(self.s.loaded, self.paths[0])

    def test_auto_advance_ignores_chapters(self):
        self._at(70)
        self.s.next(manual=False)
        self.assertEqual(self.s.loaded, self.paths[1])


class SleepTimerTest(unittest.TestCase):
    def setUp(self):
        self.s = _Session()
        self.s._volume = 80
        self.s.file_path = '/x.m4b'

    def test_cycles_through_every_setting(self):
        seen = [self.s.cycle_sleep() for _ in range(7)]
        self.assertEqual(seen, [15, 30, 45, 60, 'chapter', None, 15])

    def test_fades_then_pauses(self):
        self.s.mp = _MP()
        self.s.sleep_mode, self.s._sleep_deadline = 15, time.monotonic() + 2.5
        self.assertFalse(self.s._sleep_tick())
        self.assertTrue(0 < self.s.mp.volumes[-1] < 80)
        self.s._sleep_deadline = time.monotonic() + 0.1
        self.assertTrue(self.s._sleep_tick())
        self.assertTrue(self.s.mp.paused)
        self.assertEqual(self.s.mp.volumes[-1], 80)
        self.assertIsNone(self.s.sleep_mode)

    def test_end_of_chapter_counts_real_time(self):
        self.s.mp = _MP(99.0)
        self.s.chapters, self.s.rate, self.s.sleep_mode = [[0.0, 'a'], [100.0, 'b']], 2.0, 'chapter'
        self.assertAlmostEqual(self.s.sleep_left(), 0.5)
        self.s.mp.ms = 99_500
        self.assertTrue(self.s._sleep_tick())


if __name__ == "__main__":
    unittest.main()
