"""Splitting values that hold several names into one value per name
(backtrack/id3/bulk_split.py): what divides by default, and what is written."""
import os
import shutil
import tempfile
import unittest

from mutagen.id3 import ID3, TCOM, TPE1, TSOC  # type: ignore[reportPrivateImportUsage]

from backtrack.id3 import bulk_split as bs


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)

    def _mp3(self, **frames) -> str:
        path = os.path.join(self.tmp, f"{len(os.listdir(self.tmp))}.mp3")
        open(path, "wb").close()
        audio = ID3()
        for frame in frames.values():
            audio.add(frame)
        audio.save(path, v2_version=3)
        return path

    def test_writers_divide_at_names_and_artists_only_at_list_separators(self):
        self.assertEqual(bs.options('composer', 'Will Smith; Robert Bell')[0], ['Will Smith', 'Robert Bell'])
        self.assertEqual(bs.options('composer', 'Jerome Kern & Johnny Mercer')[0], ['Jerome Kern', 'Johnny Mercer'])
        self.assertEqual(bs.options('artist', 'Earth, Wind & Fire')[0], ['Earth, Wind & Fire'])    # an act, whole
        self.assertIn(['Earth', 'Wind', 'Fire'], bs.options('artist', 'Earth, Wind & Fire'))       # still offered
        self.assertEqual(bs.options('artist', 'Gary Barlow; Robbie Williams')[0], ['Gary Barlow', 'Robbie Williams'])

    def test_a_split_writes_one_value_per_name_and_its_sort_tag_alongside(self):
        p = self._mp3(c=TCOM(encoding=3, text=['Fred E. Ahlert; Roy Turk']),
                      s=TSOC(encoding=3, text=['Ahlert, Fred E.; Turk, Roy']),
                      a=TPE1(encoding=3, text=['Earth, Wind & Fire']))
        entries, skipped = bs.read_candidates([p], ['composer', 'artist'])
        plan = bs.plan_split(entries, {}, skipped)
        self.assertEqual(len(plan.changed), 1)
        bs.write_split(plan.changed[0])
        tags = ID3(p)
        self.assertEqual(tags['TCOM'].text, ['Fred E. Ahlert', 'Roy Turk'])
        self.assertEqual(tags['TSOC'].text, ['Ahlert, Fred E.', 'Turk, Roy'])
        self.assertEqual(tags['TPE1'].text, ['Earth, Wind & Fire'])                 # left whole
        self.assertEqual(tags.version[:2], (2, 4))                                  # several values need v2.4

    def test_a_reviewed_split_wins_and_one_name_is_nothing_to_do(self):
        p = self._mp3(c=TCOM(encoding=3, text=['Simon & Garfunkel']))
        entries, _ = bs.read_candidates([p], ['composer'])
        plan = bs.plan_split(entries, {'Simon & Garfunkel': ['Paul Simon', 'Art Garfunkel']})
        self.assertEqual(plan.changed[0].fields['split']['composer'], ['Paul Simon', 'Art Garfunkel'])
        lone = self._mp3(c=TCOM(encoding=3, text=['Lalo Schifrin']))
        self.assertEqual(bs.read_candidates([lone], ['composer'])[0], {})


if __name__ == "__main__":
    unittest.main()
