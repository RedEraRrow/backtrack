"""Headless tests for src/id3/id3_tag_handler.summarize_tag_value."""
import unittest

from mutagen.id3 import USLT, TIT2  # type: ignore[reportPrivateImportUsage]

from src.id3.id3_tag_handler import summarize_tag_value


class SummarizeTagValueTest(unittest.TestCase):
    def test_uslt_scalar_text_not_split_into_characters(self):
        frame = USLT(encoding=3, lang='eng', desc='', text='hello\nworld')
        self.assertEqual(summarize_tag_value('USLT::eng', frame), 'hello\\world')

    def test_list_text_frame_still_joins_values(self):
        frame = TIT2(encoding=3, text=['foo', 'bar'])
        self.assertEqual(summarize_tag_value('TIT2', frame), 'foo; bar')


if __name__ == '__main__':
    unittest.main()
