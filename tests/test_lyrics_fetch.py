"""Lyrics from LRCLIB: the exact lookup, else a search by title and main
artist matched by length; synced lines when there are some."""
import json
import unittest
from unittest import mock

from backtrack.lyrics import online


def _answers(by_endpoint):
    def fetch(url):
        endpoint = url.split('/api/')[1].split('?')[0]
        answer = by_endpoint[endpoint]
        if isinstance(answer, Exception):
            raise answer
        return json.dumps(answer).encode()
    return fetch


class FetchTest(unittest.TestCase):
    def test_search_after_a_miss_takes_the_nearest_length_synced_first(self):
        hits = [{'duration': 250.0, 'plainLyrics': "plain"},
                {'duration': 252.0, 'syncedLyrics': "[00:01.00]Look at me"},
                {'duration': 300.0, 'syncedLyrics': "[00:01.00]Another song"}]
        fetch = _answers({'get': OSError("404 Not Found"), 'search': hits})
        with mock.patch.object(online, 'fetch', fetch):
            record = online.find("Misty", "Jeff Goldblum Feat. Melody Gardot", "Night Blooms", 251)
        self.assertEqual(online.rows(record), [("Look at me", 1000)])

    def test_plain_and_instrumental(self):
        self.assertEqual(online.rows({'plainLyrics': "One\n\nTwo"}), [("One", None), ("Two", None)])
        self.assertEqual(online.rows({'instrumental': True, 'plainLyrics': "x"}), [])
        self.assertEqual(online.rows(None), [])

    def test_a_server_error_is_asked_again(self):
        calls = []
        def fetch(url):
            calls.append(url)
            if len(calls) == 1:
                raise OSError("503 Service Unavailable")
            return b'{"syncedLyrics": "[00:02.00]Hi"}'
        with mock.patch.object(online, 'fetch', fetch), mock.patch.object(online.time, 'sleep', lambda s: None):
            self.assertEqual(online.rows(online.find("T", "A", "B", 100)), [("Hi", 2000)])


if __name__ == '__main__':
    unittest.main()
