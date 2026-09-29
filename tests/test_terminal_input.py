"""The polling key read the player uses: real terminal bytes in, the same key
names every other screen gets out."""
import os
import pty
import sys
import termios
import tty
import unittest
from unittest.mock import patch

from src.utils import terminal_input as ti


class NonBlockingKeyTest(unittest.TestCase):
    def setUp(self):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave, termios.TCSANOW)   # raw_mode's drain would wait on the pty forever
        self.stdin = os.fdopen(self.slave, 'rb', buffering=0)
        self._p = patch.object(sys, 'stdin', self.stdin)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.stdin.close()
        os.close(self.master)

    def keys(self, raw: bytes) -> list:
        os.write(self.master, raw)
        out = []
        while (k := ti.get_key_non_blocking()) is not None:
            out.append(k)
        return out

    def test_nothing_waiting(self):
        self.assertEqual(self.keys(b''), [])

    def test_named_keys(self):
        self.assertEqual(self.keys(b'\x1b[C \x1b'), ['RIGHT', 'SPACE', 'ESC'])   # a lone Esc
        self.assertEqual(self.keys(b'p['), ['p', '['])

    def test_mouse_and_transport(self):
        self.assertEqual(self.keys(b'\x1b[<0;12;5M\x1b[<0;12;5m\x10'),
                         ['MOUSE_CLICK:0:5:12', 'MOUSE_RELEASE:0:5:12', '\x10'])


if __name__ == "__main__":
    unittest.main()
