"""Headless tests for src/feed.py.

The title cases come from a real feed — `tests/fixtures/friday_night_comedy.rss`
is a trimmed copy of BBC Radio 4's *Friday Night Comedy*, kept because its
titles were written by different people over ten years and disagree with each
other in every way a title can. No test here touches the network.
"""
import os
import unittest
from datetime import datetime, timezone

from src import feed as fd

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures',
                       'friday_night_comedy.rss')


def _when(text: str):
    """An RFC-822 date as the parser would hand it over."""
    from email.utils import parsedate_to_datetime
    return parsedate_to_datetime(text)


class WhitespaceTest(unittest.TestCase):
    def test_a_literal_tab_becomes_a_space(self):
        self.assertEqual(fd.normalise_whitespace("Ep1.\tKeir vs Kemi"),
                         "Ep1. Keir vs Kemi")

    def test_a_non_breaking_space_becomes_a_space(self):
        self.assertEqual(fd.normalise_whitespace("Dead Ringers"),
                         "Dead Ringers")

    def test_runs_of_space_collapse_and_the_ends_are_trimmed(self):
        self.assertEqual(fd.normalise_whitespace("  a   b \n c  "), "a b c")

    def test_a_zero_width_space_is_dropped_entirely(self):
        self.assertEqual(fd.normalise_whitespace("a​b"), "ab")

    def test_empty_stays_empty(self):
        self.assertEqual(fd.normalise_whitespace(''), '')


class TitleParsingTest(unittest.TestCase):
    """One case per shape the real feed uses."""

    def test_colon_and_a_space_after_ep(self):
        got = fd.parse_title("The News Quiz: Ep 2. Team Vibes vs Team Bribes")
        self.assertEqual(got.show, "The News Quiz")
        self.assertEqual(got.episode, 2)
        self.assertEqual(got.title, "Team Vibes vs Team Bribes")

    def test_no_colon_and_no_space_after_ep(self):
        got = fd.parse_title("The News Quiz Ep5. Starmer psychodrama")
        self.assertEqual(got.show, "The News Quiz")
        self.assertEqual(got.episode, 5)
        self.assertEqual(got.title, "Starmer psychodrama")

    def test_a_tab_between_the_number_and_the_title(self):
        got = fd.parse_title("Dead Ringers: Ep1.\tKeir vs Kemi")
        self.assertEqual(got.show, "Dead Ringers")
        self.assertEqual(got.episode, 1)
        self.assertEqual(got.title, "Keir vs Kemi")

    def test_a_show_name_containing_a_semicolon_survives(self):
        got = fd.parse_title("Too Long; Didn't Read: Ep 6. Computer says no")
        self.assertEqual(got.show, "Too Long; Didn't Read")
        self.assertEqual(got.episode, 6)

    def test_an_en_dash_separator(self):
        got = fd.parse_title("The News Quiz – Ep 4. Conference")
        self.assertEqual(got.show, "The News Quiz")
        self.assertEqual(got.episode, 4)

    def test_a_bare_number_where_another_feed_writes_ep(self):
        got = fd.parse_title("Catherine Bohart: TL;DR - 6. Ghosts in the machine")
        self.assertEqual(got.episode, 6)
        self.assertEqual(got.title, "Ghosts in the machine")

    def test_ep_with_a_full_stop_after_it(self):
        got = fd.parse_title("A Show: Ep. 12. The twelfth")
        self.assertEqual(got.episode, 12)
        self.assertEqual(got.title, "The twelfth")

    def test_a_series_number_anywhere_is_picked_up(self):
        got = fd.parse_title("A Show Series 3: Ep 2. Something")
        self.assertEqual(got.series, 3)
        self.assertEqual(got.episode, 2)

    def test_nothing_structured_leaves_the_title_whole(self):
        got = fd.parse_title("Best of The News Quiz 2025")
        self.assertEqual(got.title, "Best of The News Quiz 2025")
        self.assertEqual(got.show, '')
        self.assertIsNone(got.episode)

    def test_the_raw_title_is_always_kept(self):
        raw = "Dead Ringers: Ep1.\tKeir vs Kemi"
        self.assertEqual(fd.parse_title(raw).raw, raw)


class TitleDateTest(unittest.TestCase):
    def test_a_full_date_in_the_title_wins(self):
        got = fd.parse_title("News Quiz 25th February 2022",
                             _when("Fri, 01 Mar 2022 18:00:00 +0000"))
        self.assertEqual(got.date, "2022-02-25")
        self.assertEqual(got.date_source, 'title')
        self.assertEqual(got.show, "News Quiz")

    def test_a_dateless_year_takes_the_year_from_the_feed(self):
        got = fd.parse_title("Dead Ringers - 31st May",
                             _when("Fri, 07 Jun 2024 18:00:00 +0000"))
        self.assertEqual(got.date, "2024-05-31")
        self.assertEqual(got.date_source, 'title+feed')

    def test_with_no_date_in_the_title_the_feed_date_is_used_and_said_so(self):
        got = fd.parse_title("A Show: Ep 1. Hello",
                             _when("Fri, 07 Jun 2024 18:00:00 +0000"))
        self.assertEqual(got.date, "2024-06-07")
        self.assertEqual(got.date_source, 'feed')

    def test_with_no_dates_at_all_there_is_no_date(self):
        got = fd.parse_title("A Show: Ep 1. Hello")
        self.assertEqual((got.date, got.date_source), ('', ''))

    def test_the_date_becomes_the_episode_title(self):
        # Otherwise the show name appears twice in a track row.
        got = fd.parse_title("Dead Ringers - 31st May",
                             _when("Fri, 07 Jun 2024 18:00:00 +0000"))
        self.assertEqual(got.title, "31st May")

    def test_a_short_month_name_is_understood(self):
        got = fd.parse_title("A Show - 3rd Feb 2020")
        self.assertEqual(got.date, "2020-02-03")

    def test_a_month_that_is_not_a_month_is_not_a_date(self):
        got = fd.parse_title("A Show - 3rd Smarch 2020")
        self.assertEqual(got.date, '')
        self.assertEqual(got.title, "A Show - 3rd Smarch 2020")


class FeedParsingTest(unittest.TestCase):
    def setUp(self):
        self.feed = fd.parse_feed(FIXTURE)

    def test_the_channel_is_read(self):
        self.assertEqual(self.feed.title,
                         "Friday Night Comedy from BBC Radio 4")

    def test_every_item_is_read(self):
        self.assertEqual(len(self.feed.items), 10)

    def test_each_item_has_an_enclosure_and_a_guid(self):
        for item in self.feed.items:
            self.assertTrue(item.url)
            self.assertTrue(item.guid)

    def test_the_real_shapes_all_parse(self):
        by_raw = {fd.normalise_whitespace(i.parsed.raw): i.parsed
                  for i in self.feed.items}
        self.assertEqual(
            by_raw["The News Quiz: Ep 2. Team Vibes vs Team Bribes"].episode, 2)
        self.assertEqual(by_raw["Dead Ringers: Ep1. Keir vs Kemi"].show,
                         "Dead Ringers")
        self.assertEqual(by_raw["Dead Ringers - 31st May"].date_source,
                         'title+feed')
        self.assertEqual(by_raw["News Quiz 25th February 2022"].date,
                         "2022-02-25")

    def test_a_feed_with_no_channel_is_rejected(self):
        with self.assertRaises(ValueError):
            fd.parse_feed(b"<rss version='2.0'></rss>")

    def test_something_that_is_not_xml_is_rejected(self):
        with self.assertRaises(ValueError):
            fd.parse_feed(b"<html><body>404 Not Found</body></html>")

    def test_an_item_with_no_enclosure_still_parses(self):
        feed = fd.parse_feed(
            b"<rss version='2.0'><channel><title>T</title>"
            b"<item><title>No audio</title></item></channel></rss>")
        self.assertEqual(feed.items[0].url, '')
        self.assertEqual(feed.items[0].parsed.title, 'No audio')


class IdentityTest(unittest.TestCase):
    def test_the_guid_is_the_key_when_there_is_one(self):
        item = fd.Item(guid='urn:x:1', url='http://example.invalid/a.mp3')
        self.assertEqual(item.key, 'urn:x:1')

    def test_the_enclosure_url_is_the_key_when_there_is_no_guid(self):
        item = fd.Item(guid='', url='http://example.invalid/a.mp3')
        self.assertEqual(item.key, 'http://example.invalid/a.mp3')

    def test_an_item_with_neither_has_no_key(self):
        self.assertEqual(fd.Item().key, '')

    def test_the_fixture_has_no_duplicate_keys(self):
        keys = [i.key for i in fd.parse_feed(FIXTURE).items]
        self.assertEqual(len(keys), len(set(keys)))


class FilterTest(unittest.TestCase):
    def setUp(self):
        self.items = fd.parse_feed(FIXTURE).items

    def test_an_empty_filter_keeps_everything(self):
        self.assertEqual(len(fd.matching(self.items, '')), len(self.items))

    def test_a_substring_narrows_it(self):
        got = fd.matching(self.items, 'Dead Ringers')
        self.assertTrue(got)
        self.assertTrue(all('dead ringers' in i.parsed.raw.casefold()
                            for i in got))

    def test_the_filter_ignores_case(self):
        self.assertEqual(len(fd.matching(self.items, 'dead ringers')),
                         len(fd.matching(self.items, 'DEAD RINGERS')))

    def test_the_filter_matches_the_raw_title_not_the_parsed_one(self):
        # "Dead Ringers" is the show prefix, which parsing removes from `title`.
        self.assertTrue(fd.matching(self.items, 'Dead Ringers'))

    def test_a_filter_that_matches_nothing_gives_nothing(self):
        self.assertEqual(fd.matching(self.items, 'zzzznothing'), [])


class NamingTest(unittest.TestCase):
    def test_a_slug_is_lowercase_and_hyphenated(self):
        self.assertEqual(fd.slugify("Friday Night Comedy from BBC Radio 4!"),
                         "friday-night-comedy-from-bbc-radio-4")

    def test_a_slug_is_never_empty(self):
        self.assertEqual(fd.slugify("!!!"), "feed")

    def test_illegal_path_characters_are_removed(self):
        self.assertEqual(fd.safe_name('A/B: "C"?'), 'AB C')

    def test_a_name_is_never_empty(self):
        self.assertEqual(fd.safe_name('   '), 'untitled')

    def test_the_extension_comes_from_the_url_when_it_can(self):
        item = fd.Item(url='http://example.invalid/x/ep.m4a')
        self.assertEqual(fd.extension_for(item), '.m4a')

    def test_the_extension_falls_back_to_the_mime_type(self):
        item = fd.Item(url='http://example.invalid/redir/vpid/abc',
                       mime='audio/mp4')
        self.assertEqual(fd.extension_for(item), '.m4a')

    def test_the_extension_falls_back_to_mp3(self):
        item = fd.Item(url='http://example.invalid/redir/vpid/abc')
        self.assertEqual(fd.extension_for(item), '.mp3')

    def test_the_path_is_show_then_episode_and_title(self):
        item = fd.Item(url='http://example.invalid/a.mp3',
                       parsed=fd.parse_title("A Show: Ep 4. The Fourth"))
        self.assertEqual(fd.target_path(item, '/music'),
                         os.path.join('/music', 'A Show', '04 - The Fourth.mp3'))

    def test_the_path_falls_back_to_the_feed_title_for_the_folder(self):
        item = fd.Item(url='http://example.invalid/a.mp3',
                       parsed=fd.parse_title("Just A Title"))
        self.assertEqual(fd.target_path(item, '/music', 'My Feed'),
                         os.path.join('/music', 'My Feed', 'Just A Title.mp3'))


class TagPlanTest(unittest.TestCase):
    def test_the_raw_title_is_kept_in_a_comment(self):
        raw = "A Show: Ep 4.\tThe Fourth"
        _fields, frames = fd.tag_plan(
            fd.Item(parsed=fd.parse_title(raw)), 'My Feed')
        self.assertEqual(frames['COMM::eng'], raw)

    def test_the_parsed_fields_become_tags(self):
        fields, _frames = fd.tag_plan(
            fd.Item(parsed=fd.parse_title("A Show Series 2: Ep 4. Fourth",
                                          _when("Fri, 07 Jun 2024 18:00:00 +0000"))),
            'My Feed')
        self.assertEqual(fields['title'], 'Fourth')
        self.assertEqual(fields['album'], 'A Show')
        self.assertEqual(fields['track'], '4')
        self.assertEqual(fields['disc'], '2')
        self.assertEqual(fields['album_artist'], 'My Feed')

    def test_the_full_date_goes_in_the_frame_and_the_year_in_the_field(self):
        fields, frames = fd.tag_plan(
            fd.Item(parsed=fd.parse_title(
                "A Show - 31st May", _when("Fri, 07 Jun 2024 18:00:00 +0000"))))
        self.assertEqual(fields['year'], '2024')
        self.assertEqual(frames['TDRC'], '2024-05-31')

    def test_every_episode_is_marked_as_a_podcast(self):
        _fields, frames = fd.tag_plan(fd.Item(parsed=fd.parse_title("X")))
        self.assertEqual(frames['TCON'], 'Podcast')

    def test_no_empty_values_are_written(self):
        fields, frames = fd.tag_plan(fd.Item(parsed=fd.parse_title("X")))
        self.assertTrue(all(fields.values()))
        self.assertTrue(all(frames.values()))


class StateTest(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        from pathlib import Path

        from src import config as cfg
        self.tmp = tempfile.mkdtemp()
        self._saved = cfg.CONFIG_DIR
        cfg.CONFIG_DIR = Path(self.tmp)
        self.addCleanup(lambda: setattr(cfg, 'CONFIG_DIR', self._saved))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_an_absent_store_reads_as_empty(self):
        self.assertEqual(fd.load_feeds(), {})

    def test_a_saved_store_reads_back(self):
        fd.save_feeds({'x': {'url': 'u', 'seen': ['a']}})
        self.assertEqual(fd.load_feeds()['x']['seen'], ['a'])

    def test_a_corrupt_store_reads_as_empty_rather_than_raising(self):
        with open(os.path.join(self.tmp, 'feeds.json'), 'w') as handle:
            handle.write('{not json')
        self.assertEqual(fd.load_feeds(), {})


if __name__ == "__main__":
    unittest.main()
