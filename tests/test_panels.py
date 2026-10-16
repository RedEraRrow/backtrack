"""The player's newer panels: what each reads from the tags, and which a track has."""
import unittest
from unittest import mock

from mutagen.id3 import COMM, ID3, TCOM, TCOP, TEXT, TIPL, TPUB

from backtrack.playback import panels, player_ui


class PanelsTest(unittest.TestCase):
    def tags(self):
        a = ID3()
        a.add(TCOM(encoding=3, text=["Fred E. Ahlert"]))
        a.add(TEXT(encoding=3, text=["Roy Turk"]))
        a.add(TCOP(encoding=3, text=["2015 Pozzitive"]))
        a.add(TPUB(encoding=3, text=["BBC"]))
        a.add(COMM(encoding=3, lang='eng', desc='', text=["A rogue lemon."]))
        a.add(COMM(encoding=3, lang='eng', desc='iTunNORM', text=["0000 0001"]))
        return a

    def test_credits_come_from_their_own_tags(self):
        self.assertEqual(panels.tag_credits(self.tags()), [('composer', 'Fred E. Ahlert'), ('lyricist', 'Roy Turk')])

    def test_people_take_them_too_unless_turned_off(self):
        a = self.tags()
        a.add(TIPL(encoding=3, people=[['producer', 'David Tyler']]))
        with mock.patch.dict(player_ui._ui_state, people_all=True):
            self.assertIn(('composer', 'Fred E. Ahlert'), player_ui.people_of(a)[1])
        with mock.patch.dict(player_ui._ui_state, people_all=False):
            self.assertEqual(player_ui.people_of(a)[1], [('producer', 'David Tyler')])

    def test_notes_leave_out_itunes_bookkeeping(self):
        self.assertEqual(panels.notes(self.tags()), [("", "A rogue lemon.")])

    def test_several_comments_are_headed_and_one_language_each(self):
        from backbone import ui
        a = ID3()
        for lang, desc, text in (('deu', 'Description', "Eine Zitrone."), ('eng', 'Description', "A lemon."),
                                 ('xxx', 'Recording', "Live, 2011."), ('fra', 'Recording', "En direct.")):
            a.add(COMM(encoding=3, lang=lang, desc=desc, text=[text]))
        with mock.patch('backtrack.id3.languages.system_codes', return_value=('ger', 'deu')):
            self.assertEqual(panels.notes(a), [("Description", "Eine Zitrone."), ("Recording", "Live, 2011.")])
        with mock.patch('backtrack.id3.languages.system_codes', return_value=()):
            lines = [ui.strip_ansi(l) for l in panels.notes_lines(a, 30, 10)]
        self.assertEqual(lines, ["Description", "A lemon.", "", "Recording", "Live, 2011."])

    def test_info_names_the_copyright_and_label(self):
        with mock.patch.object(panels, '_plays', return_value=(0, '')):
            pairs = dict((k, v) for k, v in panels.info_pairs(self.tags(), '/nowhere.mp3') if k)
        self.assertEqual(pairs['Copyright'], "© 2015 Pozzitive")
        self.assertEqual(pairs['Label'], "BBC")

    def test_a_track_with_nothing_has_no_credits_notes_or_booklet(self):
        has = panels.has(ID3(), '/nowhere.mp3')
        self.assertFalse(has['credits'] or has['notes'] or has['booklet'])
        self.assertTrue(has['info'])

    def test_meters_and_bars_fit_their_box(self):
        import numpy as np
        from backbone import ui
        lines = panels._bars(np.full(45, 200, np.uint8), 30, 6, 1.0, {})
        self.assertEqual(len(lines), 6)
        self.assertTrue(all(ui.visual_len(l) == 30 for l in lines))
        for l in panels._meters(np.array([200, 180, 240, 230], np.uint8), 30, 5, {}):
            self.assertLessEqual(ui.visual_len(l), 30)


    def test_booklet_pages_come_from_its_folder_in_name_order(self):
        import os, shutil, tempfile
        d = tempfile.mkdtemp()
        try:
            track = os.path.join(d, 'CD1', '01.mp3')
            os.makedirs(os.path.dirname(track))
            open(track, 'w').close()
            os.makedirs(os.path.join(d, 'Booklet'))
            for name in ('page 10.jpg', 'page 2.jpg', 'notes.txt'):
                open(os.path.join(d, 'Booklet', name), 'w').close()
            open(os.path.join(d, 'Cover.jpg'), 'w').close()
            pages = [os.path.basename(p) for p in panels.booklet_pages(track)]
            self.assertEqual(pages, ['page 2.jpg', 'page 10.jpg'])
        finally:
            shutil.rmtree(d)


    def test_last_play_is_the_one_before_this_and_when_it_began(self):
        import datetime
        rows = [("2026-10-16 09:00:00", "600s", "/a.mp3"), ("2026-10-15 20:10:00", "600s", "/a.mp3"),
                ("2026-10-15 19:00:00", "60s", "/b.mp3")]
        now_started = datetime.datetime(2026, 10, 16, 8, 0).timestamp()      # the 09:00 one ended after this began
        panels._plays.cache_clear()
        with mock.patch('backtrack.history.get_history', return_value=rows):
            self.assertEqual(panels._plays('/a.mp3', 1.0, now_started), (2, "2026-10-15 20:00"))


    def test_credits_greyed_as_a_repeat_only_when_people_has_nothing_else(self):
        a = self.tags()
        with mock.patch.dict(player_ui._ui_state, people_all=True):
            self.assertEqual(player_ui.panel_notes(a), {'credits': "repeat data"})
            self.assertFalse(player_ui.track_has(a, '/nowhere.mp3', True, False)['credits'])
            a.add(TIPL(encoding=3, people=[['producer', 'David Tyler']]))
            self.assertEqual(player_ui.panel_notes(a), {})
        with mock.patch.dict(player_ui._ui_state, people_all=False):
            self.assertEqual(player_ui.panel_notes(self.tags()), {})
            self.assertTrue(player_ui.track_has(self.tags(), '/nowhere.mp3', False, False)['credits'])


if __name__ == '__main__':
    unittest.main()
