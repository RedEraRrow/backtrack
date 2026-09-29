"""The lyric pane's layout: the speaker column, a split line named once, and centring."""
import unittest

from backbone import ui
from backtrack.lyrics import lyric_pane


def _frame(tl, **kw):
    return [ui.strip_ansi(l) for l in
            lyric_pane.frame(tl, 6.0, lyric_pane.Geometry(row=1, col=1, width=60, bottom=20, **kw))]


class LyricPaneLayoutTest(unittest.TestCase):
    def setUp(self):
        self.song = lyric_pane.from_sylt([("First", 0), ("Second", 5000), ("Third", 9000)], 20.0)

    def test_words_start_at_the_top_with_no_divider(self):
        self.assertEqual(_frame(self.song)[0], '│ First')

    def test_no_speaker_column_without_speakers(self):
        self.assertIn('│ Second', _frame(self.song))

    def test_speaker_column_kept_for_dialogue(self):
        beats = [lyric_pane.Beat(0, 5, speaker='Ann', text='Hello'),
                 lyric_pane.Beat(5, 10, speaker='Bob', text='Hi')]
        rows = _frame(lyric_pane.Timeline(beats, 10.0))
        self.assertTrue(any(r.startswith('Bob') and r.endswith('│ Hi') and r.index('│') > 12
                            for r in rows))

    def test_a_split_line_names_its_speaker_once_beside_the_current_segment(self):
        B = lyric_pane.Beat
        beats = [B(0, 5, speaker='Ann', aside='quietly', line=0, text='One.'),
                 B(5, 10, speaker='Ann', aside='quietly', line=0, text='Two.'),
                 B(10, 15, speaker='Ann', aside='quietly', line=0, text='Three.')]
        rows = _frame(lyric_pane.Timeline(beats, 15.0))
        said = [r for r in rows if '│' in r]
        self.assertEqual([r.split('│')[1].strip() for r in said], ['One.', 'Two.', 'Three.'])
        self.assertEqual(sum('Ann' in r for r in rows), 1)
        self.assertTrue(said[1].startswith('Ann'))                   # beside the current one
        self.assertTrue(any(r.startswith('(quietly)') for r in rows))  # the aside stays up

    def test_separate_lines_each_name_their_speaker(self):
        B = lyric_pane.Beat
        beats = [B(0, 5, speaker='Ann', line=0, text='Hi.'),
                 B(5, 10, speaker='Bob', line=1, text='Hey.'),
                 B(10, 15, speaker='Ann', line=2, text='Bye.')]
        rows = _frame(lyric_pane.Timeline(beats, 15.0))
        self.assertEqual([r[:3] for r in rows if '│' in r], ['Ann', 'Bob', 'Ann'])

    def test_current_line_held_on_the_centre_row(self):
        rows = _frame(self.song, centre=10)
        self.assertEqual(rows[10 - 1], '│ Second')

    def test_centre_never_pulls_the_words_up(self):
        self.assertEqual(_frame(self.song, centre=1), _frame(self.song))


if __name__ == "__main__":
    unittest.main()
