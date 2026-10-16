"""Lyrics to and from files (lyrics.files) and where the player takes them from
(lyric_pane.timeline_for, by the lyrics_source setting)."""
import os
import shutil
import tempfile
import unittest

from mutagen.id3 import ID3, SYLT, USLT

from backtrack.lyrics import files as lf
from backtrack.lyrics import lyric_pane


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_each_kind_of_file_reads_as_lines(self):
        lrc = self.write('a.lrc', "[ar:Someone]\n[00:12.00]First line\n[00:15.50]Second\n")
        self.assertEqual(lf.read_file(lrc), [("First line", 12000), ("Second", 15500)])
        srt = self.write('a.srt', "1\r\n00:00:12,000 --> 00:00:15,000\r\nFirst\r\nline\r\n\r\n2\r\n00:00:15,500 --> 00:00:18,000\r\nSecond\r\n")
        self.assertEqual(lf.read_file(srt), [("First line", 12000), ("Second", 15500)])
        txt = self.write('a.txt', "Just the words\r\n\r\nand more\r\n")
        self.assertEqual(lf.read_file(txt), [("Just the words", None), ("and more", None)])
        timed_txt = self.write('b.txt', "[00:01.00]Timed after all\n")
        self.assertEqual(lf.read_file(timed_txt), [("Timed after all", 1000)])

    def test_subtitles_and_whisper_json_read_as_lines(self):
        vtt = self.write('a.vtt', "WEBVTT\n\nNOTE made by hand\n\n00:12.000 --> 00:15.000\n<v Singer>First</v> line\n\n"
                                  "00:00:15.500 --> 00:00:18.000 align:start\nSecond\n")
        self.assertEqual(lf.read_file(vtt), [("First line", 12000), ("Second", 15500)])
        ass = self.write('a.ass', "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                                  "Dialogue: 0,0:00:15.50,0:00:18.00,Default,,0,0,0,,{\\i1}Second{\\i0}\n"
                                  "Dialogue: 0,0:00:12.00,0:00:15.00,Default,,0,0,0,,First\\Nline, with a comma\n")
        self.assertEqual(lf.read_file(ass), [("First line, with a comma", 12000), ("Second", 15500)])
        whisper = self.write('w.json', '{"text": "x", "segments": [{"start": 12.0, "end": 15.0, "text": " First line"},'
                                       ' {"start": 14.0, "end": 15.0, "text": "", "kind": "dead_air"},'
                                       ' {"start": 15.5, "end": 18.0, "text": " Second"}]}')
        self.assertEqual(lf.read_file(whisper), [("First line", 12000), ("Second", 15500)])
        words = self.write('x.json', '{"word_segments": [{"word": "First", "start": 12.0}, {"word": "line.", "start": 12.4},'
                                     ' {"word": "Second", "start": 15.5}]}')
        self.assertEqual(lf.read_file(words), [("First line.", 12000), ("Second", 15500)])
        mac = self.write('m.json', '[{"text": "First line", "timestamp": "00:00:12,000 --> 00:00:15,000"}]')
        self.assertEqual(lf.read_file(mac), [("First line", 12000)])
        self.assertEqual(lf.read_file(self.write('n.json', '{"title": "not lyrics"}')), [])

    def test_what_is_written_reads_back_the_same(self):
        rows = [("First line", 12000), ("Second", 15500)]
        for fmt in ('lrc', 'srt', 'vtt', 'json'):
            path = self.write(f'r.{fmt}', lf.render(rows, fmt))
            self.assertEqual(lf.read_file(path), rows, fmt)
        path = self.write('r.txt', lf.render(rows, 'txt'))
        self.assertEqual(lf.read_file(path), [("First line", None), ("Second", None)])

    def test_the_file_beside_a_track_is_lrc_first(self):
        track = self.write('song.mp3', '')
        self.assertIsNone(lf.sidecar(track))
        self.write('song.txt', 'words')
        self.assertTrue(lf.sidecar(track).endswith('song.txt'))
        self.write('song.lrc', '[00:01.00]words')
        self.assertTrue(lf.sidecar(track).endswith('song.lrc'))


class DialogueRowsTest(unittest.TestCase):
    """A dialogue track exported: speakers and manner named when they change,
    directions as bracketed lines, markdown dropped."""

    def test_speakers_manner_and_directions(self):
        d = tempfile.mkdtemp()
        md = os.path.join(d, 'ep.md')
        with open(md, 'w') as fh:
            fh.write("**MARTIN** *(angrily)*: Stop it. Right *now*.\n*(Door opens.)*\n**ARTHUR**: Hello!\n")
        segs = []
        t = 0.0
        for text in ("Stop it.", "Right now.", "Hello!"):
            ws = text.split()
            segs.append({'text': text, 'start': t, 'end': t + len(ws) * 0.5,
                         'words': [{'word': w, 'start': t + i * 0.5, 'end': t + i * 0.5 + 0.5} for i, w in enumerate(ws)]})
            t += len(ws) * 0.5 + (3.0 if text == "Right now." else 0.0)
        rows = lf.dialogue_rows(segs, md)
        shutil.rmtree(d)
        self.assertEqual([r for r, _ms in rows],
                         ["MARTIN (angrily): Stop it.", "Right now.", "(Door opens.)", "ARTHUR: Hello!"])
        self.assertEqual(rows[0][1], 0)

class TimelineForTest(unittest.TestCase):
    """Which lyrics the player takes for each lyrics_source setting."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.track = os.path.join(self.dir, 'song.mp3')
        open(self.track, 'w').close()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def tags(self, synced=False, words=False):
        audio = ID3()
        if synced:
            audio.add(SYLT(encoding=3, lang='eng', desc='', format=2, type=1, text=[("Tagged synced", 1000)]))
        if words:
            audio.add(USLT(encoding=3, lang='eng', desc='', text="Tagged words"))
        return audio

    def beside(self, text):
        with open(os.path.join(self.dir, 'song.lrc'), 'w') as f:
            f.write(text)

    def source(self, audio, setting):
        return lyric_pane.timeline_for(self.track, audio, 180.0, setting)[1]

    def test_best_there_is_puts_tagged_synced_before_a_file(self):
        self.beside("[00:02.00]File synced")
        self.assertEqual(self.source(self.tags(synced=True), 'auto'), ['embedded SYLT'])
        self.assertEqual(self.source(self.tags(words=True), 'auto'), ['song.lrc'])   # timed beats paced

    def test_files_first_puts_the_file_before_the_tags(self):
        self.beside("[00:02.00]File synced")
        self.assertEqual(self.source(self.tags(synced=True), 'files'), ['song.lrc'])

    def test_tags_only_ignores_files(self):
        self.beside("[00:02.00]File synced")
        self.assertEqual(self.source(self.tags(words=True), 'embedded'), ['embedded USLT'])
        self.assertEqual(self.source(self.tags(), 'embedded'), [])

    def test_synced_only_never_paces_out_words(self):
        self.assertEqual(self.source(self.tags(words=True), 'synced'), [])
        self.beside("Just words in a file")
        self.assertEqual(self.source(self.tags(words=True), 'synced'), [])
        self.assertEqual(self.source(self.tags(synced=True), 'synced'), ['embedded SYLT'])


if __name__ == '__main__':
    unittest.main()
