"""Clicking a transport hint (^p, ^n/^b) acts like pressing it: consume_chrome
used to hand the control character back to widgets that don't handle it."""
import unittest
from unittest.mock import patch

from src.utils import prompt, prompt_core


class TransportHintClickTest(unittest.TestCase):
    def test_clicked_transport_hints_run_the_transport(self):
        calls = []
        cells: dict = {}
        prompt_core.add_hint_click_cells(cells, "[^p] play/pause  [^n/^b] next/prev", 5,
                                         [("^p", "play/pause"), ("^n/^b", "next/prev")])
        with patch.object(prompt, '_transport_handler', calls.append), \
             patch.object(prompt, 'now_playing_click_action', lambda r, c: None):
            click = lambda col: prompt.consume_chrome(f"MOUSE_CLICK:0:5:{col}", cells)
            cols = {v: c for (r, c), v in cells.items()}
            for key in ('\x10', '\x0e', '\x02'):
                self.assertIs(click(cols[key]), prompt.CHROME_HANDLED)
        self.assertEqual(calls, ['playpause', 'next', 'prev'])

    def test_page_arrows_are_clickable(self):
        self.assertEqual([t[2] for t in prompt_core._hint_key_tokens("⇞⇟")], ['PGUP', 'PGDN'])


if __name__ == "__main__":
    unittest.main()
