"""The lyrics editor driven by keys, end to end: session, key handlers and
screen wired together, checked by what it saves."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from mutagen.id3 import ID3, SYLT

import backtrack.lyrics.editor_keys as keys_mod
import backtrack.lyrics.editor as le
from backbone import ui


def _word(w, s, e):
    return {'word': w, 'start': s, 'end': e}


SEGS = [
    {'text': 'Hello there', 'start': 1.0, 'end': 2.0, 'words': [_word('Hello', 1.0, 1.4), _word('there', 1.5, 2.0)]},
    {'text': 'How are you', 'start': 3.0, 'end': 4.0, 'words': [_word('How', 3.0, 3.3), _word('are', 3.4, 3.6), _word('you', 3.7, 4.0)]},
    {'text': 'Goodbye', 'start': 5.0, 'end': 6.0, 'words': [_word('Goodbye', 5.0, 6.0)]},
]


class _Screen:
    row = 1
    def __init__(self, fd): pass
    def render(self, lines): pass
    def clear(self): pass
    def anchor_reset(self): pass


def run_editor(keys, answers=(), sylt=False):
    """Run the editor on a fresh track with scripted keys and prompt answers;
    returns (the saved working copy or None, the track's SYLT text, status lines)."""
    d = tempfile.mkdtemp()
    mp3 = os.path.join(d, 'Ep.mp3')
    with open(mp3, 'wb') as f:
        f.write(b'\xff\xfb\x90\x00' * 64)
    if sylt:
        tags = ID3()
        tags.add(SYLT(encoding=3, lang='eng', desc='', format=2, type=1,
                      text=[(s['text'], int(s['start'] * 1000)) for s in SEGS]))
        tags.save(mp3)
    else:
        with open(os.path.join(d, 'Ep.json'), 'w') as f:
            json.dump({'segments': SEGS}, f)
    feed, replies, status = iter(keys), iter(answers), []

    def read_key(fd):
        return next(feed)
    fakes = {'_read_key': read_key, '_wait_for_keypress': lambda t: True, '_set_raw': lambda fd: None,
             '_restore_term_attrs': lambda fd, o: None, '_get_term_attrs': lambda fd: None,
             '_Widget': _Screen, '_prompt_text': lambda *a, **k: next(replies, '')}
    real = sys.stdout
    with ExitStack() as stack:
        for mod in (le, keys_mod):
            for name, fake in fakes.items():
                if hasattr(mod, name):
                    stack.enter_context(patch.object(mod, name, fake))
        stack.enter_context(patch.object(le, '_vlc', None))
        stack.enter_context(patch.object(sys.stdin, 'fileno', lambda: 0))
        stack.enter_context(patch.object(ui, 'consume_resize', lambda: False))
        stack.enter_context(patch.object(ui, 'show_status', lambda m, **k: status.append(m)))
        sys.stdout = io.StringIO()
        try:
            le.lyrics_editor(mp3)
        finally:
            sys.stdout = real
    side = os.path.join(d, 'Ep.sync.json')
    saved = None
    if os.path.exists(side):
        with open(side) as f:
            saved = json.load(f)['segments']
    frames = ID3(mp3).getall('SYLT') if sylt else []
    return saved, (frames[0].text if frames else None), status


class LyricsEditorKeysTest(unittest.TestCase):
    def test_nudge_moves_the_line_and_s_saves(self):
        saved, _, _ = run_editor(['DOWN', 'LEFT', 's', 'ESC'])
        self.assertEqual(saved[1]['start'], 2.75)          # ← is −0.25 s
        self.assertEqual(saved[0]['start'], 1.0)

    def test_j_k_reorder_and_undo(self):
        saved, _, _ = run_editor(['DOWN', 'J', 's', 'ESC'])
        self.assertEqual([s['text'] for s in saved], ['How are you', 'Hello there', 'Goodbye'])
        saved, _, _ = run_editor(['K', 'u', 's', 'ESC'])
        self.assertEqual([s['text'] for s in saved], ['Hello there', 'How are you', 'Goodbye'])

    def test_unsaved_changes_are_asked_about(self):
        saved, _, _ = run_editor(['LEFT', 'ESC', 'ESC'], answers=['n', 'y'])   # first Esc refused
        self.assertIsNone(saved)                            # quit without saving

    def test_q_quits_the_app_and_esc_goes_back(self):
        from backbone.nav import QuitToTerminal
        with self.assertRaises(QuitToTerminal):
            run_editor(['DOWN', 'q'])
        saved, _, _ = run_editor(['DOWN', 'LEFT', 'ESC', 'ESC'], answers=['y'])  # leave without saving
        self.assertIsNone(saved)

    def test_timestamp_editor(self):
        saved, _, _ = run_editor(['e', 'TAB', '0', '9', 'ENTER', 's', 'ESC'])   # tab to seconds
        self.assertAlmostEqual(saved[0]['start'], 9.0)

    def test_sylt_track_saves_back_to_its_tag(self):
        _, sylt, status = run_editor(['DOWN', 'RIGHT', 's', 'ESC'], sylt=True)
        self.assertEqual(sylt[1], ('How are you', 3250))
        self.assertTrue(any(s.startswith('Saved') for s in status), status)


if __name__ == "__main__":
    unittest.main()
