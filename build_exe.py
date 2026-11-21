"""Build the Windows app: dist/PDFScript/PDFScript.exe, zipped to dist/PDFScript-windows.zip.

Run it from a clean virtual environment so only what the app needs is bundled:
    python -m venv .venv
    .venv\\Scripts\\activate
    pip install -r requirements.txt pyinstaller
    python build_exe.py

Upload dist/PDFScript-windows.zip to a GitHub Release. Users unzip it and
double-click PDFScript.exe; they don't need Python.
"""

import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent

# Optional engines and extras RapidOCR or PyMuPDF can use but this app doesn't.
EXCLUDE = ["torch", "paddle", "openvino", "tensorrt", "pymupdf4llm", "matplotlib", "pandas", "IPython"]

# Bundled files the app never touches, relative to the _internal folder.
PRUNE = ["pymupdf/layout", "pymupdf/mupdf-devel", "cv2/opencv_videoio_ffmpeg*.dll"]


def build(entry, name, windowed=True, dist=ROOT / "dist", work=ROOT / "build"):
    """Bundle `entry` into dist/<name>/<name>.exe and return that folder."""
    PyInstaller.__main__.run([
        str(entry),
        "--name", name,
        "--windowed" if windowed else "--console",
        "--onedir",            # one folder: starts fast, and OCR languages downloaded later are kept
        "--noconfirm",
        "--clean",
        "--distpath", str(dist),
        "--workpath", str(work),
        "--specpath", str(work),
        "--collect-data", "rapidocr",          # config files and the bundled OCR models
        "--collect-submodules", "rapidocr",    # the inference engine is picked at run time
        *[arg for module in EXCLUDE for arg in ("--exclude-module", module)],
    ])
    app = Path(dist) / name
    internal = app / "_internal"
    for pattern in PRUNE:
        for path in internal.glob(pattern):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    if sys.platform == "win32" and not (app / f"{name}.exe").exists():
        sys.exit(f"Build failed: {name}.exe was not created.")
    return app


if __name__ == "__main__":
    app = build(ROOT / "PDFScript.pyw", "PDFScript")
    archive = shutil.make_archive(str(app.parent / "PDFScript-windows"), "zip", app.parent, app.name)
    print(f"\nApp:     {app}\nArchive: {archive}")
