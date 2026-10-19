import io
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import NamedTuple

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

log = logging.getLogger("rg2watch.render")

OVERLAP = 0.04
MAX_DPI = 300
MAX_PAGES = 60
MAX_PAGE_PIXELS = 40_000_000
OSD_PAGE_TIMEOUT_S = 10
OSD_BUDGET_S = 20
Image.MAX_IMAGE_PIXELS = 200_000_000


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
    long_in = max(page.get_width(), page.get_height()) / 72
    best = 0.0
    try:
        for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
            best = max(best, max(obj.get_px_size()) / long_in)  # rotation-proof: long edges
    except Exception:
        pass
    return best


def _osd_rotation(img: Image.Image, timeout: float = OSD_PAGE_TIMEOUT_S) -> int:
    if not shutil.which("tesseract"):
        return 0
    try:
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "page.png"
            img.save(png)
            out = subprocess.run(["tesseract", str(png), "-", "--psm", "0"],
                                 capture_output=True, text=True, timeout=timeout)
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
        if n > MAX_PAGES:
            raise RenderError(f"PDF has {n} pages (max {MAX_PAGES}): {pdf_path}")
        try:
            plan = []  # (page index, effective dpi, rendered long edge)
            for i in range(n):
                page = pdf[i]
                eff = min(MAX_DPI, max(dpi, _embedded_dpi(page)))
                w, h = page.get_width() * eff / 72, page.get_height() * eff / 72
                if w * h > MAX_PAGE_PIXELS:
                    raise RenderError(f"page {i + 1} would render at {w:.0f}x{h:.0f} px: {pdf_path}")
                plan.append((i, eff, max(w, h)))

            split = sum(2 if edge > max_edge else 1 for _, _, edge in plan) <= max_tiles
            keep = plan
            if not split and n > max_tiles:
                log.warning("%s has %d pages; keeping first %d and last", pdf_path, n, max_tiles - 1)
                keep = plan[:max_tiles - 1] + plan[-1:]

            tiles: list[Tile] = []
            osd_start = time.monotonic()
            for i, eff, _ in keep:
                img = pdf[i].render(scale=eff / 72).to_pil().convert("RGB")
                remaining = OSD_BUDGET_S - (time.monotonic() - osd_start)
                rot = _osd_rotation(img, min(OSD_PAGE_TIMEOUT_S, remaining)) if remaining > 0 else 0
                if rot:
                    img = img.rotate(-rot, expand=True)  # PIL rotates counter-clockwise
                if split and max(img.size) > max_edge:
                    for suffix, half in zip("ab", _halves(img)):
                        tiles.append(Tile(f"p{i + 1}{suffix}", _jpeg(half, max_edge)))
                else:
                    tiles.append(Tile(f"p{i + 1}", _jpeg(img, max_edge)))
            return tiles
        except RenderError:
            raise
        except Exception as exc:
            raise RenderError(f"cannot render {pdf_path}: {exc}") from exc
    finally:
        pdf.close()
