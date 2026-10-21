"""One key handler for the player, whether this window plays the audio or
joins another's: each key does the same to the session it's given, and says
what the loop should follow up with."""
import unittest
from unittest import mock

from backtrack.playback import player, player_ui


class _Session:
    def __init__(self, next_result='b.mp3'):
        self.calls = []
        self._next = next_result

    def pause_toggle(self): self.calls.append('pause')
    def seek(self, s): self.calls.append(('seek', s))
    def seek_to(self, s): self.calls.append(('seek_to', s))
    def next(self, manual=True): self.calls.append('next'); return self._next
    def prev(self): self.calls.append('prev'); return 'a.mp3'
    def stop(self): self.calls.append('stop')
    def edit(self, op, **kw): self.calls.append((op, kw.get('pos')))


def _v(session, joined=False):
    return player._Input(session=session, joined=joined, elapsed=30.0, duration=100.0, rate=1.0,
                         chapters=[], ctrl_row=10, queue=['a.mp3', 'b.mp3'], index=0,
                         path='a.mp3', audio=None, has_lyrics=False)


class PlayerInputTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(player_ui, 'arrange_key', lambda k: False),
                  mock.patch.object(player, '_tab_asked', lambda k: None),
                  mock.patch.object(player, 'has_other_windows', lambda: False),
                  mock.patch.object(player.time, 'sleep', lambda s: None),
                  mock.patch.dict(player_ui._ui_state, show_queue=False),
                  mock.patch('backbone.keys._overrides', lambda: {})):         # the default keys, not the user's
            p.start()
            self.addCleanup(p.stop)

    def press(self, key, **kw):
        s = _Session(**{k: v for k, v in kw.items() if k == 'next_result'})
        return player._player_input(key, _v(s, kw.get('joined', False))), s.calls

    def test_the_same_keys_drive_either_session(self):
        for joined in (False, True):
            self.assertEqual(self.press('SPACE', joined=joined), ('ctrl', ['pause']))
            self.assertEqual(self.press('RIGHT', joined=joined)[1], [('seek', 5.0)])
            self.assertEqual(self.press(']', joined=joined), ('track', ['next']))
            self.assertEqual(self.press('s', joined=joined), ({"status": "STOP"}, ['stop']))
            self.assertEqual(self.press('b', joined=joined)[0], {"status": "DETACH"})

    def test_next_off_the_end_leaves_only_the_window_playing(self):
        self.assertEqual(self.press(']', next_result=None)[0], {"status": "OK"})
        self.assertEqual(self.press(']', next_result=None, joined=True)[0], 'track')

    def test_back_stays_while_another_window_is_attached(self):
        with mock.patch.object(player, 'has_other_windows', lambda: True):
            self.assertEqual(self.press('b')[0], 'ctrl')
            self.assertEqual(self.press('s', joined=True), ('redraw', ['stop']))


if __name__ == '__main__':
    unittest.main()
