"""Tests for disc-level search: the computed 'disc_label' field and
collect_disc_entities (src/search.py). Lets a query like "John Finnemore's
Souvenir Programme Series 1" isolate just that disc's tracks."""
import unittest

from src import search


def _track(path, title, artist, album, disc='1', total_discs='1', disc_subtitle=''):
    return {'path': path, 'title': title, 'artist': artist, 'album': album,
            'disc': disc, 'total_discs': total_discs, 'disc_subtitle': disc_subtitle}


class DiscLabelTest(unittest.TestCase):
    def test_subtitle_wins_over_number_for_fuzzy_matching(self):
        # Deliberately just the subtitle, not both — see _disc_label's
        # docstring: combining them let a bare digit cross-match whichever
        # number happens to contain it when the two disagree.
        song = _track('/a', 't', 'ar', 'al', disc='1', total_discs='2', disc_subtitle='Series 1')
        self.assertEqual(search._disc_label(song), 'Series 1')

    def test_falls_back_to_disc_number_when_multi_disc(self):
        song = _track('/a', 't', 'ar', 'al', disc='3', total_discs='4')
        self.assertEqual(search._disc_label(song), 'Disc 3')

    def test_blank_for_single_disc_album(self):
        song = _track('/a', 't', 'ar', 'al', disc='1', total_discs='1')
        self.assertEqual(search._disc_label(song), '')

    def test_blank_when_total_discs_unparseable(self):
        song = _track('/a', 't', 'ar', 'al', disc='1', total_discs='weird')
        self.assertEqual(search._disc_label(song), '')


class DiscDisplayLabelTest(unittest.TestCase):
    def test_combines_subtitle_and_number(self):
        song = _track('/a', 't', 'ar', 'al', disc='5', total_discs='10', disc_subtitle='Series 4')
        self.assertEqual(search._disc_display_label(song), 'Series 4 (Disc 5)')

    def test_number_only_when_no_subtitle(self):
        song = _track('/a', 't', 'ar', 'al', disc='3', total_discs='4')
        self.assertEqual(search._disc_display_label(song), 'Disc 3')

    def test_blank_for_single_disc_album(self):
        song = _track('/a', 't', 'ar', 'al', disc='1', total_discs='1')
        self.assertEqual(search._disc_display_label(song), '')


class ExtractDiscConstraintTest(unittest.TestCase):
    def test_extracts_disc_and_number(self):
        remaining, disc_number = search.extract_disc_constraint(['jfsp', 'disc', '5'])
        self.assertEqual(remaining, ['jfsp'])
        self.assertEqual(disc_number, '5')

    def test_extracts_cd_and_number(self):
        remaining, disc_number = search.extract_disc_constraint(['cd', '2'])
        self.assertEqual(remaining, [])
        self.assertEqual(disc_number, '2')

    def test_no_match_leaves_tokens_untouched(self):
        remaining, disc_number = search.extract_disc_constraint(['jfsp', 'series', '4'])
        self.assertEqual(remaining, ['jfsp', 'series', '4'])
        self.assertIsNone(disc_number)

    def test_disc_word_without_a_following_number_is_left_alone(self):
        remaining, disc_number = search.extract_disc_constraint(['disc', 'world'])
        self.assertEqual(remaining, ['disc', 'world'])
        self.assertIsNone(disc_number)


class SearchWithDiscLabelFieldTest(unittest.TestCase):
    def test_disc_subtitle_query_matches_via_disc_label_field(self):
        library = [
            _track('/a/1.mp3', 'Ep 1', 'John Finnemore', "John Finnemore's Souvenir Programme",
                   disc='1', total_discs='2', disc_subtitle='Series 1'),
            _track('/a/2.mp3', 'Ep 2', 'John Finnemore', "John Finnemore's Souvenir Programme",
                   disc='2', total_discs='2', disc_subtitle='Series 2'),
        ]
        results = search.search(library, "souvenir programme series 1",
                                fields=['title', 'artist', 'album', 'disc_label'])
        paths = {r.song['path'] for r in results}
        self.assertEqual(paths, {'/a/1.mp3'})

    def test_plain_disc_number_query_matches_with_no_subtitle(self):
        library = [
            _track('/a/1.mp3', 'Ep 1', 'Some Artist', 'Boxset', disc='1', total_discs='3'),
            _track('/a/2.mp3', 'Ep 2', 'Some Artist', 'Boxset', disc='3', total_discs='3'),
        ]
        results = search.search(library, "boxset disc 3",
                                fields=['title', 'artist', 'album', 'disc_label'])
        paths = {r.song['path'] for r in results}
        self.assertEqual(paths, {'/a/2.mp3'})

    def test_plain_disc_number_query_also_matches_a_named_disc(self):
        """Regression: "jfsp disc 2" found nothing, because a disc with a
        named subtitle only exposed "Series 2", never the literal word "disc"."""
        library = [
            _track('/a/1.mp3', 'Ep 1', 'JFSP', "John Finnemore's Souvenir Programme",
                   disc='1', total_discs='2', disc_subtitle='Series 1'),
            _track('/a/2.mp3', 'Ep 2', 'JFSP', "John Finnemore's Souvenir Programme",
                   disc='2', total_discs='2', disc_subtitle='Series 2'),
        ]
        fields = ['title', 'artist', 'album', 'disc_label']
        for query in ("jfsp disc 2", "jfsp series 2"):
            with self.subTest(query=query):
                paths = {r.song['path'] for r in search.search(library, query, fields=fields)}
                self.assertEqual(paths, {'/a/2.mp3'})

    def test_disc_number_and_series_number_disagreeing_does_not_cross_match(self):
        """Regression: a series can be renumbered relative to its disc
        ordinal (disc 5 tagged as "Series 4"). "disc 4" must not match the
        disc-5-Series-4 track just because its subtitle contains a 4, and
        "series 4" must not match the disc-4-Series-3 track just because its
        disc number happens to be 4."""
        library = [
            _track('/d4', 'Ep', 'JFSP', 'JFSP', disc='4', total_discs='10', disc_subtitle='Series 3'),
            _track('/d5', 'Ep', 'JFSP', 'JFSP', disc='5', total_discs='10', disc_subtitle='Series 4'),
        ]
        fields = ['title', 'artist', 'album', 'disc_label']
        cases = {
            "jfsp disc 4": {'/d4'},
            "jfsp disc 5": {'/d5'},
            "jfsp series 3": {'/d4'},
            "jfsp series 4": {'/d5'},
            "jfsp disc 6": set(),
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                paths = {r.song['path'] for r in search.search(library, query, fields=fields)}
                self.assertEqual(paths, expected)


class CollectDiscEntitiesTest(unittest.TestCase):
    def _search_and_collect(self, library, query):
        fields = ['title', 'artist', 'album', 'disc_label']
        results = search.search(library, query, fields=fields)
        tokens = search.tokenize(query)
        return search.collect_disc_entities(results, tokens)

    def test_isolates_just_that_disc(self):
        library = [
            _track('/a/1.mp3', 'Ep 1', 'John Finnemore', "John Finnemore's Souvenir Programme",
                   disc='1', total_discs='2', disc_subtitle='Series 1'),
            _track('/a/2.mp3', 'Ep 2', 'John Finnemore', "John Finnemore's Souvenir Programme",
                   disc='1', total_discs='2', disc_subtitle='Series 1'),
            _track('/a/3.mp3', 'Ep 1', 'John Finnemore', "John Finnemore's Souvenir Programme",
                   disc='2', total_discs='2', disc_subtitle='Series 2'),
        ]
        ents = self._search_and_collect(library, "john finnemore's souvenir programme series 1")
        self.assertEqual(len(ents), 1)
        self.assertEqual({t['path'] for t in ents[0].tracks}, {'/a/1.mp3', '/a/2.mp3'})
        self.assertIn('Series 1', ents[0].name)

    def test_isolates_a_named_disc_by_plain_number_too(self):
        library = [
            _track('/a/1.mp3', 'Ep 1', 'JFSP', "John Finnemore's Souvenir Programme",
                   disc='1', total_discs='2', disc_subtitle='Series 1'),
            _track('/a/2.mp3', 'Ep 2', 'JFSP', "John Finnemore's Souvenir Programme",
                   disc='2', total_discs='2', disc_subtitle='Series 2'),
        ]
        ents = self._search_and_collect(library, "jfsp disc 2")
        self.assertEqual(len(ents), 1)
        self.assertEqual({t['path'] for t in ents[0].tracks}, {'/a/2.mp3'})

    def test_disc_and_series_numbers_disagreeing_still_isolate_correctly(self):
        library = [
            _track('/d4', 'Ep', 'JFSP', 'JFSP', disc='4', total_discs='10', disc_subtitle='Series 3'),
            _track('/d5', 'Ep', 'JFSP', 'JFSP', disc='5', total_discs='10', disc_subtitle='Series 4'),
        ]
        for query, expected_path in (("jfsp disc 4", '/d4'), ("jfsp disc 5", '/d5'),
                                     ("jfsp series 3", '/d4'), ("jfsp series 4", '/d5')):
            with self.subTest(query=query):
                ents = self._search_and_collect(library, query)
                self.assertEqual(len(ents), 1)
                self.assertEqual({t['path'] for t in ents[0].tracks}, {expected_path})

    def test_same_disc_label_different_albums_stay_distinct(self):
        library = [
            _track('/a/1.mp3', 'Ep', 'Show A', 'Show A Boxset', disc='1', total_discs='2', disc_subtitle='Series 1'),
            _track('/b/1.mp3', 'Ep', 'Show B', 'Show B Boxset', disc='1', total_discs='2', disc_subtitle='Series 1'),
        ]
        ents = self._search_and_collect(library, "series 1")
        # Both match "series 1" alone (no album token to disambiguate), but
        # must remain two separate entities, not merged into one "Series 1".
        self.assertEqual(len(ents), 2)
        names = {e.name for e in ents}
        self.assertEqual(len(names), 2)

    def test_single_disc_album_produces_no_disc_entity(self):
        library = [_track('/a/1.mp3', 'Ep', 'Artist', 'Album', disc='1', total_discs='1')]
        ents = self._search_and_collect(library, "album")
        self.assertEqual(ents, [])

    def test_no_tokens_returns_nothing(self):
        self.assertEqual(search.collect_disc_entities([], []), [])


if __name__ == "__main__":
    unittest.main()
