"""Opt-in real-image album art (iTerm2): off unless the setting is on AND the
terminal is iTerm2 outside tmux; when on, the layout is exactly the text art's
(same cells, left blank) and the image is drawn over those cells."""
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from src.playback import player_art as art
from src.playback.player_geom import geom

ART = "\n".join("\033[38;2;1;2;3m\033[48;2;4;5;6m▀" * 8 + "\033[0m" for _ in range(4))


class InlineArtTest(unittest.TestCase):
    def test_a_heavy_cover_is_sent_at_display_size(self):
        import cv2
        import numpy as np
        big = cv2.imencode('.png', np.random.randint(0, 255, (1600, 1600, 3), np.uint8))[1].tobytes()
        art._inline_art_cache.clear(); art._decoded_cache.clear()
        with patch.object(art, "get_art_bytes", lambda p: big), \
             patch.object(art.os.path, "getmtime", lambda p: 1.0):
            b64, n = art._inline_art_data("/m/big.mp3", 20, 10)
        img = cv2.imdecode(np.frombuffer(__import__('base64').b64decode(b64), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(img.shape[:2], (10 * 2 * art._INLINE_PX_PER_COL, 20 * art._INLINE_PX_PER_COL))
        self.assertLess(n, len(big))

    def _env(self, term="iTerm.app", tmux=None, on=True):
        art._inline_cfg.clear()          # the setting is cached on config.json's mtime
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TERM_PROGRAM", "LC_TERMINAL")}
        if term:
            env["TERM_PROGRAM"] = term
        if tmux:
            env["TMUX"] = tmux
        return (patch.dict(os.environ, env, clear=True),
                patch("src.config.load_config", lambda: {"art_inline_images": on}))

    def test_off_unless_setting_on_and_iterm_outside_tmux(self):
        for kw, expect in (({}, True), ({"on": False}, False),
                           ({"term": "Apple_Terminal"}, False), ({"tmux": "/tmp/x"}, False)):
            a, b = self._env(**kw)
            with a, b:
                self.assertEqual(art.inline_art_enabled(), expect, kw)

    def test_colour_then_preview_then_full_image_over_the_same_cells(self):
        a, b = self._env()
        sized = []
        with a, b, patch.object(art, "_art_fit", lambda *x: (ART, ART.splitlines())), \
             patch.object(art, "_cover_mean", lambda p: (10, 20, 30)), \
             patch.object(art, "_inline_art_data",
                          lambda p, c=0, r=0, px=12: sized.append((c, r, px)) or (f"IMG{px}", 3)):
            _s, lines = art._art_width_for_height("/m/x.mp3", 8, 10, None)
            self.assertEqual(lines, ["\033[48;2;10;20;30m" + " " * 8 + "\033[0m"] * 4)   # average colour
            geom.art_top, geom.art_left = 3, 5
            geom.art_width, geom.art_height = 8, 4
            buf = io.StringIO()
            with redirect_stdout(buf):
                art._draw_inline_art()
        out = buf.getvalue()
        self.assertEqual(sized, [(8, 4, art._INLINE_PREVIEW_PX), (8, 4, art._INLINE_PX_PER_COL)])
        self.assertLess(out.index(f"IMG{art._INLINE_PREVIEW_PX}"),
                        out.index(f"IMG{art._INLINE_PX_PER_COL}"))       # preview first
        self.assertEqual(out.count("\033[3;6H"), 2)               # both at the art's top-left cell
        self.assertIn("width=8;height=4", out)
        self.assertTrue(out.startswith("\0337") and out.endswith("\a\0338"))

    def test_mid_resize_sends_only_the_preview_then_only_full_once_settled(self):
        sent = []
        art._inline_art['path'] = "/m/x.mp3"
        geom.art_top, geom.art_left, geom.art_width, geom.art_height = 2, 0, 8, 4
        with patch.object(art, "_inline_art_data", lambda p, c=0, r=0, px=12: sent.append(px) or ("X", 1)), \
             redirect_stdout(io.StringIO()):
            art.set_resizing(True)
            art._draw_inline_art()
            self.assertEqual(sent, [art._INLINE_PREVIEW_PX])
            sent.clear()
            art.set_resizing(False)
            art.redraw_art_image()
        self.assertEqual(sent, [art._INLINE_PX_PER_COL])          # the preview is already showing
        art._inline_art['path'] = None

    def test_a_resize_mid_send_cuts_the_full_image_short_for_a_resend(self):
        art._inline_art['path'] = "/m/x.mp3"
        geom.art_top, geom.art_left, geom.art_width, geom.art_height = 2, 0, 8, 4
        big = "Z" * (art._INLINE_CHUNK * 3)
        calls = iter([0.0, 1e12])                     # no resize yet, then one mid-send
        buf = io.StringIO()
        with patch.object(art, "_inline_art_data", lambda *a: (big, 1)), \
             patch.object(art.ui_utils, "last_resize_signal_at", lambda: next(calls, 1e12)), \
             redirect_stdout(buf):
            art.redraw_art_image()
        out = buf.getvalue()
        self.assertEqual(out.count("Z"), art._INLINE_CHUNK)       # one piece went, then it stopped
        self.assertTrue(out.endswith("\a\0338"))               # escape closed, cursor restored
        self.assertTrue(art.art_image_incomplete())
        with patch.object(art, "_inline_art_data", lambda *a: ("B" * 10, 1)), redirect_stdout(io.StringIO()):
            art.redraw_art_image()
        self.assertFalse(art.art_image_incomplete())
        art._inline_art['path'] = None

    def test_full_screen_mode_turns_auto_wrap_off_and_back_on(self):
        from src.utils import ui_utils
        buf = io.StringIO()
        with redirect_stdout(buf):
            ui_utils.enter_alt_screen()
            on = buf.getvalue()
            ui_utils.exit_alt_screen()
        self.assertIn("\033[?7l", on)
        self.assertIn("\033[?7h", buf.getvalue()[len(on):])

    def test_a_cover_that_wont_decode_falls_back_to_text(self):
        a, b = self._env()
        with a, b, patch.object(art, "_art_fit", lambda *x: (ART, ART.splitlines())), \
             patch.object(art, "_cover_mean", lambda p: None):
            _s, lines = art._art_width_for_height("/m/x.mp3", 8, 10, None)
        self.assertEqual(lines, ART.splitlines())
        self.assertIsNone(art._inline_art['path'])

    def test_group_covers_and_disabled_keep_the_text_art(self):
        a, b = self._env(on=False)
        with a, b, patch.object(art, "_art_fit", lambda *x: (ART, ART.splitlines())):
            _s, lines = art._art_width_for_height("/m/x.mp3", 8, 10, None)
        self.assertEqual(lines, ART.splitlines())
        self.assertIsNone(art._inline_art['path'])
        a, b = self._env()
        with a, b, patch.object(art, "_art_fit", lambda *x: (ART, ART.splitlines())):
            _s, lines = art._art_width_for_height("/m/x.mp3", 8, 10, ART)   # a group cover
        self.assertEqual(lines, ART.splitlines())


if __name__ == "__main__":
    unittest.main()
