"""Accent colour (presets, custom hex, the Settings picker) and the player's
remembered track-details line."""
import io
import os
import sys
import unittest
from unittest.mock import patch

from src import config
from src.utils import ui_utils as u


class AccentTest(unittest.TestCase):
    def tearDown(self):
        u.set_accent(u.DEFAULT_ACCENT)

    def test_codes(self):
        with patch.dict(os.environ, {'COLORTERM': 'truecolor'}):
            self.assertEqual(u.accent_code('green'), '\033[1;32m')         # the terminal's own green
            self.assertEqual(u.accent_code('#FFB000'), '\033[1;38;2;255;176;0m')
            self.assertEqual(u.accent_code('#fb0'), '\033[1;38;2;255;187;0m')
        with patch.dict(os.environ, {'COLORTERM': ''}):
            self.assertEqual(u.accent_code('amber'), '\033[1;38;5;214m')  # nearest of 256
        for bad in (None, 'nope', '#12', '#GGGGGG', ''):
            self.assertIsNone(u.accent_code(bad), bad)

    def test_set_accent_falls_back_and_respects_no_colour(self):
        u.set_accent('#nonsense')
        self.assertEqual(u.Colors.ACCENT, '\033[1;32m')
        u.set_colour(False)
        try:
            u.set_accent('red')
            self.assertEqual(u.Colors.ACCENT, '')
        finally:
            u.set_colour(True)
        self.assertEqual(u.Colors.ACCENT, '\033[1;31m')

    def test_labels(self):
        self.assertEqual([u.accent_label(v) for v in ('rose', '#ab12cd', None, 'junk')],
                         ['Rose', '#AB12CD', 'Green', 'Green'])


def _pick(keys, typed=None):
    """Drive Settings → Accent colour with scripted keys; returns the config."""
    from src.utils.prompt import lists
    from src.menus import settings

    class W:
        def __init__(self, fd): pass
        def render(self, lines): pass
        def clear(self): pass
        def anchor_reset(self): pass
    feed = iter(keys)
    cfg = {'accent_colour': 'green'}
    real, sys.stdout = sys.stdout, io.StringIO()
    try:
        with patch.object(lists, '_read_key', lambda fd: next(feed)), \
             patch.object(lists, '_wait_for_keypress', lambda t: True), \
             patch.object(lists, '_set_raw'), patch.object(lists, '_restore_term_attrs'), \
             patch.object(lists, '_get_term_attrs'), patch.object(lists, '_Widget', W), \
             patch.object(settings.prompt, 'text', lambda *a, **k: typed), \
             patch.object(lists.sys.stdin, 'fileno', lambda: 0):
            settings._pick_accent(cfg)
    finally:
        sys.stdout = real
    return cfg


class PickerTest(unittest.TestCase):
    def tearDown(self):
        u.set_accent(u.DEFAULT_ACCENT)

    def test_picking_a_preset_applies_it(self):
        cfg = _pick(['DOWN', 'ENTER', 'ESC'])                   # Green → Red
        self.assertEqual(cfg['accent_colour'], 'red')
        self.assertEqual(u.Colors.ACCENT, '\033[1;31m')

    def test_custom_colour_is_normalised_and_bad_input_changes_nothing(self):
        self.assertEqual(_pick(['END', 'ENTER', 'ESC'], typed='4fc3f7')['accent_colour'], '#4FC3F7')
        self.assertEqual(_pick(['END', 'ENTER', 'ESC'], typed='blue-ish')['accent_colour'], 'green')


class PlayerDetailsTest(unittest.TestCase):
    def test_m_is_remembered(self):
        from src.playback import playback_ui as ui
        before = config.load_config().get('player_show_metadata', True)
        try:
            config.update_config({'player_show_metadata': True})
            ui.refresh_player_settings()
            self.assertTrue(ui._ui_state['show_metadata'])      # on by default
            ui.toggle_metadata()
            self.assertFalse(config.load_config()['player_show_metadata'])
            ui._ui_state['show_metadata'] = True                # a later play reloads it
            ui.refresh_player_settings()
            self.assertFalse(ui._ui_state['show_metadata'])
        finally:
            config.update_config({'player_show_metadata': before})


if __name__ == "__main__":
    unittest.main()
