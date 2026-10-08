"""Typing a tag's id: suggested as it's typed, checked before it's taken, and
anything taken can be saved (no frame the writer would then refuse)."""
import unittest

from backtrack.id3 import tag_handler as th


class TagIdTest(unittest.TestCase):
    def test_good_ids_are_taken_in_the_apps_form(self):
        for typed, form in (("tpe2", "TPE2"), ("TXXX:Mood", "TXXX:Mood"), ("comm[ENG]", "COMM[eng]"),
                            ("COMM:Notes:FRE", "COMM:Notes:fre"), ("COMM::ger", "COMM::ger"),
                            ("wxxx:Shop", "WXXX:Shop")):
            self.assertIsNone(th.check_tag_id(typed), typed)
            self.assertEqual(th.normalise_tag_id(typed), form)

    def test_bad_ids_say_why(self):
        for typed, says in (("TIT", "four"), ("ZZZZ", "isn't a frame"), ("SYLT", "can't be typed in"),
                            ("TXXX", "needs a description"), ("TPE2:x", "has no description"),
                            ("TPE2::eng", "has no language"), ("COMM[english]", "isn't a language code"), ("COMM:x:xyz", "isn't a language code"),
                            ("CO MM", "not a tag ID")):
            self.assertIn(says, th.check_tag_id(typed) or "", typed)

    def test_renaming_keeps_the_kind_of_value(self):
        self.assertIsNone(th.check_tag_id("TPE1", like="TPE2"))
        self.assertIn("different kind", th.check_tag_id("TDRC", like="TPE2"))

    def test_every_frame_it_offers_can_be_built(self):
        for base in th.TAG_REGISTRY:
            if th._creatable(base):
                self.assertIsNone(th.check_tag_id(base + (":Mood" if base in th._NEEDS_DESC else "")), base)

    def test_suggestions_follow_where_youre_typing(self):
        self.assertIn(("TMOO", "Mood"), th.suggest_tag_ids("tm"))
        self.assertNotIn("SYLT", [v for v, _n in th.suggest_tag_ids("SY")])           # nothing it would refuse
        self.assertIn("TXXX:Mood", [v for v, _n in th.suggest_tag_ids("TXXX:mo")])
        self.assertIn("TXXX:Energy", [v for v, _n in th.suggest_tag_ids("TXXX:", {'TXXX': ["Energy"]})])
        self.assertIn(("COMM:Notes:fre", "French"), th.suggest_tag_ids("COMM:Notes:fr"))
        self.assertIn(("COMM[ger]", "German"), th.suggest_tag_ids("COMM[ge"))
        self.assertEqual(th.suggest_tag_ids(""), [])

    def test_only_ids_are_matched_not_names(self):
        self.assertEqual([k for k, _ in th.suggest_tag_ids("COMM")], ["COMM"])
        self.assertEqual(th.suggest_tag_ids("mood"), [])

    def test_only_keys_heading_for_a_good_id_get_typed(self):
        for t in ["T", "TPE2", "txxx:", "TXXX:Mood", "COMM:Notes:en", "COMM[eng]", "COMM:x:eus"]:
            self.assertTrue(th.could_be_tag_id(t), t)
        for t in [" T", "ZZ", "mood", "TPE2:", "TPE2[", "TXXX:Mood:", "COMM:x:engl", "COMM[eng]x", "COMM[e]", "SYLT", "COMM:x:xy", "COMM[qq"]:
            self.assertFalse(th.could_be_tag_id(t), t)
        self.assertFalse(th.could_be_tag_key("A=", "vorbis"))
        self.assertTrue(th.could_be_tag_key("----:com:MOOD", "mp4"))
        self.assertFalse(th.could_be_tag_key("©wrtx", "mp4"))

    def test_vorbis_and_mp4_keys(self):
        self.assertIsNone(th.check_tag_key("MOOD", "vorbis"))
        self.assertIn("can't have =", th.check_tag_key("A=B", "vorbis"))
        self.assertIsNone(th.check_tag_key("©wrt", "mp4"))
        self.assertIsNone(th.check_tag_key("----:com.apple.iTunes:MOOD", "mp4"))
        self.assertIn("four characters", th.check_tag_key("abc", "mp4"))
        self.assertIn("ALBUMARTIST", [k for k, _l in th.suggest_tag_keys("albuma", "vorbis")])


if __name__ == "__main__":
    unittest.main()
