"""A rebound key works and is shown: with the player's next-track key moved off
`]`, the hint names the new key, a click on it replays that key, the lyrics
editor's and trim editor's `]` are untouched, and the seek table follows its
own rebinding."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backbone import keys, ui
from backbone.prompt import core as pc
from backtrack.playback import player_ui
from backtrack.playback.player import _seek_step
import backtrack.lyrics.editor_view  # noqa: F401  (defines the lyrics keys)
import backtrack.trim.editor  # noqa: F401  (defines the trim keys)


class ReboundKeysTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.file = Path(self.dir.name) / "keys.json"
        self.file.write_text(json.dumps({"player.next": ["n"], "player.fwd_5": ["f"]}))
        self._p = patch.object(keys, "_path", lambda: self.file)
        self._p.start()
        keys._saved.update(map=None, mtime=None, checked=0.0)

    def tearDown(self):
        self._p.stop()
        keys._saved.update(map=None, mtime=None, checked=0.0)
        self.dir.cleanup()

    def test_the_player_follows_and_shows_the_new_key(self):
        self.assertEqual(keys.action("n", "player"), "player.next")
        self.assertIsNone(keys.action("]", "player"))
        self.assertEqual(keys.action("]", "lyrics_lines"), "lyrics.fwd_1")
        self.assertEqual(keys.action("]", "trim"), "trim.next_marker")
        with patch.object(pc, "hints_visible", lambda: True), \
             patch.object(ui, "get_terminal_size", lambda *a: (120, 40)):
            _, hints = player_ui._controls_line(False, False, 100, "", width=120)
        plain = ui.strip_ansi(hints)
        self.assertIn("[[/n] prev/next", plain)
        player_ui.compute_controls_hint_cells([ui.strip_ansi(h) for h in hints.splitlines()], 1)
        self.assertIn("n", set(player_ui._last_hint_cells.values()))

    def test_seeks_follow_their_rebinding(self):
        self.assertEqual(_seek_step("f", 100, 10)[0], 5)
        self.assertIsNone(_seek_step("RIGHT", 100, 10))


if __name__ == "__main__":
    unittest.main()
