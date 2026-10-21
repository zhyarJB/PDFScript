"""Delete a word or sentence from every page of a PDF - made for screenshot / scanned PDFs.

The pages of a screenshot PDF are pictures, so there is no real text to search.
This script reads every page with OCR (RapidOCR), finds the words you ask for,
and erases them: the pixels under each match are painted over in the colour of
the background around them, inside the page image itself. Pages where something
was erased are read again to catch anything the first pass missed. Any text
layer in the PDF (real text, or the hidden text scanner apps add) is cleaned too.

Install once:
    pip install pymupdf rapidocr onnxruntime

Examples:
    python remove_text.py                                   # asks for the file and the text
    python remove_text.py report.pdf -t "Confidential"
    python remove_text.py report.pdf -t "do not share this" -t "John Doe"
    python remove_text.py my_pdfs_folder -t "Draft"         # every PDF in a folder
    python remove_text.py report.pdf -t "Draft" --dry-run   # only show what would be deleted
    python remove_text.py report.pdf --show-text --dry-run  # print what the OCR reads
"""

import argparse
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pymupdf

try:
    from rapidocr import LangRec, ModelType, OCRVersion, RapidOCR
except ImportError:
    sys.exit("RapidOCR is not installed. Run:  pip install rapidocr onnxruntime")

# Longest side, in pixels, a page is rendered at for OCR. Bigger reads small
# text better but needs more memory and time.
MAX_SIDE = 4000

# Characters OCR commonly mixes up; both sides are folded the same way before comparing.
CONFUSABLE = str.maketrans({"0": "o", "1": "l", "i": "l"})

# Languages that only have a PP-OCRv4 model; the rest use PP-OCRv5.
V4_ONLY_LANGS = {"japan", "ka", "chinese_cht"}
LANGS = ["en", "latin", "arabic", "cyrillic", "eslav", "el", "devanagari", "ta", "te",
         "th", "korean", "ch", "japan", "ka", "chinese_cht"]


@dataclass
class Word:
    text: str   # as read by the OCR (or from the text layer)
    key: str    # normalised form used for matching
    line: object  # identifies the line the word belongs to
    box: tuple  # (x0, y0, x1, y1): rendered-image pixels for OCR, page coordinates for the text layer
    vertical: bool  # the line runs top-to-bottom (e.g. a rotated page)


@dataclass
class Match:
    target: str
    words: list
    score: float
    in_text_layer: bool = False

    @property
    def text(self):
        return " ".join(w.text for w in self.words)


def normalise(text):
    """Lower-case and keep only letters and digits, so punctuation and spacing don't matter."""
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(ch for ch in text if ch.isalnum()).translate(CONFUSABLE)


def edit_distance(a, b, limit):
    """Levenshtein distance between a and b, giving up once it exceeds `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def find_matches(words, target, similarity):
    """Find every run of consecutive OCR words that reads like `target`.

    Words are glued together before comparing, so a phrase still matches when
    the OCR splits or merges words, and runs may continue onto the next line.
    `similarity` sets how many OCR mistakes are forgiven: 0.85 allows about one
    wrong letter per 7, and short words always have to match exactly.
    """
    goal = normalise(target)
    if not goal:
        return []
    allowed = int(len(goal) * (1 - similarity) + 1e-9)
    max_span = len(target.split()) + 3

    candidates = []
    for i, first in enumerate(words):
        if not first.key:
            continue
        joined = ""
        for j in range(i, min(len(words), i + max_span)):
            joined += words[j].key
            if len(joined) > len(goal) + allowed:
                break
            if len(joined) < len(goal) - allowed or not words[j].key:
                continue
            dist = edit_distance(joined, goal, allowed)
            if dist <= allowed:
                candidates.append((dist, j - i, i, j))

    # Best matches (fewest mistakes, fewest words) claim their words first.
    candidates.sort()
    used = set()
    matches = []
    for dist, _, i, j in candidates:
        span = set(range(i, j + 1))
        if span & used:
            continue
        used |= span
        matches.append((i, Match(target, words[i:j + 1], 1 - dist / len(goal))))
    return [m for _, m in sorted(matches, key=lambda pair: pair[0])]


def choose_dpi(page):
    """Render screenshots at their own resolution: one screenshot pixel per rendered pixel.

    Upscaling adds no detail and the OCR reads small text better at the original size.
    """
    page_area = abs(page.rect)
    native = 0
    for info in page.get_image_info():
        bbox = pymupdf.Rect(info["bbox"])
        if bbox.is_empty or abs(bbox) < 0.2 * page_area:
            continue  # ignore icons and logos
        native = max(native, max(info["width"], info["height"]) / (max(bbox.width, bbox.height) / 72))
    dpi = min(max(native or 200, 150), 600)
    longest = max(page.rect.width, page.rect.height) / 72 * dpi
    return dpi * min(1, MAX_SIDE / longest)


def render(page, dpi):
    """Render the page as it is displayed (rotation applied) to an RGB array."""
    zoom = dpi / 72
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)


def read_words(engine, rgb):
    """OCR the image and return its words in reading order."""
    result = engine(np.ascontiguousarray(rgb[:, :, ::-1]), return_word_box=True)  # RapidOCR wants BGR
    words = []
    for line_no, line in enumerate(result.word_results or ()):
        boxes = []
        for _text, _score, quad in line:
            xs = [p[0] for p in quad]
            ys = [p[1] for p in quad]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
        x0, y0, x1, y1 = union(boxes)
        vertical = (y1 - y0) > 1.5 * (x1 - x0)
        for (text, _score, _quad), box in zip(line, boxes):
            words.append(Word(text, normalise(text), line_no, box, vertical))
    return words


def union(boxes):
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def text_layer_matches(page, targets, similarity):
    """Find the targets in the PDF's own text layer.

    That's the real text on normal pages, or the invisible text scanner apps
    lay over screenshots to make them searchable: OCR can't see it, but search
    and copy-paste would still find it.
    """
    words = [Word(w[4], normalise(w[4]), (w[5], w[6]), pymupdf.Rect(w[:4]), False)
             for w in page.get_text("words")]
    matches = [m for target in targets for m in find_matches(words, target, similarity)]
    for match in matches:
        match.in_text_layer = True
    return matches


def remove_text_layer_matches(page, matches):
    """Delete exactly the matched characters; images and drawings are left alone."""
    for match in matches:
        for word in match.words:
            page.add_redact_annot(word.box, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)


def match_boxes(match):
    """One pixel box per line the match covers, so the gaps between its words go too."""
    lines = {}
    for word in match.words:
        lines.setdefault(word.line, []).append(word)
    return [(union([w.box for w in words]), words[0].vertical) for words in lines.values()]


def fit_to_ink(rgb, box, vertical):
    """Fit an OCR box to the actual letters and find the colour behind them.

    OCR boxes are approximate: they can stop short of the last letter, or stick
    out of the coloured button or chat bubble the text sits on. So the most
    common colour inside the box is taken as the background, rows that are
    entirely some other colour are cut off, and the box grows along the text
    until both ends reach clean background.
    Returns the fitted box and the background colour (0-1 floats).
    """
    if vertical:  # work on a transposed view, so the text always runs left to right
        (y0, x0, y1, x1), colour = fit_to_ink(rgb.transpose(1, 0, 2), (box[1], box[0], box[3], box[2]), False)
        return (x0, y0, x1, y1), colour

    h, w = rgb.shape[:2]
    x0, y0 = max(int(box[0]), 0), max(int(box[1]), 0)
    x1, y1 = min(int(np.ceil(box[2])), w), min(int(np.ceil(box[3])), h)
    if x1 <= x0 or y1 <= y0:
        return box, (1.0, 1.0, 1.0)
    thick = y1 - y0
    reach = int(thick * 0.6) + 2
    rx0, rx1 = max(x0 - reach, 0), min(x1 + reach, w)
    bg = np.median(rgb[y0:y1, x0:x1].reshape(-1, 3), axis=0)
    ink = np.abs(rgb[y0:y1, rx0:rx1].astype(np.int16) - bg).max(axis=2) > 48

    # Cut off rows that are entirely another colour: the box sticks out of a button or bubble.
    other = ink[:, x0 - rx0:x1 - rx0].mean(axis=1) > 0.9
    top, bottom = 0, len(other)
    while top < bottom and other[top]:
        top += 1
    while bottom > top and other[bottom - 1]:
        bottom -= 1
    if bottom - top < thick // 3:
        top, bottom = 0, len(other)
    bg = np.median(rgb[y0 + top:y0 + bottom, x0:x1].reshape(-1, 3), axis=0)

    # Grow sideways while the middle of the line still has ink, i.e. a letter was cut in half.
    quarter = (bottom - top) // 4
    has_ink = ink[top + quarter:bottom - quarter].any(axis=0)
    left, right = x0 - rx0, x1 - rx0
    while left > 0 and has_ink[left - 1]:
        left -= 1
    while right < len(has_ink) and has_ink[right]:
        right += 1

    pad = max(1, round(thick * 0.04))  # anti-aliased letter edges
    pad_top = 0 if top else pad        # a cut-off edge already sits exactly on the colour boundary
    pad_bottom = 0 if bottom < len(other) else pad
    fitted = (rx0 + left - pad, y0 + top - pad_top, rx0 + right + pad, y0 + bottom + pad_bottom)
    return fitted, tuple(float(c) / 255 for c in bg)


def paintable(doc, info):
    """True for a plain image we can paint into: no transparency, not rotated at an angle."""
    xref = info["xref"]
    if xref <= 0:  # inline image
        return False
    if any(doc.xref_get_key(xref, key)[0] != "null" for key in ("SMask", "Mask")):
        return False
    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        return False
    a, b, c, d, _, _ = info["transform"]
    return (abs(b) < 1e-6 and abs(c) < 1e-6) or (abs(a) < 1e-6 and abs(d) < 1e-6)


def snap_to_pixels(rect, info):
    """Round a page rect to whole pixels of the image, so wiping and painting hit exactly the same pixels."""
    w, h = info["width"], info["height"]
    to_page = pymupdf.Matrix(info["transform"])
    r = rect * ~to_page
    x0, y0, x1, y1 = round(r.x0 * w), round(r.y0 * h), round(r.x1 * w), round(r.y1 * h)
    eps = 0.01  # stay just inside, so the neighbouring pixels aren't touched
    return pymupdf.Rect((x0 + eps) / w, (y0 + eps) / h, (x1 - eps) / w, (y1 - eps) / h) * to_page


def paint_image(doc, info, patches, as_jpeg):
    """Paint each (rect, colour) patch straight into the image's pixels."""
    xref = info["xref"]
    pix = pymupdf.Pixmap(doc, xref)
    if pix.colorspace is None or pix.colorspace.n != 3 or pix.alpha:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.Pixmap(pix, 0) if pix.alpha else pix)
    w, h = pix.width, pix.height
    img = np.frombuffer(pix.samples, np.uint8).reshape(h, w, 3).copy()
    to_unit = ~pymupdf.Matrix(info["transform"])  # page coordinates -> image's unit square
    for rect, colour in patches:
        r = (rect & pymupdf.Rect(info["bbox"])) * to_unit
        x0, x1 = max(int(np.floor(r.x0 * w)), 0), min(int(np.ceil(r.x1 * w)), w)
        y0, y1 = max(int(np.floor(r.y0 * h)), 0), min(int(np.ceil(r.y1 * h)), h)
        img[y0:y1, x0:x1] = np.round(np.array(colour) * 255)
    # Write the pixels back into the same image object; keep JPEGs as JPEGs so the file doesn't balloon.
    doc.update_object(xref, f"<</Type/XObject/Subtype/Image/Width {w}/Height {h}"
                            f"/BitsPerComponent 8/ColorSpace/DeviceRGB>>")
    if as_jpeg:
        painted = pymupdf.Pixmap(pymupdf.csRGB, w, h, img.tobytes(), False)
        doc.update_stream(xref, painted.tobytes("jpeg", jpg_quality=95), compress=False)
        doc.xref_set_key(xref, "Filter", "/DCTDecode")
    else:
        doc.update_stream(xref, img.tobytes(), compress=True)


def erase(doc, page, patches, dpi):
    """Erase each (rect, colour) patch from the page.

    Redacting first removes any real or hidden (OCR layer) text under the
    patches, and gives this page its own copy of every image they touch, with
    the pixels underneath blanked to white. Those pixels are then painted in
    the background colour. Anything that isn't a plain image gets a filled
    rectangle drawn over it instead. Returns the reloaded page.
    """
    def images_under(rect):
        hits = [info for info in page.get_image_info(xrefs=True) if pymupdf.Rect(info["bbox"]).intersects(rect)]
        return hits if hits and all(paintable(doc, info) for info in hits) else []

    def key(info):  # identifies an image across the redaction, which renumbers it
        return tuple(round(v, 2) for v in info["bbox"])

    jpegs = {key(info) for info in page.get_image_info(xrefs=True)
             if info["xref"] > 0 and "DCT" in doc.xref_get_key(info["xref"], "Filter")[1]}
    snapped = []
    for rect, colour in patches:
        hits = images_under(rect)
        if hits:
            rect = snap_to_pixels(rect, max(hits, key=lambda info: abs(pymupdf.Rect(info["bbox"]) & rect)))
        snapped.append((rect, colour))
    for rect, _ in snapped:
        page.add_redact_annot(rect, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                          text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    page = doc.reload_page(page)  # otherwise PyMuPDF still reports the old images

    edits = {}
    covers = []
    for rect, colour in snapped:
        hits = images_under(rect)  # look again: redaction gave the page its own copies
        for info in hits:
            edits.setdefault(info["xref"], (info, []))[1].append((rect, colour))
        if not hits:
            covers.append((rect, colour))
    for info, image_patches in edits.values():
        paint_image(doc, info, image_patches, key(info) in jpegs)
    grow = 72 / dpi  # one pixel, to hide blanked pixels poking out of the patch
    for rect, colour in covers:
        page.draw_rect(rect + (-grow, -grow, grow, grow), color=None, fill=colour, width=0)
    return doc.reload_page(page)


def to_pdf_rect(page, box, dpi):
    """Convert a box in rendered pixels to unrotated PDF page coordinates."""
    zoom = dpi / 72
    rect = pymupdf.Rect(box[0] / zoom, box[1] / zoom, box[2] / zoom, box[3] / zoom)
    return (rect * page.derotation_matrix).normalize()


def parse_colour(value):
    if value is None or value == "auto":
        return None
    named = {"white": (1, 1, 1), "black": (0, 0, 0)}
    if value.lower() in named:
        return named[value.lower()]
    hex_value = value.lstrip("#")
    if len(hex_value) != 6:
        raise argparse.ArgumentTypeError("use auto, white, black or a hex colour like #F0F0F0")
    return tuple(int(hex_value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def clean_pdf(src, dst, targets, engine, args):
    doc = pymupdf.open(src)
    total = 0
    for page in doc:
        dpi = args.dpi or choose_dpi(page)
        removed = text_layer_matches(page, targets, args.similarity)
        if removed and not args.dry_run:
            remove_text_layer_matches(page, removed)
        for attempt in range(args.passes):
            rgb = render(page, dpi)
            words = read_words(engine, rgb)
            if args.show_text and attempt == 0:
                print(f"--- page {page.number + 1} text ---")
                lines = {}
                for word in words:
                    lines.setdefault(word.line, []).append(word.text)
                for line in lines.values():
                    print("   ", " ".join(line))

            matches = [m for target in targets for m in find_matches(words, target, args.similarity)]
            if not matches:
                break
            removed += matches
            if args.dry_run:
                break
            patches = []
            for match in matches:
                for box, vertical in match_boxes(match):
                    box, colour = fit_to_ink(rgb, box, vertical)
                    patches.append((to_pdf_rect(page, box, dpi), args.fill or colour))
            page = erase(doc, page, patches, dpi)

        total += len(removed)
        verb = "would remove" if args.dry_run else "removed"
        print(f"  page {page.number + 1}/{doc.page_count}: {verb} {len(removed)}")
        for match in removed:
            note = "" if match.score == 1 else f"  (~{match.score:.0%} like \"{match.target}\")"
            where = "  [text layer]" if match.in_text_layer else ""
            print(f"      \"{match.text}\"{note}{where}")

    if not args.dry_run:
        doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return total


def make_engine(lang):
    params = {
        "Global.log_level": "error",
        "Global.max_side_len": MAX_SIDE + 100,  # don't let RapidOCR shrink our render
        "Global.text_score": 0.3,               # keep faint lines; matching filters the noise
    }
    if lang:
        params["Rec.lang_type"] = LangRec(lang)
        params["Rec.ocr_version"] = OCRVersion.PPOCRV4 if lang in V4_ONLY_LANGS else OCRVersion.PPOCRV5
        params["Rec.model_type"] = ModelType.MOBILE
    return RapidOCR(params=params)


def collect_pdfs(paths):
    pdfs = []
    for path in paths:
        path = Path(path.strip().strip("\"'"))  # drag-and-drop adds quotes
        if path.is_dir():
            pdfs += sorted(p for p in path.glob("*.pdf") if not p.stem.endswith("_cleaned"))
        elif path.is_file():
            pdfs.append(path)
        else:
            sys.exit(f"Not found: {path}")
    return pdfs


def output_path(src, out, many):
    if out is None:
        return src.with_name(f"{src.stem}_cleaned.pdf")
    out = Path(out)
    if many or out.is_dir() or out.suffix.lower() != ".pdf":
        out.mkdir(parents=True, exist_ok=True)
        return out / f"{src.stem}_cleaned.pdf"
    return out


def main():
    parser = argparse.ArgumentParser(
        description="Delete words or sentences from every page of a (screenshot) PDF.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Examples:")[1])
    parser.add_argument("inputs", nargs="*", help="PDF files or folders of PDFs")
    parser.add_argument("-t", "--text", action="append", default=[],
                        help="word or sentence to delete (repeat -t for more than one)")
    parser.add_argument("-o", "--output", help="output file (one input) or folder (default: NAME_cleaned.pdf)")
    parser.add_argument("--similarity", type=float, default=0.85,
                        help="how close the OCR text must be, 0-1 (default 0.85); 1 = exact only")
    parser.add_argument("--exact", action="store_const", const=1.0, dest="similarity",
                        help="same as --similarity 1")
    parser.add_argument("--fill", type=parse_colour, default=None,
                        help="colour to paint over deleted text: auto (default), white, black or #RRGGBB")
    parser.add_argument("--dpi", type=float, help="render resolution for OCR (default: automatic)")
    parser.add_argument("--passes", type=int, default=2,
                        help="OCR passes per page; extra passes re-check and catch leftovers (default 2)")
    parser.add_argument("--lang", choices=LANGS,
                        help="language-specific OCR model, if your text isn't read well by the default one")
    parser.add_argument("--dry-run", action="store_true", help="only list what would be deleted")
    parser.add_argument("--show-text", action="store_true", help="print the text the OCR reads on each page")
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")

    inputs = args.inputs or [input("PDF file or folder: ")]
    targets = [t.strip() for t in args.text if t.strip()]
    if not targets and not args.show_text:
        print("Type each word or sentence to delete, then press Enter on an empty line:")
        while line := input("> ").strip():
            targets.append(line)
    if not targets and not args.show_text:
        sys.exit("Nothing to delete.")
    if not targets:
        args.dry_run = True  # just showing the text: don't write a copy

    pdfs = collect_pdfs(inputs)
    if not pdfs:
        sys.exit("No PDF files found.")

    print("Loading OCR...")
    engine = make_engine(args.lang)
    for src in pdfs:
        dst = output_path(src, args.output, len(pdfs) > 1)
        print(f"{src}")
        total = clean_pdf(src, dst, targets, engine, args)
        if args.dry_run:
            print(f"  {total} match(es) found, nothing written.")
        else:
            print(f"  {total} removed -> {dst}")


if __name__ == "__main__":
    main()
