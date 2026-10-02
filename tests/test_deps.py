"""backtrack starts only with VLC, and says how to get it when it's missing,
before the terminal UI: a missing libvlc never surfaces as a traceback."""
import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from backbone import deps
from backtrack import deps as bt_deps
from backtrack.playback import libvlc


class BacktrackDepsTest(unittest.TestCase):
    def test_vlc_is_required_and_reported_missing(self):
        with patch.object(libvlc, "vlc", None):
            rows = dict((d.name, (d, found)) for d, found in deps.check(bt_deps.DEPS))
            self.assertIsNone(rows["VLC"][1])
            self.assertTrue(rows["VLC"][0].required)
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit):
                deps.require(bt_deps.DEPS, "backtrack")
        self.assertIn("VLC (for playing audio)", err.getvalue())

    def test_the_rest_are_optional(self):
        self.assertEqual([d.name for d in bt_deps.DEPS if d.required], ["VLC"])


if __name__ == "__main__":
    unittest.main()
