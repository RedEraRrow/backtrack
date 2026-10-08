"""The column browser's preview: the highlighted row's picture and details over what it holds."""
import unittest
from unittest import mock

from backbone import ui
from backtrack.menus import browse, browse_columns as cols


def _t(n, album="A", disc="1", fmt="MP3", kbps=320):
    return {'path': f"/m/{album}{disc}{n}.mp3", 'title': f"T{n}", 'album': album, 'album_artist': "X",
            'artist': "X", 'track': str(n), 'disc': disc, 'duration': 60, 'format': fmt,
            'sample_rate': 44100, 'channels': 2, 'bitrate': kbps, 'date': "2001"}


class ColumnsTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(cols, '_art_fit', lambda *a: ("", ["#" * 10] * 5))
        p.start()
        self.addCleanup(p.stop)

    def test_short_drops_the_picture_first(self):
        pv = cols.preview(cols.tracks_details("A", [_t(1), _t(2)]), "2 tracks", ["01 - T1", "02 - T2"])
        self.assertEqual(pv.contents, ["01 - T1", "02 - T2"])               # their own box (select)
        tall = "\n".join(ui.strip_ansi(l) for l in pv.details(40, 30))
        self.assertIn("#####", tall)
        self.assertIn("2001", tall)
        short = "\n".join(ui.strip_ansi(l) for l in pv.details(30, 9))
        self.assertNotIn("#####", short)
        self.assertIn("2001", short)

    def test_a_strip_puts_the_picture_beside_the_facts(self):
        lines = [ui.strip_ansi(l) for l in cols.preview(cols.tracks_details("A", [_t(1)])).details(80, 5)]
        self.assertLessEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("#") and "A" in lines[0][10:])

    def test_an_album_of_one_format_says_it_whatever_the_bitrates(self):
        _path, lines = cols.tracks_details("A", [_t(1, kbps=250), _t(2, kbps=310)])
        self.assertIn("MP3 44.1 kHz · stereo", lines)
        _path, lines = cols.tracks_details("A", [_t(1), _t(2, fmt="FLAC")])
        self.assertIn("mixed formats", lines)

    def test_copyright_shows_when_it_is_one(self):
        c = "℗ 2001 Label"
        self.assertIn(c, cols.track_details({**_t(1), 'copyright': c})[1])
        self.assertIn(c, cols.tracks_details("A", [{**_t(1), 'copyright': c}, {**_t(2), 'copyright': c}])[1])
        mixed = cols.tracks_details("G", [{**_t(1), 'copyright': c}, {**_t(2), 'copyright': "© Other"}])[1]
        self.assertFalse(any("℗" in x or "©" in x for x in mixed))           # a group's many: none

    def test_the_preview_lists_what_opening_lists(self):
        songs = [_t(1, disc="1"), _t(1, disc="2"), _t(1, album="B")]
        self.assertEqual(browse._preview_of(songs, True, {})[1], ["A", "B"])
        title, rows = browse._preview_of(songs[:2], False, {})
        self.assertEqual(title, "2 tracks")                           # the disc headings aren't tracks
        self.assertEqual(len(rows), 4)


class PicturesTest(unittest.TestCase):
    def test_an_artist_shows_their_picture_or_the_avatar(self):
        import os, tempfile
        from backtrack.playback.player_art import AVATAR_ART, _shape_lines
        with tempfile.TemporaryDirectory() as d:
            album = os.path.join(d, "Artist", "Album")
            os.makedirs(album)
            t = _t(1)
            t['path'] = os.path.join(album, "01.mp3")
            self.assertEqual(cols.tracks_details("Artist", [t], person=True)[0], AVATAR_ART)
            self.assertEqual(cols.tracks_details("Album", [t])[0], t['path'])       # an album: its cover
            from PIL import Image
            wide = Image.new("RGB", (300, 100), (255, 0, 0))
            wide.paste((0, 255, 0), (100, 0, 200, 100))         # the centre third green
            wide.save(os.path.join(d, "Artist", "Artist.JPG"), "JPEG")
            with mock.patch("backtrack.music_library.CACHE_DIR", __import__("pathlib").Path(d)):
                portrait = cols.tracks_details("Artist", [t], person=True)[0]
            img = Image.open(portrait)
            self.assertEqual(img.size[0], img.size[1])                     # cropped square
            self.assertEqual(img.getpixel((0, 0))[3], 0)                   # round: corners clear
            r, g, b, a = img.getpixel((img.size[0] // 2, img.size[1] // 2))
            self.assertEqual(a, 255)
            self.assertGreater(g, 200)                                     # from the centre
            self.assertLess(r, 60)
        art = _shape_lines(AVATAR_ART, 20, 10)
        self.assertEqual(len(art), 10)
        self.assertTrue(any(c != " " for line in art for c in line))

    def test_an_image_goes_over_its_cells(self):
        with mock.patch.object(cols, 'image_cells', lambda p, w, h: (["~" * w] * h, "ESC")):
            pane = cols.preview(("/m/a.mp3", ["A"]), "", []).details(40, 30)
        line, col, rows, _key, esc, _cols = pane.pictures[0]
        self.assertEqual(esc, "ESC")
        self.assertEqual(ui.strip_ansi(pane[line])[col:col + 3], "~~~")
        self.assertGreater(rows, 0)


    def test_the_art_keeps_one_size(self):
        boxes = set()
        with mock.patch.object(cols, 'image_cells', lambda p, w, h: (["~" * w] * h, "ESC")):
            for info, preview in ((["A"], []), (["A", "B", "C", "D", "E", "F", "G"], ["x"] * 40),
                                  (["A", "B"], ["x", "y"])):
                pane = cols.preview(("/m/a.mp3", info), "t", preview).details(44, 36)
                line, col, rows, (_p, width, _r), _esc, _cols = pane.pictures[0]
                boxes.add((line, col, rows, width))
        self.assertEqual(len(boxes), 1)


if __name__ == "__main__":
    unittest.main()


class StripTest(unittest.TestCase):
    def test_a_short_strip_keeps_its_picture_square_and_its_lines_in(self):
        from backtrack.menus import browse_columns as bc
        draw = bc._details_drawer(bc.AVATAR_ART, ["Name", "a", "b", "c", "d", "e"])
        for w, h in ((60, 5), (60, 3), (80, 4), (40, 8)):
            self.assertLessEqual(len(draw(w, h)), h, (w, h))


class GroupOrderTest(unittest.TestCase):
    def test_the_preview_and_the_list_share_one_order(self):
        from backtrack.menus import browse
        grouped = {n: [{'path': f'/m/{n}.mp3', 'artist': n, 'album': 'A', 'album_artist': n}]
                   for n in ("The Examples", "Atom Heart", "Ex Ample", "Zed")}
        order = browse._group_order(grouped, 'artists', {})
        self.assertEqual(order, ["Atom Heart", "Ex Ample", "The Examples", "Zed"])   # by sort key: "Examples"
