"""track_title and first_text: how a track and a tag value are read for showing."""
import os
import tempfile
import unittest

from mutagen.id3 import ID3, TIT2

from backtrack.menus.play import _queue_titles_for_paths
from backtrack.music_library import first_text, track_title


class TrackTitleTest(unittest.TestCase):
    def test_order(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'Episode 3.mp3')
            with open(p, 'wb') as f:
                f.write(b'\xff\xfb\x90\x00' * 64)
            self.assertEqual(track_title(p), 'Episode 3')                       # file name, no extension
            self.assertEqual(track_title(p, read_tags=True), 'Episode 3')       # untagged file
            tags = ID3(); tags.add(TIT2(encoding=3, text=' The Tag ')); tags.save(p)
            self.assertEqual(track_title(p, read_tags=True), 'The Tag')
            self.assertEqual(track_title(p), 'Episode 3')                       # tags only when asked
            self.assertEqual(track_title(p, {'title': 'From Library'}, read_tags=True), 'From Library')
            self.assertEqual(track_title(p, {'title': '  '}), 'Episode 3')

    def test_queue_titles_never_show_the_extension(self):
        lib = [{'path': '/m/a.mp3', 'title': ''}, {'path': '/m/b.mp3', 'title': 'Bee'}]
        self.assertEqual(_queue_titles_for_paths(['/m/a.mp3', '/m/b.mp3', '/m/c.m4a'], lib),
                         ['a', 'Bee', 'c'])


    def test_first_text(self):
        self.assertEqual(first_text(TIT2(encoding=3, text=['  One ', 'Two'])), 'One')
        self.assertEqual(first_text(TIT2(encoding=3, text=[])), '')
        self.assertEqual(first_text(None), '')


if __name__ == "__main__":
    unittest.main()
