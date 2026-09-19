"""Tests for src/search.py's cross-field matching: different tokens of one
query can each match a different field of the same track, and a connector
word ("by", "feat"...) doesn't sink an otherwise-good result."""
import unittest

from src import search


def _track(path, title, artist, album=''):
    return {'path': path, 'title': title, 'artist': artist, 'album': album}


class CrossFieldMatchTest(unittest.TestCase):
    def setUp(self):
        self.library = [
            _track('/1', 'Hungry Like the Wolf', 'Duran Duran', 'Rio'),
            _track('/2', 'Sandstorm', 'Darude', 'Before the Storm'),
            _track('/3', 'Stand By Me', 'Ben E. King', "Don't Play That Song"),
        ]
        self.fields = ['title', 'artist', 'album']

    def _paths(self, query):
        return [r.song['path'] for r in search.search(self.library, query, fields=self.fields)]

    def test_title_and_artist_words_together(self):
        self.assertEqual(self._paths("Hungry Duran"), ['/1'])

    def test_artist_first_then_title(self):
        self.assertEqual(self._paths("Darude Sandstorm"), ['/2'])

    def test_connector_word_by_does_not_reject_the_match(self):
        self.assertEqual(self._paths("Hungry Like The Wolf by Duran Duran"), ['/1'])

    def test_connector_word_that_is_also_real_content_still_matches_normally(self):
        # "by" is a genuine substring of "Stand By Me" — it should still
        # contribute to the match rather than being silently dropped.
        self.assertEqual(self._paths("stand by me"), ['/3'])

    def test_bare_connector_word_matching_something_finds_only_that(self):
        self.assertEqual(self._paths("by"), ['/3'])

    def test_bare_connector_word_matching_nothing_finds_nothing(self):
        """A query that's entirely connector words and matches no track for
        real must not fall back to "everything matches"."""
        self.assertEqual(self._paths("vs"), [])

    def test_feat_connector_does_not_reject(self):
        library = [_track('/a', 'Waiting All Night', 'Rudimental feat Ella Eyre')]
        self.assertEqual(
            [r.song['path'] for r in search.search(library, "Waiting All Night feat Ella Eyre",
                                                    fields=self.fields)],
            ['/a'])


if __name__ == "__main__":
    unittest.main()
