"""Boxed, the player's panels each get a box: the people and the lyrics apart."""
import io
import sys
import unittest
from unittest.mock import patch

from backbone import ui
from backtrack.playback import player_art as pa, player_ui as pu


class _Frame:
    people = [["vocals", "Ann"], ["guitar", "Bo"]]


class _Audio:
    def getall(self, key):
        return {"TMCL": [_Frame()], "TIPL": [_Frame()], "USLT": ["words"]}.get(key, [])

    def get(self, key):
        return None


class PanesTest(unittest.TestCase):
    def _boxes(self, cols, rows, both=None):
        both = both or [['people', 'right'], ['lyrics', 'right']]
        pu._ui_state.update(panels={'wide': [list(x) for x in both], 'tall': [list(x) for x in both]},
                            arranging=None, lyrics_pane=True)
        with patch.object(ui, 'get_terminal_size', lambda *a: (cols, rows)), \
             patch.object(ui, 'tab_rows', lambda: 3), \
             patch.object(pa, 'inline_art_enabled', lambda: False), \
             patch.object(pu, '_meta_left_lines', lambda *a: ["Title", "Artist"]), \
             patch.object(sys, 'stdout', io.StringIO()):
            pu._lyric_src.update(track="", files=["embedded USLT"])   # the lyrics the player loaded
            pu._draw_default_ui("", _Audio(), pa.IDLE_ART, (cols, rows))
        return list(pu._frame['boxes'])

    def test_wide_the_people_over_the_lyrics_beside_the_player(self):
        player, people, lyrics, transport = self._boxes(150, 40)
        self.assertEqual(people[2:], lyrics[2:])                    # one column, down the right
        self.assertEqual(lyrics[0], people[1] + 1)                  # the lyrics' box under the people's
        self.assertGreater(people[2], player[3])                    # beside the player's

    def test_narrow_the_panels_under_the_player(self):
        player, people, lyrics, transport = self._boxes(70, 50)
        self.assertEqual(people[0], player[1] + 1)
        self.assertEqual(lyrics[0], people[1] + 1)
        self.assertEqual(people[2:], lyrics[2:])                    # all the width
        self.assertEqual(player[2:], people[2:])                    # the player's too: no gap beside it
        self.assertGreaterEqual(lyrics[1] - lyrics[0] - 1, pu._PANEL_MIN_ROWS - 2)

    def test_the_boxes_tile_the_body_with_no_gap(self):
        for cols, rows in ((70, 50), (90, 30), (100, 50), (150, 40), (200, 60)):
            player, *panels, transport = self._boxes(cols, rows)
            self.assertEqual((player[0], player[2]), (transport[0] - (player[1] - player[0] + 1) - (
                sum(p[1] - p[0] + 1 for p in panels) if panels[0][2] == player[2] else 0), transport[2]),
                (cols, rows))
            self.assertEqual(max(b[3] for b in panels + [player]), transport[3], (cols, rows))   # out to the right
            if panels[0][2] != player[2]:                                # beside: the same height
                self.assertEqual((player[1], panels[-1][1]), (transport[0] - 1,) * 2, (cols, rows))
                self.assertEqual(panels[0][2], player[3] + 1 + ui.MARGIN_H, (cols, rows))

    def test_a_progress_tick_writes_only_what_changed(self):
        *_rest, transport = self._boxes(120, 40)
        row = pu._frame['prog_row']
        with patch.object(ui, 'get_terminal_size', lambda *a: (120, 40)):
            for _ in range(2):
                out = io.StringIO()
                with patch.object(sys, 'stdout', out):
                    pu.update_progress_ui(row, 5.0, 15.0, 120)
        self.assertEqual(out.getvalue(), "")              # the same moment again: nothing (no border fight)

    def test_the_transport_box_runs_along_the_bottom(self):
        *_rest, transport = self._boxes(120, 40)
        self.assertEqual(transport[1] - transport[0], 2)            # one row inside
        self.assertEqual(pu._frame['prog_row'], transport[0] + 1)
        self.assertGreater(pu._frame['bar'][0], transport[2] + 4)   # after the controls


class PeopleTest(unittest.TestCase):
    CAST = [["vocals", f"Singer {i}"] for i in range(10)]
    CREW = [["producer", f"Knob {i}"] for i in range(6)]

    def test_one_column_when_they_fit(self):
        lines = pu._people_lines(self.CAST, self.CREW, 60, 30)
        self.assertEqual(len(lines), 17)                             # everyone, a gap between the two
        self.assertNotIn("│", ui.strip_ansi("".join(lines)))

    def test_two_columns_when_they_dont(self):
        lines = [ui.strip_ansi(l) for l in pu._people_lines(self.CAST, self.CREW, 80, 9)]
        self.assertEqual(len(lines), 9)
        self.assertTrue(all("│" in l for l in lines[:8]))
        self.assertIn("Knob 5", "".join(lines))                      # still everyone

    def test_too_many_even_for_two_are_counted(self):
        lines = [ui.strip_ansi(l) for l in pu._people_lines(self.CAST, self.CREW, 80, 4)]
        self.assertEqual(len(lines), 4)
        self.assertIn("more", "".join(lines))


class PeopleCutTest(unittest.TestCase):
    def test_a_role_too_long_ends_in_an_ellipsis_like_a_name(self):
        lines = [ui.strip_ansi(l) for l in pu._people_lines([], [('Executive Producer', 'Someone')], 24, 5)]
        self.assertTrue(lines[0].endswith("…"), lines)
        self.assertLessEqual(ui.visual_len(lines[0]), 24)


if __name__ == "__main__":
    unittest.main()


class PlacesTest(unittest.TestCase):
    """Bands across the whole width, and panels above or below the art."""
    _boxes = PanesTest._boxes

    def test_a_band_spans_the_width_over_the_player_and_its_columns(self):
        player, *panels, transport = self._boxes(150, 44, [['lyrics', 'top'], ['people', 'right']])
        band = next(b for b in panels if b[0] < player[0])
        self.assertEqual((band[2], band[3]), (transport[2], transport[3]))        # the full width
        self.assertEqual(player[0], band[1] + 1)                                 # the player under it
        self.assertTrue(any(b[2] > player[3] for b in panels))                   # the column still beside it

    def test_a_panel_above_the_art_sits_in_the_players_column(self):
        player, *panels, transport = self._boxes(150, 44, [['people', 'above'], ['lyrics', 'right']])
        above = next(b for b in panels if b[1] < player[0])
        self.assertEqual((above[2], above[3]), (player[2], player[3]))
        self.assertEqual(above[1] + 1, player[0])

    def test_arrange_moves_along_the_places(self):
        pu._ui_state.update(panels={'wide': [['people', 'right'], ['lyrics', 'right']], 'tall': []},
                            arranging='lyrics')
        pu._frame.update(panel_order=['people', 'lyrics'], shape='wide')
        with patch.object(pu, '_panels_changed', lambda: None):
            pu.arrange_key('DOWN')                     # last in its column: out into the bottom band
            self.assertIn(['lyrics', 'bottom'], pu._ui_state['panels']['wide'])
            pu.arrange_key('UP')                       # up from the bottom band: below the player
            self.assertIn(['lyrics', 'below'], pu._ui_state['panels']['wide'])
            pu.arrange_key('UP')                       # past the player: above it
            self.assertIn(['lyrics', 'above'], pu._ui_state['panels']['wide'])
            pu.arrange_key('LEFT')                     # out of the player's column: the left one
            self.assertIn(['lyrics', 'left'], pu._ui_state['panels']['wide'])
            pu.arrange_key('RIGHT')                    # towards the player: below it
            self.assertIn(['lyrics', 'below'], pu._ui_state['panels']['wide'])
        pu._ui_state['arranging'] = None
