"""Every queue edit goes through PlaybackSession.edit: adding next, at the end
or after the current album (shuffled or not, kept together), playing a picked
track per the after-pick setting, jumping, moving, removing, clearing and
shuffling what's coming, and undo, which keeps the playing track. An edit
aimed at a position that no longer holds its track is ignored."""
import unittest
from unittest.mock import patch

from backtrack.playback import session as sess


ALBUMS = {f"/m/{a}{n}.mp3": a for a in "AB" for n in range(1, 4)}


class _Session(sess.PlaybackSession):
    """No audio: loading a track just records it."""
    def _load(self, path, start_at=0.0):
        self.file_path = path
        self.mp = object()
        return True

    def _title_for(self, path):
        return path.rsplit("/", 1)[-1]

    def _ensure_advertised(self):
        pass

    def elapsed(self):
        return 0.0

    def stop(self):
        self.file_path = None


def _entry(path, by_path=None):
    return {'album': ALBUMS.get(path, '?'), 'album_artist': ''}


class QueueEditTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.file = Path(self.dir.name) / "queue.json"
        self._p = patch.object(sess, "library_entry", _entry)
        self._p.start()
        self._q = patch.object(sess, "_queue_file", lambda: self.file)
        self._q.start()
        self.s = _Session()
        self.s.start("/m/A1.mp3", queue=["/m/A1.mp3", "/m/A2.mp3", "/m/A3.mp3", "/m/B1.mp3"])

    def tearDown(self):
        self._p.stop()
        self._q.stop()
        self.dir.cleanup()

    def names(self):
        return [p.rsplit("/", 1)[-1][:-4] for p in self.s.queue]

    def test_add_next_end_and_after_the_album(self):
        self.s.edit('add', paths=["/m/x.mp3"], where='next')
        self.s.edit('add', paths=["/m/y.mp3"], where='end')
        self.assertEqual(self.names(), ["A1", "x", "A2", "A3", "B1", "y"])
        s = _Session()
        s.start("/m/A1.mp3", queue=["/m/A1.mp3", "/m/A2.mp3", "/m/A3.mp3", "/m/B1.mp3"])
        s.edit('add', paths=["/m/z.mp3"], where='after_album')
        self.assertEqual(s.queue[3], "/m/z.mp3")                       # after A3, before B1
        self.assertEqual(len(s.titles), len(s.queue))

    def test_add_shuffled_keeps_the_run_together(self):
        new = [f"/m/n{i}.mp3" for i in range(8)]
        self.s.edit('add', paths=new, where='end', shuffled=True)
        self.assertEqual(sorted(self.s.queue[4:]), sorted(new))
        self.assertEqual(self.s.queue[:4], ["/m/A1.mp3", "/m/A2.mp3", "/m/A3.mp3", "/m/B1.mp3"])

    def test_play_picked_per_setting(self):
        lst = ["/m/B1.mp3", "/m/B2.mp3", "/m/B3.mp3"]
        self.s.edit('play_picked', paths=lst, index=1, then='stop')
        self.assertEqual((self.s.queue, self.s.file_path), (["/m/B2.mp3"], "/m/B2.mp3"))
        self.s.edit('play_picked', paths=lst, index=1, then='list')
        self.assertEqual((self.s.queue, self.s.index), (lst, 1))
        self.s.start("/m/A1.mp3", queue=["/m/A1.mp3", "/m/A2.mp3"])
        self.s.edit('play_picked', paths=lst, index=2, then='queue')
        self.assertEqual(self.names(), ["A1", "B3", "A2"])               # then back to the queue
        self.assertEqual((self.s.index, self.s.file_path), (1, "/m/B3.mp3"))

    def test_move_remove_clear_shuffle(self):
        q = list(self.s.queue)
        self.assertTrue(self.s.edit('move', pos=2, delta=1, path=q[2]))
        self.assertEqual(self.names(), ["A1", "A2", "B1", "A3"])
        self.assertTrue(self.s.edit('remove', pos=1, path="/m/A2.mp3"))
        self.assertEqual(self.names(), ["A1", "B1", "A3"])
        self.assertTrue(self.s.edit('remove', pos=0, path="/m/A1.mp3"))  # the one playing
        self.assertEqual(self.s.file_path, "/m/B1.mp3")
        self.assertTrue(self.s.edit('clear_upcoming'))
        self.assertEqual(self.names(), ["B1"])
        self.assertFalse(self.s.edit('shuffle_upcoming'))                # nothing to shuffle

    def test_stale_positions_are_ignored(self):
        self.assertFalse(self.s.edit('remove', pos=1, path="/m/B1.mp3"))
        self.assertFalse(self.s.edit('jump', pos=9, path="/m/A1.mp3"))
        self.assertEqual(len(self.s.queue), 4)

    def test_undo_keeps_the_playing_track(self):
        self.s.edit('jump', pos=2, path="/m/A3.mp3")
        self.s.edit('shuffle_upcoming')
        self.s.edit('add', paths=["/m/x.mp3"], where='next')
        self.s.edit('clear_upcoming')
        self.assertTrue(self.s.edit('undo'))
        self.assertEqual(self.names(), ["A1", "A2", "A3", "x", "B1"])
        self.assertEqual(self.s.queue[self.s.index], "/m/A3.mp3")
        self.s.edit('undo')
        self.assertNotIn("/m/x.mp3", self.s.queue)
        self.assertEqual(self.s.queue[self.s.index], "/m/A3.mp3")
        self.s.edit('undo')                                               # the shuffle (one track: no-op)
        self.assertFalse(self.s.edit('undo'))

    def test_enqueue_and_play_next_go_through_edit(self):
        self.assertEqual(self.s.enqueue("/m/x.mp3"), 5)
        self.s.play_next("/m/y.mp3")
        self.assertEqual(self.names(), ["A1", "y", "A2", "A3", "B1", "x"])
        self.assertTrue(self.s.edit('undo'))
        self.assertNotIn("/m/y.mp3", self.s.queue)

    def test_unknown_or_bad_edits_change_nothing(self):
        self.assertFalse(self.s.edit('nonsense'))
        self.assertFalse(self.s.edit('add', paths=["/m/x.mp3"], where='sideways'))
        self.assertEqual(len(self.s.queue), 4)


if __name__ == "__main__":
    unittest.main()


class QueueActionsTest(unittest.TestCase):
    """A library list's queue actions: track options only on track rows,
    group options on group rows, whole-list actions apart, and none of the
    queue's own (shuffle, clear, undo: those are the player's)."""
    def setUp(self):
        from backtrack.menus import play
        self.play = play
        self.calls = []
        test = self

        class _Fake:
            def is_active(self):
                return test.playing

            def edit(self, op, **args):
                test.calls.append((op, args.get('where'), len(args.get('paths') or [])))
                return True

            def start(self, path, queue=None, titles=None, **_):
                test.calls.append(('start', path, len(queue or [])))
                return True
        self.playing = True
        self._p = [patch.object(play, "active_session", lambda: _Fake()),
                   patch.object(play, "is_client", lambda: False),
                   patch.object(play, "drop_moved", lambda paths: list(paths)),
                   patch.object(play.ui, "show_status", lambda *a, **k: None)]
        for p in self._p:
            p.start()
        self.library = [{'path': f"/m/{i}.mp3", 'title': f"t{i}", 'album': 'A', 'album_artist': ''}
                        for i in range(3)]
        self.kw = play._queue_shortcut_kwargs(self.library, group_paths=None,
                                              disc_track_map={'1': ["/m/0.mp3", "/m/1.mp3"]},
                                              list_paths=[t['path'] for t in self.library])

    def tearDown(self):
        for p in self._p:
            p.stop()

    def applies(self, name, row):
        return self.kw['row_action_applies'](f"queue_actions.{name}", row)

    def test_track_options_only_on_tracks(self):
        for name in ("play_from_here", "add_album", "play_next", "add"):
            self.assertTrue(self.applies(name, "/m/1.mp3"), name)
        self.assertFalse(self.applies("play_from_here", "__disc_1"))   # a group row
        self.assertFalse(self.applies("add_album", "__disc_1"))
        self.assertTrue(self.applies("add_shuffled", "__disc_1"))
        self.playing = False
        self.assertFalse(self.applies("after_album", "/m/1.mp3"))       # nothing playing

    def test_no_queue_level_actions_in_a_list(self):
        names = {k.split('.', 1)[1] for k in self.kw['row_actions']} | {
            k.split('.', 1)[1] for k in self.kw['list_actions']}
        self.assertFalse(names & {"shuffle", "clear", "undo"})

    def test_row_and_whole_list_actions_ask_for_their_edit(self):
        row = self.kw['row_actions']
        row["queue_actions.play_next"]("__disc_1")
        row["queue_actions.add_shuffled"]("/m/2.mp3")
        self.kw['list_actions']["queue_actions.all_after_album"]()
        self.assertEqual(self.calls, [('add', 'next', 2), ('add', 'end', 1), ('add', 'after_album', 3)])

    def test_with_nothing_playing_adding_starts_playback(self):
        self.playing = False
        self.kw['row_actions']["queue_actions.add"]("__disc_1")
        self.assertEqual(self.calls, [('start', "/m/0.mp3", 2)])


class PlayerQueueKeysTest(unittest.TestCase):
    """The queue panel's keys: a cursor through the queue, and edits at it sent
    to the session (this window's or the one joined)."""
    def setUp(self):
        from backtrack.playback import player, player_ui, queue_pane as qp
        self.player, self.ui, self.qp = player, player_ui, qp
        self.queue = [f"/m/{i}.mp3" for i in range(5)]
        qp.set_queue_context([f"t{i}" for i in range(5)], 1, self.queue)
        qp.set_queue_cursor(None)
        self.edits = []
        self.session = type("S", (), {"edit": lambda _s, op, **a: self.edits.append((op, a)) or True})()

    def press(self, key):
        return self.player._queue_key(key, self.session, self.queue, 1)

    def test_a_title_too_long_for_its_border_is_just_the_name(self):
        self.assertEqual(self.qp.queue_title(), "Queue · 2 of 5")
        self.assertEqual(self.qp.queue_title(14), "Queue · 2 of 5")              # fits whole
        self.assertEqual(self.qp.queue_title(13), "Queue")                       # not cut short: the name

    def test_the_wheel_over_the_queue_moves_its_cursor(self):
        from unittest import mock
        from backbone.prompt import core as pc
        self.qp._queue_area[0] = (10, 40, 20, 79)                        # where the list drew
        self.addCleanup(lambda: self.qp._queue_area.__setitem__(0, None))
        with mock.patch.object(pc, '_wheel_at', [(15, 60)]):
            self.assertTrue(self.player._queue_wheel('SCROLL_DOWN'))
            self.assertTrue(self.player._queue_wheel('SCROLL_DOWN'))
            self.assertEqual(self.qp.queue_cursor(), 3)                   # from the playing track, two down
            self.assertTrue(self.player._queue_wheel('SCROLL_UP'))
            self.assertEqual(self.qp.queue_cursor(), 2)
        with mock.patch.object(pc, '_wheel_at', [(5, 60)]):
            self.assertFalse(self.player._queue_wheel('SCROLL_DOWN'))      # not over the queue: not its
        self.assertFalse(self.player._queue_wheel('DOWN'))

    def test_cursor_then_edits_at_it(self):
        self.assertTrue(self.press('DOWN'))
        self.assertEqual(self.qp.queue_cursor(), 2)
        self.press('K')                                   # move it down
        self.assertEqual(self.edits[-1], ('move', {'pos': 2, 'delta': 1, 'path': "/m/2.mp3"}))
        self.assertEqual(self.qp.queue_cursor(), 3)
        self.press('d')
        self.assertEqual(self.edits[-1], ('remove', {'pos': 3, 'path': "/m/3.mp3"}))
        self.press('ENTER')
        self.assertEqual(self.edits[-1][0], 'jump')
        self.assertIsNone(self.qp.queue_cursor())          # back to following the current track
        for k, op in (('x', 'shuffle_upcoming'), ('c', 'clear_upcoming'), ('u', 'undo')):
            self.press(k)
            self.assertEqual(self.edits[-1], (op, {}))
        self.assertFalse(self.press('m'))                  # not a queue key: the player's own

    def test_the_cursor_row_is_marked(self):
        self.qp.set_queue_cursor(3)
        from backbone import ui
        raw = self.qp._build_queue_lines(60, 8)
        # (titles come from the files' names here: they don't exist)
        rows = raw[1:]                                          # positions 1-5, the window from the top
        self.assertIn(ui.Colors.BAR, rows[3])                   # the cursor: the highlight bar
        self.assertIn(ui.Colors.BAR_DIM, rows[1])               # the playing track: the soft one
        self.assertEqual(ui.strip_ansi(rows[1]).split()[0], "1")     # its title ("1.mp3"), no position before it


class SavedQueueTest(unittest.TestCase):
    """The queue is kept between runs: saved on changes, read back for Resume
    without tracks that have moved, forgotten once it plays to the end, and a
    broken file is dropped rather than trusted."""
    def setUp(self):
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        self.file = d / "queue.json"
        self.tracks = []
        for i in range(3):
            (d / f"{i}.mp3").write_bytes(b"")
            self.tracks.append(str(d / f"{i}.mp3"))
        self._p = [patch.object(sess, "_queue_file", lambda: self.file),
                   patch.object(sess, "library_entry", _entry),
                   patch.object(sess, "is_client", lambda: False)]
        for p in self._p:
            p.start()
        self.s = _Session()
        self.s.elapsed = lambda: 42.0

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.dir.cleanup()

    def test_saved_and_resumed_without_moved_tracks(self):
        self.s.start(self.tracks[1], queue=self.tracks + ["/gone/x.mp3"], index=1)
        self.s._save_queue()
        saved = sess.saved_queue()
        self.assertEqual(saved['queue'], self.tracks)             # the moved one left out
        self.assertEqual((saved['index'], saved['elapsed']), (1, 42.0))

    def test_forgotten_when_the_queue_plays_to_the_end(self):
        self.s.start(self.tracks[2], queue=self.tracks, index=2)
        self.s._save_queue()
        self.assertTrue(self.file.exists())
        self.assertIsNone(self.s.next(manual=False))
        self.assertFalse(self.file.exists())

    def test_a_broken_file_is_dropped(self):
        self.file.write_text("{not json")
        self.assertIsNone(sess.saved_queue())
        self.assertFalse(self.file.exists())
