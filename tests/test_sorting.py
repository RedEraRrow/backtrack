"""The shared sort chain (music_library.sort_tracks / sort_albums): albums stay
whole and in track order whatever their tracks' years or performers, missing
values go last in either direction, and a library can have its own chain."""
import random
import unittest
from unittest.mock import patch

from src import music_library as ml

CFG = {'music_directories': ['/m/Radio', '/m/Music']}


def t(path, album, disc=1, track=1, year='', title='', artist='', aa='', **kw):
    return dict(path=path, album=album, disc=str(disc), track=str(track), year=year,
                title=title or path, artist=artist, album_artist=aa, **kw)


def order(tracks, cfg=CFG, levels=None):
    return [x['path'] for x in ml.sort_tracks(tracks, cfg, levels=levels)]


class SortTracksTest(unittest.TestCase):
    def test_series_with_a_year_per_disc_plays_in_disc_order(self):
        # Each disc is a later series; per-track year used to put disc 2 first.
        tracks = [t('/m/Radio/b', 'Series', 2, 1, '2011-07-21', aa='Milton'),
                  t('/m/Radio/a', 'Series', 1, 1, '2010-03-04', aa='Milton'),
                  t('/m/Radio/c', 'Series', 1, 2, '2010-03-11', aa='Milton')]
        self.assertEqual(order(tracks), ['/m/Radio/a', '/m/Radio/c', '/m/Radio/b'])

    def test_compilation_with_mixed_performer_sort_tags_keeps_track_order(self):
        tracks = [t('/m/Music/1', 'Mix', 1, 1, artist='Zed', **{'Performer Sort Order': 'Zed'}),
                  t('/m/Music/2', 'Mix', 1, 2, artist='Abe', **{'Performer Sort Order': 'Abe'}),
                  t('/m/Music/3', 'Mix', 1, 3, artist='Moe')]
        self.assertEqual(order(tracks), ['/m/Music/1', '/m/Music/2', '/m/Music/3'])

    def test_missing_track_numbers_fall_back_to_title_then_oldest(self):
        tracks = [t('/m/Music/x', 'A', track='', title='Beta', year='2001'),
                  t('/m/Music/y', 'A', track='', title='Alpha', year='2005'),
                  t('/m/Music/z', 'A', track='', title='Alpha', year='2002')]
        self.assertEqual(order(tracks), ['/m/Music/z', '/m/Music/y', '/m/Music/x'])

    def test_albums_are_a_to_z_with_articles_accents_and_numbers_handled(self):
        tracks = [t('/m/Music/1', 'Vol. 10'), t('/m/Music/2', 'Vol. 2'),
                  t('/m/Music/3', 'The Cars'), t('/m/Music/4', 'Café'), t('/m/Music/5', 'Bees')]
        self.assertEqual(order(tracks),
                         ['/m/Music/5', '/m/Music/4', '/m/Music/3', '/m/Music/2', '/m/Music/1'])

    def test_newest_first_and_missing_years_last_either_way(self):
        tracks = [t('/m/Music/old', 'Old', year='1990'), t('/m/Music/new', 'New', year='2020'),
                  t('/m/Music/none', 'None')]
        desc = [['album_year', 'desc'], ['album', 'asc']]
        asc = [['album_year', 'asc'], ['album', 'asc']]
        self.assertEqual(order(tracks, levels=desc), ['/m/Music/new', '/m/Music/old', '/m/Music/none'])
        self.assertEqual(order(tracks, levels=asc), ['/m/Music/old', '/m/Music/new', '/m/Music/none'])

    def test_broadcast_order_interleaves_albums_by_date(self):
        tracks = [t('/m/Radio/a1', 'A', 1, 1, '2001-01-01'), t('/m/Radio/a2', 'A', 1, 2, '2001-01-15'),
                  t('/m/Radio/b1', 'B', 1, 1, '2001-01-08')]
        self.assertEqual(order(tracks, levels=[['date', 'asc']]),
                         ['/m/Radio/a1', '/m/Radio/b1', '/m/Radio/a2'])

    def test_a_librarys_own_chain_applies_only_when_everything_is_from_it(self):
        cfg = {**CFG, 'library_sort_levels': {'/m/Radio': [['date', 'asc']]}}
        radio = [t('/m/Radio/x', 'X', year='2001')]
        self.assertEqual(ml.resolve_levels(radio, cfg), ([['date', 'asc']], '/m/Radio'))
        mixed = radio + [t('/m/Music/y', 'Y')]
        self.assertEqual(ml.resolve_levels(mixed, cfg)[1], None)

    def test_sort_albums_uses_only_album_level_fields(self):
        albums = {'B': [t('/m/Music/b', 'B', year='1990')], 'A': [t('/m/Music/a', 'A', year='2000')]}
        levels = [['date', 'asc'], ['album_year', 'desc']]
        self.assertEqual(ml.sort_albums(['B', 'A'], albums, CFG, levels=levels), ['A', 'B'])


class AlbumShuffleTest(unittest.TestCase):
    def test_albums_move_as_whole_runs_in_track_order(self):
        from src import menus
        lib = [t(f'/m/Music/{a}{n}', a, 1, n) for a in 'ABCD' for n in (1, 2, 3)]
        paths = [x['path'] for x in lib]
        with patch.object(menus, 'play_queue', return_value=None) as pq:
            random.seed(3)
            menus._play_list('__album_shuffle__', paths, lib)
        played = pq.call_args[0][0]
        self.assertCountEqual(played, paths)
        runs = [played[i:i + 3] for i in range(0, 12, 3)]
        for run in runs:
            self.assertEqual([p[-1] for p in run], ['1', '2', '3'])
            self.assertEqual(len({p[-2] for p in run}), 1)


if __name__ == "__main__":
    unittest.main()
