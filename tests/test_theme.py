"""The theme: chosen in Settings, or Album art, with its accents taken from
the playing track's cover; and the cover pictures themselves."""
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
from backtrack import theme
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

    def _hls(self, colour):
        return colorsys.rgb_to_hls(*(v / 255 for v in ui.parse_hex_colour(colour)))

    def test_the_covers_own_colours_biggest_first(self):
        with mock.patch.object(ui, 'terminal_background', lambda: "#000000"):
            first, second = player_art.art_accents(self._with_cover('c', (255, 160, 0), (255, 160, 0), (90, 200, 255)))
        self.assertEqual((first, second), ("#FFA000", "#5AC8FF"))     # readable already: exactly as in the cover

    def test_too_dark_is_lightened_only_as_far_as_it_must_be(self):
        with mock.patch.object(ui, 'terminal_background', lambda: "#000000"):
            first, second = player_art.art_accents(self._with_cover('d', (140, 20, 20), (140, 20, 20), (20, 40, 140)))
        for got, was in ((first, "#8C1414"), (second, "#14288C")):
            self.assertGreaterEqual(ui.contrast_ratio(got, "#000000"), 4.5)
            self.assertLess(ui.contrast_ratio(got, "#000000"), 4.9)               # no further than it must
            self.assertAlmostEqual(self._hls(got)[0], self._hls(was)[0], delta=0.01)   # the cover's hue
            self.assertAlmostEqual(self._hls(got)[2], self._hls(was)[2], delta=0.03)   # and saturation

    def test_on_a_light_terminal_they_darken(self):
        with mock.patch.object(ui, 'terminal_background', lambda: "#FFFFFF"), \
             mock.patch.object(ui, 'background_is_light', lambda: True):
            first, _second = player_art.art_accents(self._with_cover('l', (255, 220, 60)))
        self.assertGreaterEqual(ui.contrast_ratio(first, "#FFFFFF"), 4.5)
        self.assertAlmostEqual(self._hls(first)[0], self._hls("#FFDC3C")[0], delta=0.01)

    def test_a_grey_cover_gives_none(self):
        self.assertIsNone(player_art.art_accents(self._with_cover('g', (90, 90, 90), (200, 200, 200))))

    def test_one_colour_gives_two_tones_of_it_not_its_opposite(self):
        with mock.patch.object(ui, 'terminal_background', lambda: "#000000"):
            first, second = player_art.art_accents(self._with_cover('o', (30, 60, 200)))
        self.assertNotEqual(first, second)
        self.assertAlmostEqual(self._hls(first)[0], self._hls(second)[0], delta=0.01)

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

    def test_a_wide_cover_keeps_its_shape_and_is_never_cut(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "wide.png")
        img = Image.new("RGB", (200, 100), (200, 30, 30))
        img.save(path)
        with mock.patch.object(ui, 'cell_aspect', lambda *a: 2.0):
            self.assertEqual(player_art.cover_box(path, 40, 20), (40, 10))          # 2:1, not a square
            self.assertEqual(player_art.cover_box("", 40, 20, player_art.IDLE_ART), (40, 20))   # a placeholder: square
            out = player_art._fit_cells(img, 20, 10)                                 # a square's room
        rows = [[out.getpixel((x, y))[3] >= 128 for x in range(20)] for y in range(20)]
        self.assertTrue(all(r[0] and r[-1] for r in rows if any(r)))              # its left and right edges both there
        self.assertFalse(any(rows[0]) and any(rows[-1]))                          # fitted: room above or below, not cut

    def test_a_round_portrait_is_never_cut(self):
        from PIL import ImageDraw
        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((0, 0, 59, 59), fill=(200, 30, 30, 255))
        with mock.patch.object(ui, 'cell_aspect', lambda *a: 2.0):
            out = player_art._fit_cells(img, 30, 5)                  # wide and short: a crop would cut it
        self.assertEqual(out.size, (30, 10))
        rows = [[out.getpixel((x, y))[3] >= 128 for x in range(30)] for y in range(10)]
        self.assertTrue(any(rows[0]) and any(rows[-1]))                   # its top and bottom both there
        self.assertFalse(rows[0][0] or rows[-1][0])                       # with clear corners: a whole circle

    def test_apply_follows_the_setting(self):
        path = self._with_cover('a', (200, 30, 30))
        terminal = ui.theme_codes('terminal')
        art = {'theme': theme.ALBUM_ART, 'accent_colour': 'blue', 'accent_colour_2': 'yellow'}
        try:
            theme.apply(path, art)
            self.assertNotEqual(ui.Colors.ACCENT, ui.TERMINAL_ACCENTS['blue'][1])    # the cover's
            self.assertEqual(ui.Colors.OK, terminal['OK'])                           # the rest Terminal's
            self.assertEqual(ui.Colors.ACCENT, ui.theme_codes('terminal', theme.overrides(theme.ALBUM_ART, path))['ACCENT'])
            theme.apply(None, art)                                    # nothing playing: the chosen accents
            self.assertEqual((ui.Colors.ACCENT, ui.Colors.ACCENT2),
                             (ui.TERMINAL_ACCENTS['blue'][1], ui.TERMINAL_ACCENTS['yellow'][1]))
            theme.apply(path, {**art, 'theme': 'terminal'})           # Terminal: the chosen accents
            self.assertEqual(ui.Colors.ACCENT, ui.TERMINAL_ACCENTS['blue'][1])
            theme.apply(path, {**art, 'theme': 'matrix'})             # a fixed theme: its own
            self.assertEqual(ui.Colors.ACCENT, ui.theme_codes('matrix')['ACCENT'])
            theme.apply(None, {'theme': 'terminal', 'accent_colour': 'rose'})   # not one of the terminal's
            self.assertEqual(ui.Colors.ACCENT, ui.TERMINAL_ACCENTS['green'][1])
        finally:
            ui.set_terminal_accents(*ui.DEFAULT_ACCENTS)
            ui.set_theme(ui.DEFAULT_THEME)

if __name__ == "__main__":
    unittest.main()
