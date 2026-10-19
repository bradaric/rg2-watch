import io
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

log = logging.getLogger("rg2watch.render")

OVERLAP = 0.04
MAX_DPI = 300
Image.MAX_IMAGE_PIXELS = None


class RenderError(Exception):
    pass


class Tile(NamedTuple):
    name: str
    jpeg: bytes


def _open(pdf_path: Path) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        raise RenderError(f"unreadable PDF {pdf_path}: {exc}") from exc


def page_count(pdf_path: Path) -> int:
    pdf = _open(pdf_path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def _embedded_dpi(page) -> float:
    width_in = page.get_width() / 72
    best = 0.0
    try:
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
            best = max(best, obj.get_px_size()[0] / width_in)
    except Exception:
        pass
    return best


def _render_page(pdf, index: int, dpi: int) -> Image.Image:
    page = pdf[index]
    eff = min(MAX_DPI, max(dpi, _embedded_dpi(page)))
    return page.render(scale=eff / 72).to_pil().convert("RGB")


def _osd_rotation(img: Image.Image) -> int:
    if not shutil.which("tesseract"):
        return 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "page.png"
            img.save(png)
            out = subprocess.run(["tesseract", str(png), "-", "--psm", "0"],
                                 capture_output=True, text=True, timeout=60)
        m = re.search(r"^Rotate:\s*(\d+)", out.stdout, re.M)
        return int(m.group(1)) % 360 if m else 0
    except Exception:
        return 0


def _jpeg(img: Image.Image, max_edge: int) -> bytes:
    if max(img.size) > max_edge:
        k = max_edge / max(img.size)
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))),
                         Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _halves(img: Image.Image) -> list[Image.Image]:
    w, h = img.size
    if w >= h:
        cut = round(w * (0.5 + OVERLAP / 2))
        return [img.crop((0, 0, cut, h)), img.crop((w - cut, 0, w, h))]
    cut = round(h * (0.5 + OVERLAP / 2))
    return [img.crop((0, 0, w, cut)), img.crop((0, h - cut, w, h))]


def render_tiles(pdf_path: Path, max_edge: int = 2576, max_tiles: int = 16,
                 dpi: int = 200) -> list[Tile]:
    pdf = _open(pdf_path)
    try:
        n = len(pdf)
        if n == 0:
            raise RenderError(f"PDF has no pages: {pdf_path}")
        try:
            pages = [_render_page(pdf, i, dpi) for i in range(n)]
        except Exception as exc:
            raise RenderError(f"cannot render {pdf_path}: {exc}") from exc
    finally:
        pdf.close()

    split = sum(2 if max(p.size) > max_edge else 1 for p in pages) <= max_tiles
    numbers = list(range(1, n + 1))
    if not split and n > max_tiles:
        log.warning("%s has %d pages; keeping first %d and last", pdf_path, n, max_tiles - 1)
        numbers = numbers[:max_tiles - 1] + [n]

    tiles: list[Tile] = []
    for num in numbers:
        img = pages[num - 1]
        rot = _osd_rotation(img)
        if rot:
            img = img.rotate(-rot, expand=True)  # PIL rotates counter-clockwise
        if split and max(img.size) > max_edge:
            for suffix, half in zip("ab", _halves(img)):
                tiles.append(Tile(f"p{num}{suffix}", _jpeg(half, max_edge)))
        else:
            tiles.append(Tile(f"p{num}", _jpeg(img, max_edge)))
    return tiles
