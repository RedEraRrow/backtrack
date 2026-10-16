"""W in the lyric editor writes the transcript in the schema it already has:
a plain Whisper one stays plain, a richer one keeps its stage directions."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from backtrack.lyrics.editor import _Session


def _commit(container: dict, segs: list) -> dict:
    d = tempfile.mkdtemp()
    try:
        jpath = os.path.join(d, 'ep.json')
        with open(jpath, 'w') as fh:
            json.dump(container, fh)
        fake = SimpleNamespace(segs=segs,
                               aux={'jpath': jpath, 'meta': {}, 'sidecar': os.path.join(d, 'ep.sync.json')})
        _Session.do_commit(fake)
        with open(jpath) as fh:
            return json.load(fh)
    finally:
        shutil.rmtree(d)


WORDS = [{'word': 'Hello.', 'start': 1.0, 'end': 1.5, 'wid': 7}]


class CommitTest(unittest.TestCase):
    def test_a_rich_transcript_keeps_its_directions(self):
        segs = [{'kind': 'stage_dir', 'text': 'Door opens.', 'start': 0.0, 'end': 1.0, 'bid': 2},
                {'kind': 'dialogue', 'text': 'Hello.', 'start': 1.0, 'end': 1.5, 'line_ref': 3, 'words': WORDS},
                {'kind': 'credit', 'text': 'Music by: X'}]
        out = _commit({'segments': [{'kind': 'dialogue', 'text': 'Hello.'}], 'direction_kinds': {}}, segs)
        self.assertEqual([s['kind'] for s in out['segments']], ['stage_dir', 'dialogue'])   # no credit line
        self.assertEqual(out['segments'][1]['line_ref'], 3)
        self.assertNotIn('bid', out['segments'][0])
        self.assertNotIn('wid', out['segments'][1]['words'][0])
        self.assertNotIn('word_segments', out)

    def test_a_plain_whisper_transcript_stays_plain(self):
        segs = [{'kind': 'stage_dir', 'text': 'Door opens.', 'start': 0.0, 'end': 1.0},
                {'text': 'Hello.', 'start': 1.0, 'end': 1.5, 'words': WORDS}]
        out = _commit({'segments': [{'text': 'Hello.', 'start': 1.0, 'end': 1.5}]}, segs)
        self.assertEqual([s['text'] for s in out['segments']], ['Hello.'])
        self.assertNotIn('kind', out['segments'][0])
        self.assertEqual(out['word_segments'][0]['word'], 'Hello.')


if __name__ == '__main__':
    unittest.main()
