"""Word times derived from a transcription's clock alone (tools/align_script.py).

The property these guard is the one the whole approach rests on: error is bounded
by the distance between measurements and never accumulates. A test cannot show
"no drift" directly, so it pins the three ways the drift-free property was
actually lost in practice — a swallowed silence, a guess treated as a
measurement, and a transcription whose own segments overlap.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import align_script as A


def script(text):
    """One line of script as `script_stream` would yield its words."""
    return [(w, 0) for w in text.split()]


def anchor(start, end, text):
    return {"start": start, "end": end, "text": text}


class AnchorTimes(unittest.TestCase):

    def test_every_word_gets_a_time_in_order(self):
        words = script("one two three four five six")
        anchors = [anchor(0.0, 2.0, "one two three"), anchor(2.0, 4.0, "four five six")]
        times, blocks = A.times_from_anchors(words, anchors)
        self.assertEqual(len(times), len(words))
        self.assertEqual(len(blocks), 2)
        for i in range(len(words) - 1):
            self.assertLessEqual(times[i][0], times[i][1])
            self.assertLessEqual(times[i][1], times[i + 1][0] + 1e-9)

    def test_a_measured_start_is_used_exactly(self):
        """A word the transcription heard is placed where it said, not near it."""
        words = script("alpha bravo charlie delta")
        anchors = [anchor(0.0, 1.0, "alpha bravo"), anchor(9.0, 10.0, "charlie delta")]
        times, _ = A.times_from_anchors(words, anchors)
        self.assertAlmostEqual(times[0][0], 0.0, places=3)
        self.assertAlmostEqual(times[2][0], 9.0, places=3)

    def test_silence_between_segments_is_not_swallowed(self):
        """The pause before a line belongs to neither line.

        Modelling time as one ruler over word boundaries gives boundary 2 a single
        value, so the second line starts where the first stopped and every pause in
        the episode disappears — which pulls every line early by the length of the
        pause before it.
        """
        words = script("hello there goodbye now")
        anchors = [anchor(0.0, 1.0, "hello there"), anchor(5.0, 6.0, "goodbye now")]
        times, _ = A.times_from_anchors(words, anchors)
        self.assertAlmostEqual(times[1][1], 1.0, places=3)     # first line ends
        self.assertAlmostEqual(times[2][0], 5.0, places=3)     # second starts late

    def test_unheard_words_are_not_stretched_across_the_silence(self):
        """A word the transcription missed keeps a plausible duration.

        Spreading it over the whole gap is what put a missed "Oh!" five seconds
        before the line it opens: a missed word is a fraction of a second whether
        the pause around it is one second or six.
        """
        words = script("heard oh heard2")
        anchors = [anchor(0.0, 1.0, "heard"), anchor(9.0, 10.0, "heard2")]
        times, _ = A.times_from_anchors(words, anchors)
        self.assertLess(times[1][1] - times[1][0], 2.0,
                        "an unheard word was stretched over the pause")

    def test_a_gap_spanning_a_line_break_is_split_at_it(self):
        """Two lines in one gap are two pieces of speech with a pause between.

        The tail of the line that ran on belongs at the start of the gap, where
        that speech stopped; the head of the next belongs at the end, running into
        the speech that was heard. Placed as one run they drag each other — which
        is what put "This week, Douz!" and "Here you are" both in the theme music.
        """
        words = [("aa", 0), ("bb", 0), ("tail", 0), ("head", 1), ("cc", 1), ("dd", 1)]
        anchors = [anchor(0.0, 1.0, "aa bb"), anchor(9.0, 10.0, "cc dd")]
        times, _ = A.times_from_anchors(words, anchors)
        self.assertAlmostEqual(times[2][0], 1.0, places=2)      # tail: butts to t0
        self.assertGreater(times[3][0], 7.5)                    # head: butts to t1
        self.assertLessEqual(times[3][1], 9.0 + 1e-9)

    def test_a_gap_uses_speech_the_transcription_could_not_name(self):
        """A segment the script never claimed still says something was said then.

        MacWhisper heard "He" and "was" where the script has "Here you are"; the
        words belong at that sound, not spread across the theme music before it.
        """
        spoken = [(w, 0) for w in "alpha beta gamma delta".split()]
        words = spoken + [("here", 1), ("you", 1), ("are", 1), ("omega", 2)]
        anchors = [anchor(0.0, 2.0, "alpha beta gamma delta"),
                   anchor(8.0, 8.6, "he was"),          # misheard, claimed by nobody
                   anchor(9.0, 10.0, "omega")]
        times, _ = A.times_from_anchors(words, anchors)
        self.assertGreater(times[4][0], 7.0, "placed before the speech that was heard")
        self.assertLess(times[4][0], 9.0)

    def test_a_one_for_one_mishearing_still_gives_a_time(self):
        """`creef` for Crieff is the same instant, however it was spelled.

        We never need to know which word was said — the script owns that — only
        when. A token-for-token disagreement is therefore a usable clock reading,
        and throwing it away costs a measurement for nothing.
        """
        words = script("hello creef here")
        anchors = [anchor(0.0, 1.0, "hello"), anchor(4.0, 5.0, "crieff"),
                   anchor(5.0, 6.0, "here")]
        times, blocks = A.times_from_anchors(words, anchors)
        self.assertEqual(len(blocks), 3, "a 1:1 mishearing was discarded")
        self.assertAlmostEqual(times[1][0], 4.0, places=3)

    def test_a_word_the_transcription_never_heard_is_not_a_measurement(self):
        """Only words the two streams agree on may carry a time.

        `pair_tokens(placement=True)` parks an unmatched word against whichever
        segment it fell nearest. Reading a clock off that is how a whole missed
        line lands inside the next segment: here the four unheard words would all
        be crushed into the 1s of "tail", half a minute from where they belong.
        """
        words = script("intro " + " ".join(f"missed{i}" for i in range(4)) + " tail")
        anchors = [anchor(0.0, 1.0, "intro"), anchor(9.0, 10.0, "tail")]
        times, blocks = A.times_from_anchors(words, anchors)
        self.assertEqual(len(blocks), 2, "an unheard run became its own measurement")
        for i in range(1, 5):
            self.assertGreaterEqual(times[i][0], 1.0)
            self.assertLessEqual(times[i][1], 9.0 + 1e-9)

    def test_overlapping_transcription_segments_stay_in_order(self):
        """About 6% of MacWhisper's segments start before the previous one ended."""
        words = script("aa bb cc dd ee ff")
        anchors = [anchor(0.0, 3.0, "aa bb cc"), anchor(2.0, 4.0, "dd ee ff")]
        times, _ = A.times_from_anchors(words, anchors)
        self.assertEqual(len(times), len(words))
        for i in range(len(words) - 1):
            self.assertLessEqual(times[i][1], times[i + 1][0] + 1e-9)

    def test_no_anchors_at_all_reports_nothing_rather_than_guessing(self):
        times, blocks = A.times_from_anchors(script("a b c"), [])
        self.assertEqual((times, blocks), ({}, []))


if __name__ == "__main__":
    unittest.main()
