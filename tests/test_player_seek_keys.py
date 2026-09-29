"""The seek keys the host player and a joined window share."""
import unittest

from src.playback.playback import _seek_step


class SeekKeysTest(unittest.TestCase):
    def test_each_key_seeks_its_amount(self):
        self.assertEqual(_seek_step('', 'C', 100, 10), (5, 'Seek Forward +5s'))
        self.assertEqual(_seek_step('', 'D', 100, 10), (-5, 'Seek Backward -5s'))
        self.assertEqual([_seek_step(k, None, 100, 10)[0] for k in ',.jJlL'], [-30, 30, -1, -1, 1, 1])
        from src import tuning as tune
        self.assertEqual(_seek_step('e', None, 100, 10)[0], 100 - tune.NEAR_END_JUMP_S - 10)
        self.assertIsNone(_seek_step('p', None, 100, 10))
        self.assertIsNone(_seek_step('', None, 100, 10))


if __name__ == "__main__":
    unittest.main()
