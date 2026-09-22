"""Tests for the MD script → timed-transcript overlay (src/lyrics/md_overlay.py,
src/lyrics/lyrics_text.py, `_parse_markdown_dialogue`).

The fixtures are the shapes the Cabin Pressure transcripts actually use and that
the parser used to get wrong: a sarcasm marker "(!)", a lettered list "(a)", a
nested bracket inside a standalone direction, a colon inside one, two directions
on one speaker header, and a repeated direction on a line the transcript kept
whole.
"""
import os
import tempfile
import unittest

from src.lyrics import lyrics_text as lt
from src.lyrics.lyrics import _parse_markdown_dialogue, _chunks_from_segments
from src.lyrics.lyrics_editor import _split_candidates, _split_seg_at
from src.lyrics.md_overlay import build_md_overlay


def _segs(*lines):
    """One segment per line of spoken words, timed back to back at 0.4s a word."""
    out, t = [], 0.0
    for text in lines:
        ws = text.split()
        out.append({'text': text, 'start': t, 'end': t + len(ws) * 0.4,
                    'words': [{'word': w, 'start': t + i * 0.4, 'end': t + i * 0.4 + 0.4}
                              for i, w in enumerate(ws)]})
        t += len(ws) * 0.4
    return out


def _md(text):
    path = os.path.join(tempfile.mkdtemp(), 'script.md')
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)
    return path


class SpokenTextTest(unittest.TestCase):
    def test_sarcasm_marker_is_spoken_punctuation_not_a_direction(self):
        # "(!)" has no letters, so it stays on the line and raises no ✦ overlay.
        self.assertEqual(lt.inline_stage_dirs("Oh, well done(!)"), [])
        self.assertEqual(lt.spoken_text("Oh, well done(!)"), "Oh, well done(!)")

    def test_sarcasm_marker_does_not_eat_the_emphasis_that_closes_before_it(self):
        self.assertEqual(lt.spoken_text("Of *course*(!) And the walk"),
                         "Of *course*(!) And the walk")

    def test_lettered_list_stays_in_the_word_stream(self):
        self.assertEqual(lt.inline_stage_dirs("Douglas. (a) Shut up; (b) go."), [])
        self.assertIn('a', lt.norm_words(lt.spoken_text("Douglas. (a) Shut up.")))

    def test_bare_parenthetical_with_words_is_still_a_direction(self):
        self.assertEqual(lt.inline_stage_dirs("for the BBC! (In Spanish accent)"),
                         [(3, 'In Spanish accent')])

    def test_direction_offsets_index_the_spoken_words(self):
        # The offset a direction reports must be an index into spoken_text's words,
        # or the overlay anchors it against the wrong segment boundary.
        for line in ("I *can*. Doing it now! *(Long pause) Wow!*",
                     "Thank you. *(Into radio)* Fitton Approach.",
                     "Oh, well done(!) *(he sighs)* Fine."):
            words = lt.spoken_text(line).split()
            for pos, _d in lt.inline_stage_dirs(line):
                self.assertLessEqual(pos, len(words), line)

    def test_okina_folds_like_an_apostrophe(self):
        self.assertEqual(lt.norm("Molokaʻi"), lt.norm("Molokai"))
        self.assertEqual(lt.matchable("Molokaʻi"), "molokai")


class AlignTest(unittest.TestCase):
    """The script and the transcript spell the same speech differently. Three of
    those differences are conventions and must not read as discrepancies; anything
    else must."""

    def _tags(self, heard, script):
        js = [t for w in heard.split() for t in lt.norm_words(w)]
        jr = [w for w in heard.split() for _ in lt.norm_words(w)]
        md = [t for w in script.split() for t in lt.norm_words(w)]
        mr = [w for w in script.split() for _ in lt.norm_words(w)]
        return [op[0] for op in lt.align_tokens(js, md, jr, mr)]

    def test_word_boundaries_reconcile(self):
        self.assertEqual(self._tags("we takeoff now", "we take-off now"),
                         ['equal', 'boundary', 'equal'])
        self.assertEqual(self._tags("noone knew", "no-one knew"), ['boundary', 'equal'])

    def test_numbers_reconcile_by_value(self):
        for heard, script in (("25 feet", "twenty-five feet"),
                              ("7000 feet", "seven thousand feet"),
                              ("250 of them", "two hundred and fifty of them"),
                              ("we climb to 200", "we climb to two hundred")):
            self.assertIn('number', self._tags(heard, script), (heard, script))

    def test_a_countdown_is_not_a_sum(self):
        # five+four+three+two+one is fifteen only to a calculator.
        self.assertIsNone(lt._number_value(['five', 'four', 'three', 'two', 'one']))
        self.assertNotIn('number', self._tags("15", "five four three two one"))

    def test_a_time_is_not_a_sum(self):
        self.assertIsNone(lt._number_value(['eleven', 'thirty']))
        self.assertEqual(lt._number_value(['11']), 11)

    def test_oh_is_the_interjection_not_a_zero(self):
        self.assertIsNone(lt._number_value(['oh']))
        self.assertNotIn('number', self._tags("0 dear", "Oh, dear"))

    def test_elision_matches_but_is_flagged(self):
        for heard, script in (("because i said", "'cause I said"),
                              ("i have it", "I 'ave it"),
                              ("until then", "'til then"),
                              ("excuse me", "'scuse me")):
            self.assertIn('elision', self._tags(heard, script), (heard, script))

    def test_elision_needs_the_apostrophe_not_just_the_letters(self):
        # "is"/"his" and "at"/"that" are different words, not elisions.
        self.assertNotIn('elision', self._tags("his hat", "is hat"))
        self.assertNotIn('elision', self._tags("that one", "at one"))

    def test_a_real_difference_is_still_reported(self):
        self.assertIn('replace', self._tags("we go to paris", "we go to Bristol"))

    def test_a_convention_beside_a_real_difference_is_still_found(self):
        # difflib bundles them into one replacement; the gap is re-walked.
        tags = self._tags("we takeoff for paris", "we take-off for Bristol")
        self.assertIn('boundary', tags)
        self.assertIn('replace', tags)

    def test_a_long_hyphenated_chant_reconciles(self):
        self.assertIn('boundary', self._tags("nonononononono", "no-no-no-no-no-no-no"))

    def test_an_anchor_latched_onto_the_wrong_repeat_is_dissolved(self):
        # difflib locks the second "cream i i i ice cream" onto the first, stranding
        # the re-spelled copy either side of its choice. Nothing is left unmatched.
        tags = self._tags("ice cream iiiice cream i i i ice cream lovely",
                          "ice cream i-i-i-ice cream i-i-i-ice cream lovely")
        self.assertEqual(set(tags), {'equal', 'boundary'})

    def test_a_repeated_phrase_crowded_with_reconciliations_still_resolves(self):
        # The real shape from Yverdon: four near-identical "five point N" phrases,
        # some re-spelled as digits. difflib strands a run either side of the copy
        # it picks, and the stretch between the two halves is full of number
        # matches, so the region has to be re-walked whole rather than gap by gap.
        tags = self._tags(
            "nearly a six 5 point 9 well 5 point 8 nooo five point 9 say 5 point eight five yes",
            "nearly a six five point nine well five point eight no-o-o five point nine"
            " say five point eight five yes")
        self.assertEqual(set(tags), {'equal', 'number', 'boundary'})

    def test_a_repeated_phrase_with_digits_still_pairs_off(self):
        tags = self._tags("nearly a six 5 point 9 well 5 point 8 no five point nine yes",
                          "nearly a six five point nine well five point eight no five point nine yes")
        self.assertEqual(set(tags), {'equal', 'number'})


class ParseTest(unittest.TestCase):
    def test_nested_bracket_in_a_standalone_direction(self):
        line = "*(A ringtone sounds ('Questa o quella' from Verdi's Rigoletto), then a beep.)*"
        dl = _parse_markdown_dialogue(line)
        self.assertEqual(len(dl), 1)
        self.assertTrue(dl[0].is_stage_direction())
        self.assertTrue(dl[0].stage_dir.endswith("then a beep."))

    def test_colon_inside_a_standalone_direction_is_not_a_speaker_header(self):
        dl = _parse_markdown_dialogue("*(Immediately: bing bong, bing bong.)*")
        self.assertTrue(dl[0].is_stage_direction())
        self.assertEqual(dl[0].text, "")

    def test_bracketed_note_line_is_a_direction_not_dialogue(self):
        dl = _parse_markdown_dialogue("*[Transcriber's note: he lisps throughout.]*")
        self.assertTrue(dl[0].is_stage_direction())

    def test_a_direction_must_be_its_own_emphasis_span(self):
        # *She pauses (softly)* used to swallow "She pauses" as part of the
        # direction. Only the bracket is a direction now; the words survive.
        line = "*Patek Philippe. (Normal voice)* Well, not a goodie."
        self.assertEqual([d for _pos, d in lt.inline_stage_dirs(line)], ['Normal voice'])
        spoken = lt.norm_words(lt.spoken_text(line))
        self.assertIn('philippe', spoken)          # the dialogue is not eaten
        self.assertNotIn('normal', spoken)         # the direction is not spoken

    def test_both_directions_on_a_speaker_header_survive(self):
        dl = _parse_markdown_dialogue("**DEROCHE** *(Swiss accent)* *(muffled)*: Come in.")
        self.assertEqual(dl[0].speakers, ['DEROCHE'])
        self.assertEqual(dl[0].stage_dir, "Swiss accent; muffled")
        self.assertEqual(dl[0].text, "Come in.")


class OverlayTest(unittest.TestCase):
    def test_no_duplicate_banner_for_a_direction_opening_its_own_line(self):
        path = _md("**CAROLYN**: Is it?\n"
                   "**DOUGLAS**: *(in a normal voice)* Not a goodie.\n")
        overlay, _q, _links = build_md_overlay(_segs("Is it?", "Not a goodie."), path)
        banners = [o['text'] for o in overlay if o['kind'] == 'speaker']
        self.assertEqual(banners, ['CAROLYN', 'DOUGLAS'])

    def test_repeated_direction_on_one_segment_is_not_stacked(self):
        path = _md("**BIRLING**: *(Ding)* Ding! *(Ding)* Ding! *(Ding)* Ding!\n"
                   "**ARTHUR**: Hello.\n")
        overlay, _q, _links = build_md_overlay(_segs("Ding! Ding! Ding!", "Hello."), path)
        for si in (0, 1):
            here = [o for o in overlay if o['kind'] == 'stage_dir' and o['before_si'] == si]
            self.assertLessEqual(len(here), 1, overlay)

    def test_pin_is_the_line_the_segment_spoke_most_of(self):
        # Whisper swallowed MARTIN's one-word line into DOUGLAS's: the segment is
        # pinned to the line it actually spoke, not merely the first one it touched.
        path = _md("**MARTIN**: Yes.\n**DOUGLAS**: No it is not at all like that.\n")
        segs = _segs("Yes. No it is not", "at all like that.")
        _ov, _q, links = build_md_overlay(segs, path)
        self.assertEqual(links[0], 1)
        self.assertEqual(links[1], 1)

    def test_repeated_words_are_not_scored_as_a_match(self):
        path = _md("**ARTHUR**: Three men went to mow went to mow a meadow.\n")
        _ov, quality, _l = build_md_overlay(_segs("Three men went to mow"), path)
        self.assertLess(quality[0]['score'], 0.8)

    def test_short_segment_absent_from_the_script_is_flagged(self):
        path = _md("**ARTHUR**: Hello there everyone.\n")
        _ov, quality, _l = build_md_overlay(_segs("Hello there everyone.", "Brilliant!"), path)
        self.assertEqual(quality[1]['score'], 0.0)


class PlacementTest(unittest.TestCase):
    """A word the transcript heard differently is still a word that was SAID at a
    known moment, so it belongs to the segment it was heard in — including when it
    opens one, where riding along with its neighbours would put it a segment early."""

    SCRIPT = "**SARGENT**: because 'e is relying on you to save 'im now\n"

    def _script_of(self, *heard):
        path = _md(self.SCRIPT)
        segs = _segs(*heard)
        _ov, quality, _l = build_md_overlay(segs, path)
        return [quality[si]['md_text'] for si in sorted(quality)]

    def test_mid_segment(self):
        self.assertEqual(self._script_of("because he is relying", "on you to save him now"),
                         ["because 'e is relying", "on you to save 'im now"])

    def test_on_the_segment_boundary(self):
        self.assertEqual(self._script_of("because he", "is relying on you to save him now"),
                         ["because 'e", "is relying on you to save 'im now"])

    def test_opening_a_segment(self):
        # "he" is the first word of segment 1, so "'e" belongs to segment 1 — not
        # backwards with "because" in segment 0.
        self.assertEqual(self._script_of("because", "he is relying on you to save him now"),
                         ["because", "'e is relying on you to save 'im now"])

    def test_a_disagreement_is_placed_but_never_counted_as_a_match(self):
        ops = [('replace', 0, 1, 0, 1)]
        self.assertEqual(lt.pair_tokens(ops), [])
        self.assertEqual(lt.pair_tokens(ops, placement=True), [(0, 0, 'replace')])


class SplitTest(unittest.TestCase):
    """A segment is cut wherever the script says a new beat starts — a new speaker
    part way through, or an inline stage direction."""

    def _split(self, script, *seg_texts):
        path = _md(script)
        segs = _segs(*seg_texts)
        cands, unplaced, suggested = _split_candidates(segs, path)
        for c in sorted(cands, key=lambda c: c['seg'], reverse=True):
            segs[c['seg']:c['seg'] + 1] = _split_seg_at(
                segs[c['seg']], c['boundaries'], c['line_refs'])
        return segs, unplaced, path, suggested

    def test_a_direction_mid_segment_cuts_it(self):
        segs, _u, _p, _g = self._split(
            "**ARTHUR**: Shush! *(Australian accent)* Yip! *(Normal voice)* Thank you.\n",
            "Shush! Yip! Thank you.")
        self.assertEqual([s['text'] for s in segs], ["Shush!", "Yip!", "Thank you."])

    def test_every_repeat_gets_its_own_beat_once_cut(self):
        # The point of cutting: four (Ding)s stop collapsing into one.
        script = "**BIRLING**: *(Ding)* Ding! *(Ding)* Ding! *(Ding)* Ding! *(Ding)* Ding!\n"
        segs, _u, path, _g = self._split(script, "Ding! Ding! Ding! Ding!")
        self.assertEqual(len(segs), 4)
        overlay, _q, _l = build_md_overlay(segs, path)
        dirs = [o for o in overlay if o['kind'] == 'stage_dir']
        self.assertEqual(len(dirs), 4)
        self.assertEqual({o['before_si'] for o in dirs}, {0, 1, 2, 3})

    def test_a_direction_that_opens_or_closes_a_line_is_not_a_cut(self):
        # Nothing to cut between: it already anchors at the segment boundary.
        segs, unplaced, _p, _g = self._split(
            "**MARTIN**: *(sighs)* Fine, we go to Bristol *(he trails off)*\n",
            "Fine, we go to Bristol")
        self.assertEqual(len(segs), 1)
        self.assertEqual(unplaced, [])

    def test_a_cut_steps_over_words_the_transcript_never_has(self):
        # '...' is a word of the script and none of the transcript, so the cut lands
        # on the next word that IS compared rather than being given up on.
        segs, unplaced, _p, _g = self._split(
            "**DOUGLAS**: Well ... *(he sighs)* ... we've got a problem.\n",
            "Well we've got a problem.")
        self.assertEqual([s['text'] for s in segs], ["Well", "we've got a problem."])
        self.assertEqual(unplaced, [])

    def test_a_direction_the_transcript_cannot_place_is_reported(self):
        # Whisper never heard "immediately" — the very word the direction stands
        # against — so there is no boundary where the script puts it. Reported, not
        # guessed at by snapping to a neighbour.
        _segsout, unplaced, _p, _g = self._split(
            "**MARTIN**: We go to Bristol. *(he sighs)* Immediately, all right?\n",
            "We go to Bristol. all right?")
        self.assertEqual([(ln, text) for ln, _at, text in unplaced], [(0, 'he sighs')])

    def test_splitting_is_idempotent(self):
        script = ("**ARTHUR**: Shush! *(Australian accent)* Yip!\n"
                  "**CAROLYN**: Arthur! You cannot be passengers!\n")
        segs, _u, path, _g = self._split(script, "Shush! Yip! Arthur! You cannot be passengers!")
        self.assertEqual(len(segs), 3)
        again, _u2, _g2 = _split_candidates(segs, path)
        self.assertEqual(again, [])

    def test_a_mid_sentence_direction_is_suggested_not_cut(self):
        # "Captain *(he assumes a French accent)* Martin duCref" — the script has not
        # finished a thought, so the bulk pass leaves it alone and says so.
        segs, _u, _p, suggested = self._split(
            "**DOUGLAS**: Captain *(he assumes a French accent)* Martin duCref here.\n",
            "Captain Martin duCref here.")
        self.assertEqual(len(segs), 1)
        self.assertEqual([t for _l, _w, t in suggested], ['he assumes a French accent'])

    def test_a_direction_after_punctuation_is_cut(self):
        for mark in ('.', '!', '?', ',', ';', '--', '...'):
            segs, _u, _p, suggested = self._split(
                f"**ARTHUR**: Shush{mark} *(Australian accent)* Yip!\n",
                f"Shush{mark} Yip!")
            self.assertEqual(len(segs), 2, mark)
            self.assertEqual(suggested, [], mark)

    def test_a_speaker_change_is_always_cut(self):
        # A new speaker is never mid-sentence, whatever the line before ended on.
        segs, _u, _p, _g = self._split(
            "**MARTIN**: \u266a Going nowhere \u266a\n**CAROLYN**: Arthur!\n",
            "Going nowhere Arthur!")
        self.assertEqual([s['text'] for s in segs], ["Going nowhere", "Arthur!"])

    def test_no_word_is_lost_or_duplicated_by_a_split(self):
        script = ("**ARTHUR**: Shush! *(Australian accent)* Yip! *(Normal voice)* Ta.\n"
                  "**CAROLYN**: Arthur!\n")
        before = "Shush! Yip! Ta. Arthur!"
        segs, _u, _p, _g = self._split(script, before)
        self.assertEqual(" ".join(w['word'] for s in segs for w in s['words']), before)


class DirectionTimingTest(unittest.TestCase):
    """A direction gets a beat of its own only when the silence can hold it long
    enough to read; otherwise it rides the neighbouring line, which stays up."""

    SCRIPT = ("**JOHN**: One two three four five.\n"
              "*(%s)*\n"
              "**DAVID**: Six seven eight nine ten.\n")
    LONG = ("this clip is almost impossible to transcribe in a way that does "
            "justice to the lisp so it is written as it would have sounded")

    def _run(self, note, gap):
        path = _md(self.SCRIPT % note)
        segs = _segs("One two three four five.")
        tail = _segs("Six seven eight nine ten.")[0]
        shift = segs[0]['end'] + gap
        tail['start'] += shift
        tail['end'] += shift
        for w in tail['words']:
            w['start'] += shift
            w['end'] += shift
        return _chunks_from_segments(segs + [tail], path, track_duration=60.0)

    def test_long_note_in_a_short_gap_rides_the_line(self):
        # It rides in `pre`, not `cues`: the note introduces the line it lands on,
        # so it reads above the words rather than under them.
        chunks, _times = self._run(self.LONG, 0.5)
        self.assertFalse(any(c['is_stage'] for c in chunks))
        self.assertTrue(any(self.LONG[:20] in cue
                            for c in chunks for cue in c.get('pre', [])))

    def test_long_note_with_room_gets_its_own_beat(self):
        chunks, times = self._run(self.LONG, 20.0)
        beat = [(c, w) for c, w in zip(chunks, times) if c['is_stage']]
        self.assertEqual(len(beat), 1)
        self.assertGreater(beat[0][1][1] - beat[0][1][0], 10.0)

    def test_short_note_still_gets_a_short_gap(self):
        chunks, _times = self._run("he sighs", 0.9)
        self.assertTrue(any(c['is_stage'] for c in chunks))


if __name__ == '__main__':
    unittest.main()
