"""The theme (the Settings picker, and settings written before themes) and
the player's remembered track-details line."""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backtrack import config
from backbone import ui as u


def _pick(keys, screen='_pick_theme'):
    """Drive Settings → Theme (or another of its screens) with scripted keys;
    returns the config."""
    from backbone.prompt import lists
    from backtrack.menus import settings

    class W:
        def __init__(self, fd): pass
        def render(self, lines): pass
        def clear(self): pass
        def anchor_reset(self): pass
    feed = iter(keys)
    cfg = {'theme': 'terminal'}
    saved = _pick.saved = []
    real, sys.stdout = sys.stdout, io.StringIO()
    try:
        with patch.object(lists, '_read_key', lambda fd: next(feed)), \
             patch.object(lists, '_wait_for_keypress', lambda t: True), \
             patch.object(lists, '_set_raw'), patch.object(lists, '_restore_term_attrs'), \
             patch.object(lists, '_get_term_attrs'), patch.object(lists, '_Widget', W), \
             patch.object(settings, '_now_playing_path', lambda: None), \
             patch.object(settings, '_commit', lambda cfg, *keys: saved.append({k: cfg[k] for k in keys})), \
             patch.object(lists.sys.stdin, 'fileno', lambda: 0):
            getattr(settings, screen)(cfg)
    finally:
        sys.stdout = real
    return cfg


class PickerTest(unittest.TestCase):
    def tearDown(self):
        u.set_terminal_accents(*u.DEFAULT_ACCENTS)
        u.set_theme(u.DEFAULT_THEME)

    def test_picking_a_theme_applies_it(self):
        cfg = _pick(['DOWN', 'ENTER', 'ESC'])                   # Terminal → Teletext
        self.assertEqual(cfg['theme'], 'teletext')
        self.assertEqual(u.Colors.ACCENT, u.theme_codes('teletext')['ACCENT'])
        self.assertEqual(_pick.saved, [{'theme': 'teletext'}])     # saved as it's picked, not on leaving

    def test_terminals_accent_is_picked_from_the_terminals_colours(self):
        cfg = _pick(['ENTER', 'DOWN', 'ENTER', 'ESC', 'ESC'], '_pick_accents')   # Accent: Green → Red
        self.assertEqual(cfg['accent_colour'], 'red')
        self.assertEqual(u.Colors.ACCENT, u.TERMINAL_ACCENTS['red'][1])
        self.assertEqual(_pick.saved, [{'accent_colour': 'red'}])

    def test_album_art_is_offered_last(self):
        self.assertEqual(_pick(['END', 'ENTER', 'ESC'])['theme'], 'album_art')


class OldSettingsTest(unittest.TestCase):
    """A config written before themes: colours from album art becomes the
    Album art theme; anything else, Terminal."""

    def load(self, saved: dict) -> dict:
        path = Path(tempfile.mkdtemp()) / "config.json"
        path.write_text(json.dumps(saved))
        with patch.object(config, 'CONFIG_FILE', path):
            return config.load_config()

    def test_art_mode_becomes_the_album_art_theme(self):
        self.assertEqual(self.load({'accent_colour': 'rose', 'accent_from_art': True})['theme'], 'album_art')

    def test_a_chosen_accent_becomes_terminal(self):
        self.assertEqual(self.load({'accent_colour': 'rose', 'accent_from_art': False})['theme'], 'terminal')

    def test_a_much_older_theme_is_replaced(self):
        self.assertEqual(self.load({'theme': {'accent': '\033[1;31m'}, 'accent_from_art': False})['theme'], 'terminal')

    def test_a_theme_already_chosen_stays(self):
        self.assertEqual(self.load({'theme': 'amber', 'accent_from_art': True})['theme'], 'amber')


class PlayerDetailsTest(unittest.TestCase):
    def test_m_is_remembered(self):
        from backtrack.playback import player_ui
        before = config.load_config().get('player_show_metadata', True)
        try:
            config.update_config({'player_show_metadata': True})
            player_ui.refresh_player_settings()
            self.assertTrue(player_ui._ui_state['show_metadata'])      # on by default
            player_ui.toggle_metadata()
            self.assertFalse(config.load_config()['player_show_metadata'])
            player_ui._ui_state['show_metadata'] = True                # a later play reloads it
            player_ui.refresh_player_settings()
            self.assertFalse(player_ui._ui_state['show_metadata'])
        finally:
            config.update_config({'player_show_metadata': before})


if __name__ == "__main__":
    unittest.main()
