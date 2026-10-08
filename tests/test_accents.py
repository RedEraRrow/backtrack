"""Accent colours: chosen in Settings, or taken from the playing track's cover."""
import colorsys
import os
import shutil
import tempfile
import unittest
from io import BytesIO
from unittest import mock

from PIL import Image

from _media import make_audio, needs_ffmpeg
from backbone import ui
from backtrack import accents
from backtrack.id3 import tag_writer as tw
from backtrack.playback import player_art


def _png(*colours) -> bytes:
    """A cover in vertical bands of `colours`."""
    img = Image.new("RGB", (60, 60))
    w = 60 // len(colours)
    for i, c in enumerate(colours):
        img.paste(c, (i * w, 0, (i + 1) * w, 60))
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@needs_ffmpeg
class ArtAccentsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _with_cover(self, name, *colours):
        path = make_audio(self.dir, 'mp3', name=name)
        tw.write_cover(path, _png(*colours), 'image/png')
        return path

    def test_two_readable_colours_of_different_hues(self):
        first, second = player_art.art_accents(self._with_cover('c', (200, 30, 30), (200, 30, 30), (30, 60, 200)))
        h1, l1, _ = colorsys.rgb_to_hls(*(int(first[i:i + 2], 16) / 255 for i in (1, 3, 5)))
        h2, l2, _ = colorsys.rgb_to_hls(*(int(second[i:i + 2], 16) / 255 for i in (1, 3, 5)))
        self.assertLess(min(h1, 1 - h1), 0.05)                       # red, the biggest band, first
        self.assertAlmostEqual(h2, 0.63, delta=0.05)                 # then the blue
        self.assertAlmostEqual(l1, player_art._ART_LIGHTNESS, delta=0.02)

    def test_a_grey_cover_gives_none(self):
        self.assertIsNone(player_art.art_accents(self._with_cover('g', (90, 90, 90), (200, 200, 200))))

    def test_one_colour_gets_its_opposite(self):
        first, second = player_art.art_accents(self._with_cover('o', (30, 60, 200)))
        self.assertNotEqual(first, second)

    def test_a_cover_fills_its_box_whatever_its_shape(self):
        import re
        from backtrack.album_art import decode_image
        path = self._with_cover('w', (200, 30, 30), (30, 200, 30), (30, 30, 200))   # 3:1, wide
        with mock.patch.object(ui, 'cell_aspect', lambda *a: 2.0):
            lines = player_art.fill_art(path, 20, 10)
            self.assertEqual(len(lines), 10)
            for line in lines:
                cells = re.findall(r'((?:\x1b\[[0-9;]*m)*)([^\x1b])', line.removesuffix("\x1b[0m"))
                self.assertEqual(len(cells), 20)
                self.assertTrue(all(ch == "▀" and "49m" not in st for st, ch in cells))   # no gap anywhere
            self.assertIn("30;200;30", lines[5])                    # cropped about its centre: the green
            data = player_art._inline_art_data(path, 20, 10)
        import base64
        img = decode_image(base64.b64decode(data[0]))
        self.assertAlmostEqual(img.width / img.height, 1.0, delta=0.05)   # 20 × 10 cells: square on screen

    def test_the_tag_editors_picture_is_a_filled_square_across_the_box(self):
        from mutagen.id3 import APIC
        from backtrack.id3 import browser
        frame = APIC(encoding=3, mime='image/png', type=3, desc='', data=_png((200, 30, 30), (30, 200, 30)))
        with mock.patch.object(ui, 'cell_aspect', lambda *a: 2.0), \
             mock.patch.object(ui, 'get_terminal_width', lambda *a: 100), \
             mock.patch.object(browser, 'get_terminal_width', lambda *a: 100), \
             mock.patch.object(browser, '_visible_rows', lambda: 30), \
             mock.patch.object(player_art, 'inline_art_enabled', lambda: False):
            lines = browser._art_lines_boxed(frame, 10, "APIC")
        plain = [ui.strip_ansi(l) for l in lines]
        self.assertEqual({ui.visual_len(l) for l in plain}, {100 - ui.MARGIN_H})     # the screen's width
        art = [l for l in plain[1:-1]]
        self.assertEqual(len(art), 18)                                   # every row there is
        self.assertTrue(all(l.count("▀") == 36 for l in art))            # 18 rows × 2: square, no gap
        self.assertIn("Size", plain[1])                                  # what it is, beside it

    def test_a_round_portrait_is_never_cut(self):
        from PIL import ImageDraw
        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((0, 0, 59, 59), fill=(200, 30, 30, 255))
        with mock.patch.object(ui, 'cell_aspect', lambda *a: 2.0):
            out = player_art._fill_or_fit(img, 30, 5)                  # wide and short: a crop would cut it
        self.assertEqual(out.size, (30, 10))
        rows = [[out.getpixel((x, y))[3] >= 128 for x in range(30)] for y in range(10)]
        self.assertTrue(any(rows[0]) and any(rows[-1]))                   # its top and bottom both there
        self.assertFalse(rows[0][0] or rows[-1][0])                       # with clear corners: a whole circle

    def test_apply_follows_the_setting(self):
        path = self._with_cover('a', (200, 30, 30))
        art = {'accent_colour': 'green', 'accent_colour_2': 'cyan', 'accent_from_art': True}
        fixed = {**art, 'accent_from_art': False}
        try:
            accents.apply(path, art)
            self.assertNotEqual(ui.Colors.ACCENT, ui.accent_code('green'))
            accents.apply(path, fixed)
            self.assertEqual((ui.Colors.ACCENT, ui.Colors.ACCENT2), (ui.accent_code('green'), ui.accent_code('cyan')))
            accents.apply(None, art)                                  # nothing playing: the chosen ones
            self.assertEqual(ui.Colors.ACCENT, ui.accent_code('green'))
        finally:
            ui.set_accent(ui.DEFAULT_ACCENT)
            ui.set_accent(ui.DEFAULT_ACCENT2, secondary=True)


if __name__ == "__main__":
    unittest.main()
