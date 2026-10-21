"""Tests for the PDFScript.pyw window. They run the real OCR on test_sample.pdf,
which holds 14 matches for "confidential" / "do not share this" on 3 pages.

    python -m unittest -v
"""

import hashlib
import tempfile
import tkinter as tk
import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from unittest import mock

import remove_text

HERE = Path(__file__).parent
SAMPLE = HERE / "test_sample.pdf"
TARGETS = ["confidential", "do not share this"]


def load_window_module():
    loader = SourceFileLoader("pdfscript_window", str(HERE / "PDFScript.pyw"))
    module = module_from_spec(spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


gui = load_window_module()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class RunJob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = gui.engine_for(None)

    def run_job(self, out_dir, targets=TARGETS, **options):
        return gui.run_job([SAMPLE], targets, out_dir, gui.make_options(**options), self.engine,
                           log=lambda line: None, progress=lambda name, page, count: None)

    def test_remove_writes_a_cleaned_copy_with_the_text_gone(self):
        before = sha256(SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            total, written = self.run_job(tmp)
            self.assertEqual(total, 14)
            self.assertEqual(written, [Path(tmp) / "test_sample_cleaned.pdf"])
            left = remove_text.clean_pdf(written[0], None, TARGETS, self.engine, gui.make_options(dry_run=True),
                                         log=lambda line: None)
            self.assertEqual(left, 0)
        self.assertEqual(sha256(SAMPLE), before, "the original PDF must not change")

    def test_preview_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            total, written = self.run_job(tmp, dry_run=True)
            self.assertEqual((total, written), (14, []))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_exact_turns_off_ocr_mistake_tolerance(self):
        # "confidentiel" is one letter away from "confidential", which only the tolerance forgives.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_job(tmp, ["confidentiel"], dry_run=True)[0], 8)
            self.assertEqual(self.run_job(tmp, ["confidentiel"], dry_run=True, exact=True)[0], 0)


class Options(unittest.TestCase):
    def test_fill_labels_map_to_colours(self):
        cases = {"Auto (match background)": None, "White": (1, 1, 1), "Black": (0, 0, 0)}
        for label, colour in cases.items():
            with self.subTest(label):
                self.assertEqual(gui.make_options(fill=label).fill, colour)

    def test_ocr_engine_is_loaded_once_per_language(self):
        self.assertIs(gui.engine_for(None), gui.engine_for(None))


class MissingPackage(unittest.TestCase):
    def test_missing_remove_text_says_to_keep_the_files_together(self):
        message = gui.missing_package_message(ModuleNotFoundError("No module named 'remove_text'", name="remove_text"))
        self.assertIn("same folder", message)
        self.assertNotIn("pip install", message)

    def test_missing_library_says_to_install_the_requirements(self):
        message = gui.missing_package_message(ModuleNotFoundError("No module named 'pymupdf'", name="pymupdf"))
        self.assertIn("pip install -r requirements.txt", message)


class Prepare(unittest.TestCase):
    def test_valid_input_gives_pdfs_and_trimmed_targets(self):
        pdfs, targets = gui.prepare(f'"{SAMPLE}"', "confidential\n\n   do not share this  \n")
        self.assertEqual(pdfs, [SAMPLE])
        self.assertEqual(targets, ["confidential", "do not share this"])

    def test_bad_input_is_explained(self):
        with tempfile.TemporaryDirectory() as tmp:
            not_pdf = Path(tmp) / "notes.txt"
            not_pdf.write_text("hi")
            empty_folder = Path(tmp) / "empty"
            empty_folder.mkdir()
            cases = [
                ("", "x", "Choose a PDF"),
                (str(Path(tmp) / "missing.pdf"), "x", "Can't find"),
                (str(not_pdf), "x", "isn't a PDF"),
                (str(empty_folder), "x", "No PDF files"),
                (str(SAMPLE), "  \n \n", "Type at least one"),
            ]
            for path, text, message in cases:
                with self.subTest(message):
                    with self.assertRaisesRegex(ValueError, message):
                        gui.prepare(path, text)


class Window(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = gui.App(self.root)

    def tearDown(self):
        self.root.destroy()

    def wait_until_done(self, seconds=180):
        for _ in range(seconds * 20):
            self.root.update()
            if not self.app.running:
                return
            self.root.after(50)
        self.fail("the job did not finish")

    def fill_in(self, pdf, out_dir):
        self.app.path.set(str(pdf))
        self.app.text.insert("1.0", "confidential\ndo not share this")
        self.app.out_dir.set(out_dir)

    def test_preview_button_shows_matches_in_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fill_in(SAMPLE, tmp)
            self.app.preview_button.invoke()
            self.assertEqual(str(self.app.remove_button["state"]), "disabled")
            self.wait_until_done()
            log = self.app.log_box.get("1.0", "end")
            self.assertIn("page 2/3: would remove 4", log)
            self.assertIn("14 match(es) found, nothing written.", log)
            self.assertEqual(float(self.app.progress["value"]), 100.0)
            self.assertEqual(str(self.app.remove_button["state"]), "normal")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_remove_button_saves_the_copy_and_enables_open_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.fill_in(SAMPLE, tmp)
            self.app.remove_button.invoke()
            self.wait_until_done()
            self.assertTrue((Path(tmp) / "test_sample_cleaned.pdf").is_file())
            self.assertEqual(self.app.output_folder, Path(tmp))
            self.assertEqual(str(self.app.open_button["state"]), "normal")

    def test_unreadable_pdf_shows_an_error_and_unlocks_the_window(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gui.messagebox, "showerror") as showerror:
            broken = Path(tmp) / "broken.pdf"
            broken.write_bytes(b"this is not a pdf")
            self.fill_in(broken, tmp)
            self.app.remove_button.invoke()
            self.wait_until_done()
            showerror.assert_called_once()
            self.assertEqual(self.app.status.get(), "Stopped because of an error.")
            self.assertEqual(str(self.app.remove_button["state"]), "normal")
            self.assertEqual(str(self.app.open_button["state"]), "disabled")


if __name__ == "__main__":
    unittest.main()
