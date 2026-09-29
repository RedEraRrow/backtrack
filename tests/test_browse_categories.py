"""The extra Browse categories: year/decade/people grouping, optional credits
left out rather than filed under "Unknown", and the saved menu order."""
import unittest

from backtrack import music_library as ml
from backtrack.menus import common as menus


def t(path, **kw):
    return dict(path=path, **kw)


class GroupingTest(unittest.TestCase):
    def test_years_and_decades_come_from_any_date_form_and_skip_undated(self):
        lib = [t('a', year='1994-05-01 18:30:00'), t('b', year='1999'), t('c', year='Unknown Year')]
        self.assertEqual(sorted(ml.get_grouped_data(lib, 'year')), ['1994', '1999'])
        self.assertEqual({k: len(v) for k, v in ml.get_grouped_data(lib, 'decade').items()}, {'1990s': 2})

    def test_people_group_by_name_whatever_their_roles(self):
        lib = [t('a', credits=[['Olivia Colman', 'Minka'], ['Tom Goodman-Hill', 'Archie']]),
               t('b', credits=[['Olivia Colman', 'Jane'], ['Olivia Colman', 'Narrator']])]
        g = ml.get_grouped_data(lib, 'people')
        self.assertEqual(sorted(g), ['Olivia Colman', 'Tom Goodman-Hill'])
        self.assertEqual(len(g['Olivia Colman']), 2)

    def test_a_role_containing_commas_is_one_person_from_the_real_tag(self):
        import os
        import shutil
        import tempfile
        from mutagen.id3 import ID3, TMCL
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "ep.mp3")
            open(path, "wb").close()
            tags = ID3()
            tags.add(TMCL(encoding=3, people=[["Sundry Ruffians, Publishers, and Whackwallop",
                                               "Mark Evans"]]))
            tags.save(path)
            song = ml.get_metadata(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(song['credits'], [['Mark Evans', 'Sundry Ruffians, Publishers, and Whackwallop']])
        self.assertEqual(list(ml.get_grouped_data([song], 'people')), ['Mark Evans'])
        from backtrack import search
        self.assertEqual(search._entity_values(song, 'people'), ['Mark Evans'])

    def test_tracks_without_a_composer_are_not_one_unknown_row(self):
        lib = [t('a', composer='Bach'), t('b', composer='')]
        self.assertEqual(list(ml.get_grouped_data(lib, 'composer')), ['Bach'])


class BrowseMenuSettingTest(unittest.TestCase):
    def test_default_order_and_unknown_or_repeated_keys_dropped(self):
        self.assertEqual(menus.browse_menu_keys({}), menus.DEFAULT_BROWSE_MENU)
        cfg = {'browse_menu': ['people', 'nonsense', 'years', 'people']}
        self.assertEqual(menus.browse_menu_keys(cfg), ['people', 'years'])


if __name__ == "__main__":
    unittest.main()
