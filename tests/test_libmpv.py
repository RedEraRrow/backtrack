"""The editors' player: no libmpv or a file it can't open means an editor
without sound, never an error; loading libmpv leaves the environment as it was."""
import os
import unittest
from unittest.mock import patch

from backtrack.playback import libmpv


class LibmpvTest(unittest.TestCase):
    def test_no_player_or_no_file_is_no_clip(self):
        with patch.object(libmpv, 'mpv', None):
            self.assertIsNone(libmpv.clip("/m/song.mp3"))
            self.assertIsNone(libmpv.version())
        if libmpv.mpv is not None:
            self.assertIsNone(libmpv.clip("/nowhere/song.mp3"))

    def test_loading_leaves_the_library_path_alone(self):
        before = os.environ.get(libmpv._FALLBACK)
        libmpv._load()
        self.assertEqual(os.environ.get(libmpv._FALLBACK), before)


if __name__ == "__main__":
    unittest.main()
