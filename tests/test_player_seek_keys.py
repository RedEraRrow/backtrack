"""The seek keys the host player and a joined window share."""
import unittest

from backtrack.playback.player import _seek_step


class SeekKeysTest(unittest.TestCase):
    def test_each_key_seeks_its_amount(self):
        self.assertEqual(_seek_step('RIGHT', 100, 10), (5, 'Seek Forward +5s'))
        self.assertEqual(_seek_step('LEFT', 100, 10), (-5, 'Seek Backward -5s'))
        self.assertEqual([_seek_step(k, 100, 10)[0] for k in ',.jl'], [-30, 30, -1, 1])
        from backtrack import tuning as tune
        from backtrack.playback import player_ui
        self.assertIsNone(_seek_step('e', 100, 10))          # Diagnostics off
        player_ui._ui_state['debug'] = True
        try:
            self.assertEqual(_seek_step('e', 100, 10)[0], 100 - tune.NEAR_END_JUMP_S - 10)
        finally:
            player_ui._ui_state['debug'] = False
        self.assertIsNone(_seek_step('p', 100, 10))
        self.assertIsNone(_seek_step('', 100, 10))


if __name__ == "__main__":
    unittest.main()
