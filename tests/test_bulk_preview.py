"""The tidy operations' shared preview: only ticked rows that change are written,
so ticking a "keeps disc 2" row can't strip that disc number."""
import unittest
from unittest.mock import patch

from src.id3 import bulk_common, bulk_ops as bo


class PreviewAndApplyTest(unittest.TestCase):
    def _run(self, ticked):
        plan = bo.Plan(changes=[bo.Change('/m/a.mp3', why='disc 1/1 → —'),
                                bo.Change('/m/b.mp3')])
        written, status = [], []
        with patch.object(bulk_common.prompt, 'select', lambda *a, **k: ticked), \
             patch('src.utils.ui_utils.show_status', lambda m, **k: status.append(m)), \
             patch.object(bo, 'refresh_library_entry', lambda *a: None):
            bulk_common.preview_and_apply(
                plan, [], lambda sub: (lambda: [sub]), lambda c: written.append(c.path),
                "Removed the disc number from", count="2 tracks", changing="with 1/1",
                unchanged=lambda c: "keeps disc 2")
        return written, status

    def test_only_changing_rows_are_written(self):
        written, status = self._run(['/m/a.mp3', '/m/b.mp3'])
        self.assertEqual(written, ['/m/a.mp3'])
        self.assertIn("1 file", status[-1])

    def test_ticking_only_unchanged_rows_writes_nothing(self):
        written, status = self._run(['/m/b.mp3'])
        self.assertEqual(written, [])
        self.assertEqual(status, ["No tracks selected."])

    def test_back_writes_nothing(self):
        self.assertEqual(self._run(None), ([], []))


if __name__ == "__main__":
    unittest.main()
