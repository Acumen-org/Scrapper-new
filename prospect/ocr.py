"""Reading scanned brochures with Baidu's Unlimited-OCR.

Almost every Form ADV brochure is a PDF with a real text layer, and pdfplumber
reads that exactly; no OCR can improve on it. A few (about 80 of 20,000) are
scanned images with no text at all. Those are what this is for: each page is
rendered to an image and sent to Unlimited-OCR, which returns the page as
Markdown, layout and tables included.

The model needs an NVIDIA GPU, so it runs as a server of its own, which serves
an OpenAI-compatible API (https://github.com/baidu/Unlimited-OCR):

    docker run --gpus all -p 8000:8000 vllm/vllm-openai:unlimited-ocr \\
        --model baidu/Unlimited-OCR --served-model-name Unlimited-OCR
    python -m sglang.launch_server --model baidu/Unlimited-OCR \\
        --served-model-name Unlimited-OCR --context-length 32768 --port 10000

Set its address in Settings, Crawling (for example http://ocr-host:8000/v1).
Until one is set, scanned brochures wait, and Settings says how many.
"""

from __future__ import annotations

import base64
import io
import re

from . import settings

PROMPT = "document parsing."          # the model's own single-page instruction
DPI = 200                             # enough for brochure type; 300 is the model's PDF default
PAGE_TIMEOUT_S = 300
DET_RE = re.compile(r"<\|det\|>([^<\s]+)(?:\s*\[[^\]]*\])?\s*<\|/det\|>(.*)", re.S)
TAG_RE = re.compile(r"<\|/?(ref|det|grounding)\|>")


class OCRError(Exception):
    pass


def init(conn) -> None:
    """The brochure columns OCR fills, once the brochure table exists."""
    from . import db
    try:
        if conn.execute("SELECT to_regclass('brochure') t").fetchone()["t"]:
            for col, typ in (("ocr_status", "TEXT"), ("ocr_at", "TEXT"), ("ocr_pages", "INTEGER")):
                db.add_column(conn, "brochure", col, typ)
        conn.commit()
    except Exception:
        conn.rollback()


def configured() -> bool:
    return bool(settings.get("ocr.base_url"))


def remove_det(raw: str) -> str:
    """The page as plain Markdown: <|det|>type [bbox]<|/det|> markers stripped,
    lines of one block kept together, blocks separated by a blank line, image
    blocks dropped (after the model's own post-processing)."""
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


def render_pages(pdf: bytes, max_pages: int, dpi: int = DPI) -> list[bytes]:
    """PNG images of the first pages, with pypdfium2 (installed with pdfplumber)."""
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(pdf)
    out = []
    try:
        for i in range(min(len(doc), max_pages)):
            page = doc[i]
            img = page.render(scale=dpi / 72).to_pil()
            buf = io.BytesIO()
            img.convert("RGB").save(buf, "PNG", optimize=True)
            out.append(buf.getvalue())
            page.close()
    finally:
        doc.close()
    return out


def read_page(png: bytes) -> str:
    """One page through the OCR server, as Markdown."""
    import requests
    base = (settings.get("ocr.base_url") or "").rstrip("/")
    if not base:
        raise OCRError("No OCR server is set in Settings, Crawling.")
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


def test_connection() -> tuple[bool, str]:
    """A blank page through the server: proves the address, key and model."""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (400, 120), "white").save(buf, "PNG")
        read_page(buf.getvalue())
    except OCRError as e:
        return False, str(e)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200]
    return True, "The OCR server answered."
