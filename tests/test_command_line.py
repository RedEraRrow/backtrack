"""The `:` command line: CLI commands run in the app, on its library and session."""
import os
import tempfile
import unittest
from unittest import mock

from backbone import output as out, ui
from backtrack import cli, cli_commands
from backtrack.playback import session as sess


class RunInAppTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.track = os.path.join(self.dir, "a.mp3")
        open(self.track, "w").close()
        self.library = [{'path': self.track, 'title': "Only Here", 'artist': "Nobody Else",
                         'album': "Inside", 'album_artist': "Nobody Else", 'track': '1', 'disc': '1'}]

    def test_the_apps_library_and_its_state_put_back(self):
        ui.set_colour(True)
        code, text = cli.run_in_app("track list --json", self.library)
        self.assertEqual(code, out.OK)
        self.assertIn("Only Here", text)
        self.assertNotIn("\033[", text)
        self.assertTrue(ui._colour_on)
        self.assertFalse(out.json_mode())
        self.assertFalse(cli_commands.IN_APP[0])

    def test_play_goes_to_this_windows_session(self):
        sent = []
        with mock.patch.object(sess.SESSION, '_handle_remote_command', lambda n, a: sent.append((n, a))), \
             mock.patch.object(sess.SESSION, 'start_background_tick', lambda: None), \
             mock.patch.object(sess, 'is_client', lambda: False), \
             mock.patch.object(cli_commands, '_play_here', side_effect=AssertionError("started its own audio")):
            code, _text = cli.run_in_app(f"play '{self.track}'", self.library)
        self.assertEqual(code, out.OK)
        self.assertEqual(sent[0][0], 'play')
        self.assertEqual(sent[0][1]['path'], self.track)

    def test_unreadable_and_empty(self):
        self.assertEqual(cli.run_in_app("track list --artist 'unclosed", self.library)[0], out.USAGE)
        self.assertEqual(cli.run_in_app("   ", self.library), (out.OK, ""))
        self.assertNotEqual(cli.run_in_app("nonsense", self.library)[0], out.OK)


if __name__ == "__main__":
    unittest.main()
