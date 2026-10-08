"""The Directory tab: folders and audio files only, and each folder's tracks."""
import os
import shutil
import tempfile
import unittest

from backtrack.menus import directory


class DirectoryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        for rel in ("b/1.mp3", "b/sub/2.flac", "A/3.ogg", ".hid/4.mp3", "notes.txt", "x.MP3", ".y.mp3"):
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w").close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_folders_and_audio_only(self):
        subs, files = directory._listing(self.root, {"ignore_hidden_files": True})
        self.assertEqual([os.path.basename(s) for s in subs], ["A", "b"])
        self.assertEqual([os.path.basename(f) for f in files], ["x.MP3"])
        subs, files = directory._listing(self.root, {"ignore_hidden_files": False})
        self.assertIn(".hid", [os.path.basename(s) for s in subs])
        self.assertIn(".y.mp3", [os.path.basename(f) for f in files])

    def test_each_folder_gets_everything_beneath_it(self):
        j = lambda *p: os.path.join(self.root, *p)
        lib = [{'path': j("b", "sub", "2.flac")}, {'path': j("b", "1.mp3")}, {'path': j("A", "3.ogg")},
               {'path': j("x.MP3")}, {'path': j("bb", "5.mp3")}]
        under = directory._under(lib, self.root + os.sep, [j("A"), j("b")])
        self.assertEqual([t['path'] for t in under[j("b")]], [j("b", "1.mp3"), j("b", "sub", "2.flac")])
        self.assertEqual(len(under[j("A")]), 1)


class EmptyFolderTest(unittest.TestCase):
    def test_a_folder_with_no_audio_beneath_is_left_out(self):
        root = tempfile.mkdtemp()
        try:
            j = lambda *p: os.path.join(root, *p)
            os.makedirs(j("Album", "CD1")); open(j("Album", "CD1", "01.mp3"), "w").close()
            os.makedirs(j("Scans", "Inner")); open(j("Scans", "Inner", "cover.jpg"), "w").close()
            os.makedirs(j("Empty"))
            subs, _files = directory._listing(root, {"ignore_hidden_files": True})
            self.assertEqual([os.path.basename(p) for p in subs], ["Album"])
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
