"""One album credit everywhere (music_library.album_credit): the album artist
tag, else "Various Artists" for a flagged compilation, else the artists on
every track. Search's album and disc results use it too, so a compilation with
no album artist is one album rather than one per track artist."""
import unittest

from mutagen.id3 import ID3, TCMP

from backtrack import search
from backtrack.music_library import album_credit, compilation_flag


def _track(n, artist, album_artist='', compilation=False, disc='1', total_discs='1'):
    return {'path': f'/m/{n}.mp3', 'title': f'Track {n}', 'artist': artist, 'album': 'Cafe del Mar Volumen Uno',
            'album_artist': album_artist, 'compilation': compilation,
            'disc': disc, 'total_discs': total_discs, 'disc_subtitle': ''}


class AlbumCreditTest(unittest.TestCase):
    def test_album_artist_tag_wins(self):
        self.assertEqual(album_credit([_track(1, 'A', 'José Padilla', True)]), 'José Padilla')

    def test_flagged_compilation_is_various_artists(self):
        # One track (or every track by the same act) would otherwise derive that act.
        self.assertEqual(album_credit([_track(1, 'A', compilation=True),
                                       _track(2, 'A', compilation=True)]), 'Various Artists')

    def test_unflagged_falls_back_to_the_track_casts(self):
        self.assertEqual(album_credit([_track(1, 'A'), _track(2, 'A')]), 'A')
        self.assertEqual(album_credit([_track(1, 'A'), _track(2, 'B')]), 'Various Artists')

    def test_reads_the_id3_flag(self):
        tags = ID3()
        self.assertFalse(compilation_flag(tags))
        tags.add(TCMP(encoding=3, text=['1']))
        self.assertTrue(compilation_flag(tags))
        tags.setall('TCMP', [TCMP(encoding=3, text=['0'])])
        self.assertFalse(compilation_flag(tags))


class SearchCompilationTest(unittest.TestCase):
    def _collect(self, library, query):
        results = search.search(library, query, fields=['title', 'artist', 'album', 'disc_label'])
        tokens = search.tokenize(query)
        return search.collect_entities(results, tokens), search.collect_disc_entities(results, tokens)

    def test_one_album_and_one_disc_credited_to_various_artists(self):
        library = [_track(n, artist, compilation=True, disc='1', total_discs='2')
                   for n, artist in enumerate(('Afterlife', 'Lamb', 'Moby'))]
        ents, discs = self._collect(library, 'cafe del mar')
        self.assertEqual([(e.name, e.subtitle, len(e.tracks)) for e in ents['album']],
                         [('Cafe del Mar Volumen Uno', 'Various Artists', 3)])
        self.assertEqual([(e.subtitle, len(e.tracks)) for e in discs], [('Various Artists', 3)])


if __name__ == '__main__':
    unittest.main()
