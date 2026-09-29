"""Transport buttons: both icon sets, and clicks landing on the columns drawn."""
import unittest

from backtrack.playback import player_ui


class TransportIconsTest(unittest.TestCase):
    def tearDown(self):
        player_ui._ui_state['nerd_icons'] = False

    def test_click_columns_follow_each_icon_set(self):
        player_ui.geom.art_left, player_ui.geom.art_width = 0, 64
        for nerd, prev_col in ((False, 27), (True, 28)):
            player_ui._ui_state['nerd_icons'] = nerd
            player_ui._controls_line(False, False, 50, '')
            cols = player_ui._last_transport_cols
            self.assertEqual((cols['prev'], cols['playpause'], cols['next']), (prev_col, 32, 36))
            self.assertEqual(player_ui.transport_click_action(5, 37, 5), 'next')


if __name__ == "__main__":
    unittest.main()
