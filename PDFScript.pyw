"""PDFScript window: delete words or sentences from every page of a PDF, without the terminal.

Double-click this file to open it. Python and the packages in requirements.txt
must be installed first:
    pip install -r requirements.txt

The OCR and erasing are done by remove_text.py; this file is only the window.
"""

import argparse
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

# pythonw.exe (used for .pyw files) has no console, so libraries that print would crash.
for _name in ("stdout", "stderr"):
    if getattr(sys, _name) is None:
        setattr(sys, _name, open(os.devnull, "w"))


def missing_package_message(error):
    if getattr(error, "name", None) == "remove_text":
        return "remove_text.py must be in the same folder as PDFScript.pyw."
    return (f"A required package is missing:\n\n{error}\n\n"
            "Open a terminal in this folder and run:\n    pip install -r requirements.txt")


try:
    import remove_text
except (ImportError, SystemExit) as error:  # remove_text exits when RapidOCR is missing
    tk.Tk().withdraw()
    messagebox.showerror("PDFScript", missing_package_message(error))
    sys.exit(1)

FILLS = {"Auto (match background)": "auto", "White": "white", "Black": "black"}
LANGUAGE_NAMES = {
    "en": "English", "latin": "European (Latin alphabet)", "arabic": "Arabic script", "cyrillic": "Cyrillic",
    "eslav": "Russian, Ukrainian, Belarusian", "el": "Greek", "devanagari": "Devanagari (Hindi...)",
    "ta": "Tamil", "te": "Telugu", "th": "Thai", "korean": "Korean", "ch": "Chinese (Simplified)",
    "japan": "Japanese", "ka": "Kannada", "chinese_cht": "Chinese (Traditional)",
}
LANGUAGES = {"Default": None} | {LANGUAGE_NAMES.get(code, code): code for code in remove_text.LANGS}

_engines = {}


def engine_for(lang):
    """Load the OCR engine for `lang` the first time it's needed, then reuse it."""
    if lang not in _engines:
        _engines[lang] = remove_text.make_engine(lang)
    return _engines[lang]


def make_options(exact=False, fill="Auto (match background)", dry_run=False):
    """The settings clean_pdf reads; the ones the window doesn't show keep the command line's defaults."""
    return argparse.Namespace(similarity=1.0 if exact else 0.85, fill=remove_text.parse_colour(FILLS[fill]),
                              dpi=None, passes=2, dry_run=dry_run, show_text=False)


def prepare(path_text, targets_text):
    """Check what was typed in the window. Returns (pdfs, targets), or raises ValueError saying what's wrong."""
    path_text = path_text.strip().strip("\"'")  # "Copy as path" in Explorer adds quotes
    if not path_text:
        raise ValueError("Choose a PDF file or folder first.")
    path = Path(path_text)
    if not path.exists():
        raise ValueError(f"Can't find {path}")
    if path.is_file() and path.suffix.lower() != ".pdf":
        raise ValueError(f"{path.name} isn't a PDF file.")
    targets = [line.strip() for line in targets_text.splitlines() if line.strip()]
    if not targets:
        raise ValueError("Type at least one word or sentence to delete.")
    pdfs = remove_text.collect_pdfs([path_text])
    if not pdfs:
        raise ValueError(f"No PDF files found in {path}")
    return pdfs, targets


def run_job(pdfs, targets, out_dir, options, engine, log, progress):
    """Clean every PDF, or with options.dry_run only list the matches. Returns (matches, files written).

    `progress(file_name, page, page_count)` is called after each page.
    """
    total, written = 0, []
    for src in pdfs:
        dst = remove_text.output_path(src, out_dir, len(pdfs) > 1)
        log(str(src))
        found = remove_text.clean_pdf(src, dst, targets, engine, options, log,
                                      lambda page, count: progress(src.name, page, count))
        total += found
        if options.dry_run:
            log(f"  {found} match(es) found, nothing written.")
        else:
            log(f"  {found} removed -> {dst}")
            written.append(dst)
    return total, written


def explain(error):
    if isinstance(error, PermissionError):
        return f"Can't write {error.filename}.\n\nIf it's open in another program, close it and try again."
    return str(error) or type(error).__name__


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class App:
    """The window. The work runs in a background thread and reports back through `self.messages`."""

    def __init__(self, root):
        self.root = root
        self.messages = queue.Queue()
        self.running = False
        self.output_folder = None

        root.title("PDFScript - remove text from PDFs")
        root.minsize(640, 560)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="PDF file or folder").grid(row=0, column=0, sticky="w")
        self.path = tk.StringVar()
        ttk.Entry(frame, textvariable=self.path).grid(row=0, column=1, sticky="ew", padx=6)
        pick = ttk.Frame(frame)
        pick.grid(row=0, column=2, sticky="e")
        ttk.Button(pick, text="File...", command=self.choose_file).pack(side="left")
        ttk.Button(pick, text="Folder...", command=self.choose_folder).pack(side="left", padx=(4, 0))

        ttk.Label(frame, text="Words or sentences to delete, one per line").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(12, 2))
        self.text = tk.Text(frame, height=5, wrap="word", undo=True, font="TkTextFont")
        self.text.grid(row=2, column=0, columnspan=3, sticky="nsew")

        ttk.Label(frame, text="Save to folder").grid(row=3, column=0, sticky="w", pady=(12, 0))
        self.out_dir = tk.StringVar(value=str(remove_text.OUTPUT_DIR))
        ttk.Entry(frame, textvariable=self.out_dir).grid(row=3, column=1, sticky="ew", padx=6, pady=(12, 0))
        ttk.Button(frame, text="Browse...", command=self.choose_out_dir).grid(
            row=3, column=2, sticky="ew", pady=(12, 0))

        options = ttk.Frame(frame)
        options.grid(row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))
        self.exact = tk.BooleanVar()
        ttk.Checkbutton(options, text="Exact matches only", variable=self.exact).pack(side="left")
        ttk.Label(options, text="Fill colour").pack(side="left", padx=(16, 4))
        self.fill = tk.StringVar(value=next(iter(FILLS)))
        ttk.Combobox(options, textvariable=self.fill, values=list(FILLS), state="readonly", width=25).pack(side="left")
        ttk.Label(options, text="Language").pack(side="left", padx=(16, 4))
        self.language = tk.StringVar(value="Default")
        ttk.Combobox(options, textvariable=self.language, values=list(LANGUAGES), state="readonly",
                     width=26).pack(side="left")

        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        self.preview_button = ttk.Button(actions, text="Preview", command=lambda: self.start(dry_run=True))
        self.preview_button.pack(side="left")
        self.remove_button = ttk.Button(actions, text="Remove text", command=lambda: self.start(dry_run=False))
        self.remove_button.pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(frame, maximum=100)
        self.progress.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(12, 4))
        self.status = tk.StringVar(value="Choose a PDF, type what to delete, then press Preview or Remove text.")
        ttk.Label(frame, textvariable=self.status).grid(row=7, column=0, columnspan=3, sticky="w")
        self.log_box = scrolledtext.ScrolledText(frame, height=12, wrap="word", state="disabled")
        self.log_box.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
        frame.rowconfigure(2, weight=1)
        frame.rowconfigure(8, weight=3)

        self.open_button = ttk.Button(frame, text="Open output folder", command=self.open_output, state="disabled")
        self.open_button.grid(row=9, column=2, sticky="e", pady=(8, 0))

    def choose_file(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("PDF files", "*.pdf")])
        if path:
            self.path.set(path)

    def choose_folder(self):
        path = filedialog.askdirectory(parent=self.root, title="Folder of PDFs")
        if path:
            self.path.set(path)

    def choose_out_dir(self):
        path = filedialog.askdirectory(parent=self.root, title="Save cleaned PDFs to")
        if path:
            self.out_dir.set(path)

    def start(self, dry_run):
        try:
            pdfs, targets = prepare(self.path.get(), self.text.get("1.0", "end"))
        except ValueError as error:
            messagebox.showerror("PDFScript", str(error), parent=self.root)
            return
        out_dir = self.out_dir.get().strip().strip("\"'") or str(remove_text.OUTPUT_DIR)
        options = make_options(self.exact.get(), self.fill.get(), dry_run)
        lang = LANGUAGES[self.language.get()]

        self.set_running(True)
        self.log_box["state"] = "normal"
        self.log_box.delete("1.0", "end")
        self.log_box["state"] = "disabled"
        self.progress["value"] = 0
        self.open_button["state"] = "disabled"
        self.status.set("Loading OCR..." if lang not in _engines else "Working...")
        threading.Thread(target=self.work, args=(pdfs, targets, out_dir, options, lang), daemon=True).start()
        self.root.after(100, self.poll)

    def work(self, pdfs, targets, out_dir, options, lang):
        """Runs in the background thread: never touch the widgets here, only send messages."""
        send = self.messages.put
        try:
            if lang not in _engines:
                send(("log", "Loading OCR (a language's model is downloaded the first time it's used)..."))
            total, written = run_job(pdfs, targets, out_dir, options, engine_for(lang),
                                     log=lambda line: send(("log", line)),
                                     progress=lambda name, page, count: send(("progress", name, page, count)))
            send(("done", total, written, options.dry_run, None))
        except Exception as error:
            send(("done", 0, [], options.dry_run, error))

    def poll(self):
        while True:
            try:
                kind, *data = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.write_log(data[0])
            elif kind == "progress":
                name, page, count = data
                self.progress["value"] = 100 * page / count
                self.status.set(f"{name}: page {page} of {count}")
            elif kind == "done":
                self.finish(*data)
                return
        self.root.after(100, self.poll)

    def finish(self, total, written, dry_run, error):
        self.set_running(False)
        if error:
            self.status.set("Stopped because of an error.")
            self.write_log(f"Error: {error}")
            messagebox.showerror("PDFScript", explain(error), parent=self.root)
        elif dry_run:
            self.status.set(f"Preview: {total} match(es) found. Nothing was written.")
        else:
            self.status.set(f"Done: {total} removed, {len(written)} cleaned file(s) saved.")
            self.output_folder = written[0].parent
            self.open_button["state"] = "normal"

    def set_running(self, running):
        self.running = running
        for button in (self.preview_button, self.remove_button):
            button["state"] = "disabled" if running else "normal"

    def write_log(self, line):
        self.log_box["state"] = "normal"
        self.log_box.insert("end", line + "\n")
        self.log_box.see("end")
        self.log_box["state"] = "disabled"

    def open_output(self):
        if self.output_folder:
            open_folder(self.output_folder)

    def close(self):
        if self.running and not messagebox.askyesno(
                "PDFScript", "A PDF is still being cleaned. Quit anyway?", parent=self.root):
            return
        self.root.destroy()


def main():
    if sys.platform == "win32":
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    root.report_callback_exception = lambda *exc: messagebox.showerror(
        "PDFScript", "".join(traceback.format_exception(*exc)), parent=root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
