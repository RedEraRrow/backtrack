"""The empty player a pinned window shows while the other window browses."""
import contextlib
import io
import unittest
from unittest import mock

from backtrack.playback import player


class IdlePlayerTest(unittest.TestCase):
    def _run(self, woken, others):
        with mock.patch.object(player, 'raw_mode', lambda _f: contextlib.nullcontext()), \
             mock.patch.object(player, 'get_key_non_blocking', lambda: None), \
             mock.patch.object(player, 'has_other_windows', others), \
             mock.patch.object(player.ui, 'get_terminal_size', lambda *_: (100, 40)), \
             mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            return player._idle_view(woken, 50), out.getvalue()

    def test_draws_note_then_wakes_when_something_plays(self):
        ticks = iter([False, False, True])
        woke, drawn = self._run(lambda: next(ticks), lambda: True)
        self.assertTrue(woke)
        self.assertIn('Not playing', drawn)
        self.assertIn('⣿', drawn)

    def test_returns_to_browse_when_the_other_window_closes(self):
        woke, _ = self._run(lambda: False, lambda: False)
        self.assertFalse(woke)


if __name__ == "__main__":
    unittest.main()
