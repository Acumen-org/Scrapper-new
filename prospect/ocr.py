"""Reading brochures that have no text layer, on the server's own CPU.

Almost every Form ADV brochure is a PDF with a real text layer, and pdfplumber
reads that exactly. About 60 of 20,000 come back (nearly) empty. Looking at
all of them (2026-10) showed what they really are:

  form fields   56 were typed into the SEC's fillable Form ADV template. The
                text is in the PDF's form fields, which a text extractor skips
                and a renderer leaves blank. Read straight from the fields it
                is exact, with no OCR at all: form_text().
  image scans   3 were scanned paper. Those pages are images, read with
                Tesseract, the open-source OCR engine, on the CPU (about 0.7
                seconds a page, English, spacing and punctuation kept).
  blank         3 were filed as an empty template; there is nothing to read.

Baidu's Unlimited-OCR reads layout and tables better still, but needs an
NVIDIA GPU. If one is ever available it runs as its own server and can be set
in Settings, Crawling (ocr.base_url); scans then go there instead of to
Tesseract.
"""

from __future__ import annotations

import base64
import io
import os
import re
import shutil
import subprocess

from . import settings

PROMPT = "document parsing."          # Unlimited-OCR's single-page instruction
DPI = 300                             # Tesseract reads small type best at 300
FORM_MIN_CHARS = 200                  # less than this in the form fields is a blank template
PAGE_TIMEOUT_S = 300
TESSERACT_TIMEOUT_S = 120
DET_RE = re.compile(r"<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)", re.S)
TAG_RE = re.compile(r"<\|/?(ref|det|grounding)\|>")


class OCRError(Exception):
    pass


def init(conn) -> None:
    """The brochure columns this fills, once the brochure table exists."""
    from . import db
    try:
        if conn.execute("SELECT to_regclass('brochure') t").fetchone()["t"]:
            for col, typ in (("ocr_status", "TEXT"), ("ocr_at", "TEXT"), ("ocr_pages", "INTEGER"),
                             ("ocr_method", "TEXT")):
                db.add_column(conn, "brochure", col, typ)
        conn.commit()
    except Exception:
        conn.rollback()


# ------------------------------------------------------------------ engines

def tesseract_cmd() -> str | None:
    """The tesseract binary: TESSERACT_CMD if set, else whatever is on PATH."""
    cmd = os.environ.get("TESSERACT_CMD")
    if cmd and os.path.exists(cmd):
        return cmd
    return shutil.which("tesseract")


def engine() -> str | None:
    """Which OCR reads scanned pages here: an Unlimited-OCR server when one is
    set, else Tesseract when installed, else none."""
    if settings.get("ocr.base_url"):
        return "unlimited-ocr"
    if tesseract_cmd():
        return "tesseract"
    return None


def configured() -> bool:
    """Whether brochures without a text layer can be read here. Form fields
    need nothing; scans need an engine. Kept as the job's requirement."""
    return True


# ------------------------------------------------------------------ form fields

def _decode(v) -> str:
    if isinstance(v, bytes):
        if v[:2] in (b"\xfe\xff", b"\xff\xfe"):
            return v.decode("utf-16", "replace")
        return v.decode("latin-1")
    return "" if v is None else str(v)


def form_text(pdf: bytes) -> str:
    """The text typed into a PDF's form fields, in page order, top to bottom.
    The SEC's fillable Form ADV template keeps a firm's whole brochure there."""
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1
    try:
        doc = PDFDocument(PDFParser(io.BytesIO(pdf)))
        af = resolve1(doc.catalog.get("AcroForm"))
    except Exception:
        return ""
    if not af:
        return ""
    page_no = {}
    try:
        for i, page in enumerate(PDFPage.create_pages(doc)):
            page_no[page.pageid] = i
    except Exception:
        pass
    found = []          # (page, -top, order, text)
    order = 0
    stack = [(f, None) for f in reversed(list(resolve1(af.get("Fields")) or []))]
    seen = set()
    while stack:
        ref, inherited = stack.pop()
        key = getattr(ref, "objid", None)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        f = resolve1(ref)
        if not isinstance(f, dict):
            continue
        value = f.get("V", inherited)
        kids = resolve1(f.get("Kids")) or []
        if kids:
            stack += [(k, value) for k in reversed(list(kids))]
            continue
        text = _decode(resolve1(value)).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or text in ("Off", "Yes"):
            continue
        pg = f.get("P")
        pno = page_no.get(getattr(pg, "objid", None), 10**6)
        rect = resolve1(f.get("Rect")) or [0, 0, 0, 0]
        try:
            top = float(rect[3])
        except (TypeError, ValueError, IndexError):
            top = 0.0
        order += 1
        found.append((pno, -top, order, text))
    found.sort()
    return "\n\n".join(t for _, _, _, t in found)


# ------------------------------------------------------------------ scans

def remove_det(raw: str) -> str:
    """Unlimited-OCR's reply as plain Markdown: <|det|>type [bbox]<|/det|>
    markers stripped, lines of one block kept together, blocks separated by a
    blank line, image blocks dropped (the model's own post-processing)."""
    blocks, cur = [], None
    for line in (raw or "").splitlines():
        line = line.rstrip()
        if not line:
            continue
        m = DET_RE.match(line)
        if m:
            category, content = m.group(1).strip(), m.group(2).strip()
            if cur is not None:
                blocks.append(cur)
            cur = None if category == "image" else ([content] if content else [])
            continue
        if cur is None:
            cur = []
        cur.append(line)
    if cur:
        blocks.append(cur)
    return TAG_RE.sub("", "\n\n".join("\n".join(b) for b in blocks if b)).strip()


def image_pages(pdf: bytes, max_pages: int) -> list[int]:
    """Indexes of the pages that carry an image, the only ones OCR can help."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        out = []
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            if any(o.type == 3 for o in page.get_objects()):    # FPDF_PAGEOBJ_IMAGE
                out.append(i)
            page.close()
        return out
    finally:
        doc.close()


def render(pdf: bytes, index: int, dpi: int = DPI, grayscale: bool = True) -> bytes:
    """One page as a PNG, with pypdfium2 (installed with pdfplumber)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    try:
        page = doc[index]
        img = page.render(scale=dpi / 72, grayscale=grayscale).to_pil()
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        page.close()
        return buf.getvalue()
    finally:
        doc.close()


def tesseract_page(png: bytes) -> str:
    cmd = tesseract_cmd()
    if not cmd:
        raise OCRError("Tesseract is not installed here.")
    try:
        r = subprocess.run([cmd, "stdin", "stdout", "-l", "eng", "--psm", "3"], input=png,
                           capture_output=True, timeout=TESSERACT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise OCRError("Tesseract took longer than two minutes on one page.") from None
    if r.returncode != 0:
        raise OCRError("Tesseract failed: " + r.stderr.decode("utf-8", "replace")[-200:])
    return r.stdout.decode("utf-8", "replace").strip()


def read_page(png: bytes) -> str:
    """One page through the Unlimited-OCR server, as Markdown."""
    import requests
    base = (settings.get("ocr.base_url") or "").rstrip("/")
    if not base:
        raise OCRError("No Unlimited-OCR server is set in Settings, Crawling.")
    headers = {"Content-Type": "application/json"}
    key = settings.get("ocr.api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": settings.get("ocr.model") or "Unlimited-OCR",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": PROMPT},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}}]}],
        "temperature": 0,
        "max_tokens": 8192,
        "skip_special_tokens": False,
    }
    try:
        r = requests.post(base + "/chat/completions", json=body, headers=headers,
                          timeout=PAGE_TIMEOUT_S)
    except requests.RequestException as e:
        raise OCRError(f"Could not reach the OCR server: {type(e).__name__}") from None
    if r.status_code >= 400:
        raise OCRError(f"The OCR server answered HTTP {r.status_code}: {r.text[:160]}")
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        raise OCRError("The OCR server sent back something unreadable.") from None
    return remove_det(text if isinstance(text, str) else "")


def ocr_page(pdf: bytes, index: int) -> str:
    """One scanned page with whichever engine is here."""
    eng = engine()
    if eng == "unlimited-ocr":
        return read_page(render(pdf, index, dpi=200, grayscale=False))
    if eng == "tesseract":
        return tesseract_page(render(pdf, index))
    raise OCRError("No OCR engine here: install Tesseract or set an Unlimited-OCR server.")


# ------------------------------------------------------------------ a whole brochure

def read_pdf(pdf: bytes, max_pages: int) -> tuple[str, str, int]:
    """(text, method, pages read) for a brochure with no text layer.
    method is 'form' (exact text from form fields), 'tesseract' or
    'unlimited-ocr' (scanned pages), or 'empty' (nothing on it to read)."""
    text = form_text(pdf)
    if len(text) >= FORM_MIN_CHARS:
        return text, "form", 0
    pages = image_pages(pdf, max_pages)
    if not pages:
        return text, "empty", 0
    eng = engine()
    if not eng:
        raise OCRError("This brochure is a scan and no OCR engine is installed here.")
    parts = [text] if text else []
    parts += [ocr_page(pdf, i) for i in pages]
    return "\n\n".join(p for p in parts if p).replace("\x00", ""), eng, len(pages)


def test_connection() -> tuple[bool, str]:
    """Proves the engine scans would go to: a blank page through it."""
    eng = engine()
    if not eng:
        return False, "No OCR engine: Tesseract is not installed and no server is set."
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("L" if eng == "tesseract" else "RGB", (400, 120), 255).save(buf, "PNG")
        if eng == "tesseract":
            tesseract_page(buf.getvalue())
        else:
            read_page(buf.getvalue())
    except OCRError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return True, ("Tesseract answered." if eng == "tesseract" else "The Unlimited-OCR server answered.")
