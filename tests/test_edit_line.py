"""The one line editor every text field uses."""
import unittest

from src.utils.prompt_core import edit_line


class EditLineTest(unittest.TestCase):
    def test_keys(self):
        buf = list("ac")
        pos = edit_line(buf, 1, 'b')                 # typing inserts at the caret
        self.assertEqual(("".join(buf), pos), ("abc", 2))
        pos = edit_line(buf, pos, 'SPACE')
        self.assertEqual(("".join(buf), pos), ("ab c", 3))
        pos = edit_line(buf, pos, 'BACKSPACE')
        self.assertEqual(("".join(buf), pos), ("abc", 2))
        self.assertEqual(edit_line(buf, 0, 'HOME'), 0)   # 0 is a position, not "unhandled"
        self.assertEqual(edit_line(buf, 0, 'BACKSPACE'), 0)
        self.assertEqual(edit_line(buf, 0, 'DELETE'), 0)
        self.assertEqual("".join(buf), "bc")
        self.assertEqual(edit_line(buf, 2, 'DELETE'), 2)  # at the end: nothing to delete
        self.assertEqual((edit_line(buf, 0, 'LEFT'), edit_line(buf, 2, 'RIGHT'), edit_line(buf, 0, 'END')), (0, 2, 2))
        self.assertIsNone(edit_line(buf, 0, 'ENTER'))
        self.assertIsNone(edit_line(buf, 0, 'MOUSE_CLICK:0:1:1'))


if __name__ == "__main__":
    unittest.main()
