"""Features the audit found silently not working: toasts, the history switch,
same-named albums, search splitting "AC/DC", people credits round-tripping,
fractional discs, unnumbered tracks in a reflow, equaliser click columns."""
import unittest
from unittest.mock import patch

from src import music_library as ml
from src import search
from src.utils import ui_utils


class PlayerToastTest(unittest.TestCase):
    def test_the_toast_is_on_the_controls_line(self):
        from src.playback import playback_ui as ui
        status, _hints = ui._controls_line(False, False, 50, "Seek Forward +5s")
        self.assertIn("Seek Forward +5s", ui_utils.strip_ansi(status))


class HistorySwitchTest(unittest.TestCase):
    def test_nothing_is_logged_with_history_off(self):
        from src import history
        with patch("src.config.load_config", lambda: {"history_enabled": False}), \
             patch("builtins.open") as opened:
            history.log_listening_history("/m/a.mp3", 0, 30)
        opened.assert_not_called()


class GroupingTest(unittest.TestCase):
    def test_same_named_albums_by_different_artists_stay_apart(self):
        lib = [{'album': 'Greatest Hits', 'album_artist': 'ABBA'},
               {'album': 'Greatest Hits', 'album_artist': 'Queen'},
               {'album': 'Solo', 'album_artist': ''}]
        self.assertEqual(sorted(ml.get_grouped_data(lib, 'album')),
                         ['Greatest Hits (ABBA)', 'Greatest Hits (Queen)', 'Solo'])

    def test_search_splits_names_the_way_browse_does(self):
        self.assertEqual(search._entity_values({'artist': 'AC/DC'}, 'artist'), ['AC/DC'])
        self.assertEqual(search._entity_values({'artist': 'Barlow; Williams'}, 'artist'),
                         ['Barlow', 'Williams'])


class TagTextTest(unittest.TestCase):
    def test_people_credits_round_trip_through_text(self):
        from src.id3.id3_tag_handler import people_from_text, people_to_text
        pairs = [['Writer', 'Mark Evans'], ['Sundry Ruffians, Publishers', 'Mark Evans'],
                 ['', 'Solo Name']]
        self.assertEqual(people_from_text(people_to_text(pairs)), pairs)

    def test_a_fractional_disc_is_written_as_it_is(self):
        from src.id3.tag_writer import _fmt_pair
        self.assertEqual(_fmt_pair('1.5', 3), '1.5/3')
        self.assertEqual(_fmt_pair('4', '12'), '4/12')

    def test_reflow_leaves_an_unnumbered_track_alone(self):
        from src import bulk_pattern as bp
        songs = [{'path': 'a', 'disc': '1', 'track': '2'}, {'path': 'b', 'disc': '1', 'track': ''}]
        plan = bp.reflow_discs(songs, track_totals=True)
        self.assertNotIn('track', plan['b'])            # was written as track 1
        self.assertEqual(plan['a']['track'], 2)


class EqualiserClickTest(unittest.TestCase):
    def test_band_columns_are_shared_by_drawing_and_clicks(self):
        from src.utils.prompt import audio as prompt
        cols = prompt._eq_band_x(4, 40)
        self.assertEqual(cols, [5, 15, 25, 35])
        self.assertTrue(all(c < 40 for c in prompt._eq_band_x(40, 40)))


if __name__ == "__main__":
    unittest.main()
