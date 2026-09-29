"""Writes that used to lose data: a one-track cache, ID3 bytes in an MP4, a tag
deleted before its replacement was known to be valid, a rename onto an existing
frame, other lyrics frames wiped on save, a stale config saved over a newer one,
and a config file cut short by a crash mid-write."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mutagen.id3 import ID3, SYLT, TIT1, TIT2, TRCK

from src import config as cfg
from src import music_library as ml
from src.id3 import id3_tag_handler as th


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mp3(self, name="a.mp3", *frames):
        path = os.path.join(self.tmp, name)
        open(path, "wb").close()
        tags = ID3()
        for f in frames:
            tags.add(f)
        tags.save(path)
        return path


class LibraryCacheTest(_Tmp):
    def test_no_library_is_never_saved_as_the_cache(self):
        path = self.mp3()
        with patch.object(ml, "save_library_cache") as save:
            ml.refresh_library_entry(None, path)
        save.assert_not_called()

    def test_an_empty_library_is_real_and_gets_the_track(self):
        path, lib = self.mp3(), []
        with patch.object(ml, "save_library_cache") as save:
            ml.refresh_library_entry(lib, path)
        self.assertEqual([t["path"] for t in lib], [path])
        save.assert_called_once_with(lib)


class TagWriteTest(_Tmp):
    def test_id3_is_never_written_into_a_non_mp3(self):
        m4a = os.path.join(self.tmp, "song.m4a")
        Path(m4a).write_bytes(b"not an mp3")
        with self.assertRaises(ValueError):
            th.save_id3(ID3(), m4a)
        self.assertEqual(Path(m4a).read_bytes(), b"not an mp3")

    def test_an_invalid_value_keeps_the_old_frame(self):
        audio = ID3()
        audio.add(TRCK(encoding=3, text=["5"]))
        with patch.object(th, "create_frame", lambda *a: None):
            self.assertFalse(th.apply_bulk_edit(audio, "TRCK", "set", "abc"))
        self.assertEqual(str(audio["TRCK"]), "5")

    def test_rename_onto_an_existing_frame_is_refused(self):
        audio = ID3()
        audio.add(TIT1(encoding=3, text=["group"]))
        audio.add(TIT2(encoding=3, text=["title"]))
        old = audio.pop("TIT1")
        self.assertFalse(th.rename_frame(audio, old, "TIT2"))
        self.assertEqual(str(audio["TIT2"]), "title")

    def test_saving_lyrics_keeps_other_sylt_frames(self):
        from src.lyrics.lyrics import save_sylt_entries
        path = self.mp3("l.mp3", SYLT(encoding=3, lang="fra", desc="", format=2, type=1,
                                      text=[("bonjour", 0)]))
        with patch("src.lyrics.lyrics.ui_utils.show_status"):
            save_sylt_entries(path, [("hello", 0)], desc="", lang="eng")
        langs = sorted(f.lang for f in ID3(path).getall("SYLT"))
        self.assertEqual(langs, ["eng", "fra"])


class ConfigSaveTest(_Tmp):
    def test_update_config_keeps_a_change_made_meanwhile(self):
        with patch.object(cfg, "CONFIG_DIR", Path(self.tmp)), \
             patch.object(cfg, "CONFIG_FILE", Path(self.tmp) / "config.json"):
            stale = cfg.load_config()                         # a screen's copy
            cfg.update_config({"volume": 40})                 # the player, meanwhile
            before = dict(stale)
            stale["sort_use_tags"] = False                    # the screen's own edit
            cfg.update_config(cfg.changed_keys(before, stale))
            fresh = cfg.load_config()
        self.assertEqual(fresh["volume"], 40)
        self.assertFalse(fresh["sort_use_tags"])

    def test_a_crash_mid_write_leaves_the_old_file(self):
        from backbone.files import write_text_atomic
        path = os.path.join(self.tmp, "x.json")
        write_text_atomic(path, "old")
        with patch("os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                write_text_atomic(path, "new")
        self.assertEqual(Path(path).read_text(), "old")
        self.assertEqual(os.listdir(self.tmp), ["x.json"])            # no temp left behind


if __name__ == "__main__":
    unittest.main()
