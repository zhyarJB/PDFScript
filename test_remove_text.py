"""Tests for remove_text.py. They run the real OCR on test_sample.pdf, which holds
14 matches for "confidential" / "do not share this" on 3 pages (5, 4 and 5).

    python -m unittest -v
"""

import argparse
import contextlib
import io
import unittest
from pathlib import Path

import remove_text

SAMPLE = Path(__file__).parent / "test_sample.pdf"
TARGETS = ["confidential", "do not share this"]


def dry_run_options():
    return argparse.Namespace(similarity=0.85, fill=None, dpi=None, passes=2, dry_run=True, show_text=False)


class CleanPdfCallbacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = remove_text.make_engine(None)

    def test_output_goes_to_log_instead_of_stdout(self):
        lines = []
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            total = remove_text.clean_pdf(SAMPLE, None, TARGETS, self.engine, dry_run_options(), log=lines.append)
        self.assertEqual(total, 14)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("  page 1/3: would remove 5", lines)
        self.assertIn("  page 3/3: would remove 5", lines)

    def test_progress_is_reported_after_each_page(self):
        calls = []
        remove_text.clean_pdf(SAMPLE, None, TARGETS, self.engine, dry_run_options(),
                              log=lambda line: None, progress=lambda page, count: calls.append((page, count)))
        self.assertEqual(calls, [(1, 3), (2, 3), (3, 3)])


if __name__ == "__main__":
    unittest.main()
