"""quietly(): carry on past a failure, but leave a trace in the diagnostics log."""
import logging
import unittest

from src.state import QuitToTerminal
from src.utils.log import log, quietly


class QuietlyTest(unittest.TestCase):
    def test_carries_on_and_logs_where(self):
        ran_after = False
        with self.assertLogs(log, level=logging.DEBUG) as seen:
            level = log.level
            log.setLevel(logging.DEBUG)
            try:
                with quietly():
                    raise ValueError("boom")
                ran_after = True
            finally:
                log.setLevel(level)
        self.assertTrue(ran_after)
        self.assertRegex(seen.output[0], r"ignored at test_quietly\.py:\d+: ValueError: boom")

    def test_never_swallows_a_quit(self):
        with self.assertRaises(QuitToTerminal):
            with quietly():
                raise QuitToTerminal()

    def test_silent_when_diagnostics_off(self):
        with quietly():
            raise OSError("nobody needs to know")


if __name__ == "__main__":
    unittest.main()
