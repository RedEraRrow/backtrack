"""Album art is never cropped or stretched: fit_art only narrows it to fit the
rows it's given, and the half-block render keeps the image's proportions for
the terminal's cell shape."""
import unittest
from unittest.mock import patch

from io import BytesIO

from PIL import Image

from backbone import ui
from backtrack.album_art import fit_art, render_native_half_block


def _image(w, h):
    buf = BytesIO()
    Image.new("RGB", (w, h), (128, 128, 128)).save(buf, "PNG")
    return buf.getvalue()


class ArtFitTest(unittest.TestCase):
    def test_never_taller_than_the_space_and_never_cut(self):
        for w, h in ((600, 600), (300, 900), (900, 300)):
            img = _image(w, h)
            render = lambda cols: render_native_half_block(img, cols)
            for max_w, avail_h in ((80, 40), (80, 12), (40, 50), (120, 7), (5, 1)):
                lines = fit_art(render, max_w, avail_h)
                self.assertLessEqual(len(lines), avail_h)
                cols = max((ui.visual_len(l) for l in lines), default=0)
                # Exactly what the renderer gives at that width: nothing cut off.
                self.assertEqual(lines, render(cols).splitlines() if cols else [])

    def test_proportions_follow_the_cell_shape(self):
        img = _image(1000, 500)                          # 2:1 landscape
        for aspect in (2.0, 2.3):
            with patch.object(ui, 'cell_aspect', lambda *a: aspect):
                lines = render_native_half_block(img, 92).splitlines()
            # On screen: 92 cells wide, rows * aspect cells tall, in cell widths.
            self.assertAlmostEqual(92 / (len(lines) * aspect), 2.0, delta=0.1)


if __name__ == '__main__':
    unittest.main()
