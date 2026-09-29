"""select()'s J/K row moves: the caller is asked, the row swaps on screen only
when it agrees, and a move never crosses a separator or the ends of the list. Also: a ListPlace
finds the same item after a re-sort, and greyed rows can't be ticked."""
import unittest
from unittest.mock import patch

from src.utils import prompt
from src.utils.prompt import lists


def _run(keys, choices, on_move=None, index=0, **kw):
    """Drive select() with a scripted key sequence, returning its result."""
    feed = iter(keys)
    with patch.object(lists, '_read_key', lambda fd: next(feed)), \
         patch.object(lists, '_wait_for_keypress', lambda t: True), \
         patch.object(lists, '_set_raw'), patch.object(lists, '_restore_term_attrs'), \
         patch.object(lists, '_get_term_attrs'), patch.object(lists, '_Widget'), \
         patch.object(lists.sys.stdin, 'fileno', lambda: 0):
        return prompt.select("", choices=choices, on_move=on_move, index=index, **kw)


class SelectMoveTest(unittest.TestCase):
    def test_moves_follow_the_row_and_stop_at_a_separator(self):
        order = ['a', 'b', 'c']
        calls = []

        def move(v, d):
            calls.append((v, d))
            i = order.index(v)
            if not 0 <= i + d < len(order):
                return False
            order[i], order[i + d] = order[i + d], order[i]
            return True

        choices = [prompt.Choice(title=x, value=x) for x in order] + [prompt.separator(), 'z']
        # K twice moves 'a' to the bottom; a third K would cross the separator.
        res = _run([prompt.MOVE_DOWN_KEY] * 3 + ['ENTER'], choices, move)
        self.assertEqual(order, ['b', 'c', 'a'])
        self.assertEqual(res, 'a')                      # the cursor followed it
        self.assertEqual(calls, [('a', 1), ('a', 1)])   # never asked past the separator
        res = _run([prompt.MOVE_UP_KEY, 'ENTER'], [prompt.Choice(title=x, value=x) for x in order],
                   lambda v, d: False, index=2)
        self.assertEqual(res, 'a')                      # refused: nothing moved

    def test_a_place_comes_back_to_the_same_item_after_a_resort(self):
        place = prompt.ListPlace()
        _run(['DOWN', 's'], ['a', 'b', 'c'], shortcuts={'s': '__sort__'}, place=place)
        self.assertEqual(place.value, 'b')
        self.assertEqual(_run(['ENTER'], ['c', 'b', 'a'], place=place), 'b')
        # The item has gone: the same position instead.
        self.assertEqual(_run(['ENTER'], ['a', 'c'], place=place), 'c')
        place.reset()
        self.assertEqual(_run(['ENTER'], ['x', 'y'], place=place), 'x')


    def test_greyed_rows_cannot_be_ticked(self):
        def rows():
            return [prompt.Choice(title='same', value='same', disabled=True),
                    prompt.Choice(title='new', value='new', checked=True),
                    prompt.Choice(title='kept', value='kept', disabled=True)]
        # The cursor starts on the changing row; space there unticks it; moving
        # down can't reach the greyed row, so a second space re-ticks the same one.
        self.assertEqual(_run(['SPACE', 'DOWN', 'SPACE', 'ENTER'], rows(), multi=True), ['new'])
        self.assertEqual(_run(['SPACE', 'ENTER'], rows(), multi=True), [])
        self.assertEqual(_run(['SPACE', 'a', 'ENTER'], rows(), multi=True), ['new'])   # select-all too


if __name__ == "__main__":
    unittest.main()
