"""The browse lists' shared result handling: each result does its one thing
and says it was handled; anything else is left to the list."""
import unittest
from unittest.mock import patch

from backtrack.menus import play


class ListResultTest(unittest.TestCase):
    def test_each_result_is_handled_once(self):
        sorts, paths = [], lambda: ['a.mp3', 'b.mp3']
        with patch.object(play, '_play_list') as pl, patch.object(play, 'bulk_id3_manager') as bm:
            self.assertTrue(play._list_result(("__edited__", 'x'), [], paths, lambda: sorts.append(1)))
            self.assertTrue(play._list_result("__sort__", [], paths, lambda: sorts.append(1)))
            self.assertTrue(play._list_result("__shuffle__", [], paths, lambda: None))
            self.assertTrue(play._list_result("__bulk_edit__", [], paths, lambda: None))
            self.assertFalse(play._list_result("Some Artist", [], paths, lambda: None))
        self.assertEqual(sorts, [1])
        pl.assert_called_once_with("__shuffle__", ['a.mp3', 'b.mp3'], [])
        bm.assert_called_once_with([], paths=['a.mp3', 'b.mp3'])


if __name__ == "__main__":
    unittest.main()
