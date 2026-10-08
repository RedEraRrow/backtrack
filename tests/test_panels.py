"""The player's panels: which show (w), and where, for each shape of window
(Arrange): their order down a column, the side of the player they're on."""
import io
import sys
import unittest
from unittest.mock import patch

from backbone import ui
from backtrack.playback import player_art as pa
from backtrack.playback import player_ui as pu


class _Frame:
    people = [["vocals", "Ann"], ["guitar", "Bo"]]


class _Audio:
    def getall(self, key):
        return {"TMCL": [_Frame()], "TIPL": [_Frame()], "USLT": ["words"]}.get(key, [])

    def get(self, key):
        return None


class PanelsTest(unittest.TestCase):
    def setUp(self):
        self.saved = patch('backtrack.config.update_config')
        self.saved.start()
        pu._ui_state.update(panels={'wide': [], 'tall': []}, arranging=None)

    def tearDown(self):
        self.saved.stop()
        pu._ui_state.update(panels={'wide': [], 'tall': []}, arranging=None)

    def test_switching_on_adds_to_both_shapes_and_off_takes_away(self):
        pu.set_panels(['lyrics', 'people'])
        self.assertEqual(pu._ui_state['panels']['wide'], [['lyrics', 'right'], ['people', 'right']])
        self.assertEqual(pu._ui_state['panels']['tall'], [['lyrics', 'right'], ['people', 'right']])
        self.assertTrue(pu._ui_state['show_lyrics'] and pu._ui_state['show_credits'])
        pu._ui_state['panels']['tall'][0][1] = 'left'                     # an arrangement survives
        pu.set_panels(['lyrics'])
        self.assertEqual(pu._ui_state['panels']['tall'], [['lyrics', 'left']])
        self.assertFalse(pu._ui_state['show_credits'])

    def test_chapters_take_the_keys_only_without_a_queue(self):
        pu.set_panels(['chapters'])
        self.assertTrue(pu.chapters_listed())
        pu.set_panels(['chapters', 'queue'])
        self.assertFalse(pu.chapters_listed())

    def test_arranging_moves_the_panel_in_this_shape_only(self):
        pu.set_panels(['lyrics', 'people', 'queue'])
        pu._frame.update(panel_order=['lyrics', 'people', 'queue'], shape='wide')
        pu._ui_state['arranging'] = 'lyrics'
        for key in ('DOWN', 'LEFT', 'TAB'):
            self.assertTrue(pu.arrange_key(key))
        wide = pu._ui_state['panels']['wide']
        self.assertEqual(wide, [['people', 'right'], ['lyrics', 'left'], ['queue', 'right']])
        self.assertEqual(pu._ui_state['arranging'], 'people')
        self.assertEqual(pu._ui_state['panels']['tall'][0], ['lyrics', 'right'])      # the other shape kept
        self.assertTrue(pu.arrange_key('x'))                                # nothing else while arranging
        self.assertTrue(pu.arrange_key('ENTER'))
        self.assertIsNone(pu._ui_state['arranging'])
        self.assertFalse(pu.arrange_key('UP'))                              # done: keys are the player's again

    def test_a_column_stacks_with_no_gap_and_drops_what_wont_fit(self):
        stack = pu._stack(['people', 'lyrics', 'queue'], 5, 30, 6)
        self.assertEqual([k for k, *_ in stack], ['people', 'lyrics', 'queue'])
        self.assertEqual((stack[0][1], stack[-1][2]), (5, 30))
        self.assertTrue(all(b[1] == a[2] + 1 for a, b in zip(stack, stack[1:])))
        self.assertEqual(stack[0][2] - stack[0][1] + 1, 6)                  # the people what they need
        self.assertEqual([k for k, *_ in pu._stack(['lyrics', 'queue'], 1, 5, 6)], ['lyrics'])
        self.assertEqual(pu._stack(['people'], 1, 20, 6), [('people', 1, 20)])   # alone: stretched

    def _boxes(self, cols, rows, wide, tall):
        pu._ui_state.update(panels={'wide': wide, 'tall': tall}, arranging=None, lyrics_pane=True)
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(ui, 'tab_rows', lambda: 2), \
             patch.object(pa, 'inline_art_enabled', lambda: False), \
             patch.object(pu, '_meta_left_lines', lambda *a: ["Title", "Artist"]), \
             patch.object(sys, 'stdout', io.StringIO()):
            pu._draw_default_ui("", _Audio(), pa.IDLE_ART, (cols, rows))
        return list(pu._frame['boxes']), pu._frame['shape']

    def test_a_wide_window_puts_panels_either_side(self):
        boxes, shape = self._boxes(200, 40, [['people', 'left'], ['lyrics', 'right']], [])
        player, people, lyrics, transport = boxes[0], boxes[1], boxes[2], boxes[-1]
        self.assertEqual(shape, 'wide')
        self.assertLess(people[3], player[2])                               # left of the player
        self.assertGreater(lyrics[2], player[3])                            # right of it
        self.assertEqual((people[2], lyrics[3]), (transport[2], transport[3]))   # edge to edge

    def test_a_tall_window_uses_its_own_arrangement(self):
        tall = [['lyrics', 'left'], ['people', 'right']]
        boxes, shape = self._boxes(90, 60, [['lyrics', 'left'], ['people', 'right']], tall)
        self.assertEqual(shape, 'tall')
        player, lyrics, people = boxes[0], boxes[1], boxes[2]
        self.assertGreater(lyrics[0], player[1])                            # under the player
        self.assertEqual(lyrics[0], people[0])                              # side by side
        self.assertLess(lyrics[3], people[2])


if __name__ == "__main__":
    unittest.main()
