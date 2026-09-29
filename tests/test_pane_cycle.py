"""`w` in the player: nothing to show means nothing to redraw."""
import unittest

from src.playback import playback_ui as ui


class PaneCycleTest(unittest.TestCase):
    def setUp(self):
        ui._set_pane_mode('off')

    def test_no_panels_is_a_no_op(self):
        self.assertFalse(ui.cycle_right_pane(False, False, False))
        self.assertEqual(ui._ui_state['pane_mode'], 'off')

    def test_cycles_only_what_exists(self):
        seen = []
        for _ in range(3):
            self.assertTrue(ui.cycle_right_pane(False, True, True))
            seen.append(ui._ui_state['pane_mode'])
        self.assertEqual(seen, ['queue', 'credits', 'off'])


if __name__ == "__main__":
    unittest.main()
