import io
import shutil
from pathlib import Path

import pytest
from PIL import Image

from rg2watch import render
from rg2watch.render import RenderError, page_count, render_tiles

FIX = Path(__file__).parent / "fixtures"


def make_pdf(path: Path, size: tuple[int, int], pages: int, dpi: int = 200) -> Path:
    imgs = [Image.new("RGB", size, (255, 255, 255)) for _ in range(pages)]
    imgs[0].save(path, "PDF", save_all=True, append_images=imgs[1:], resolution=dpi)
    return path


@pytest.fixture(autouse=True)
def no_osd(request, monkeypatch):
    if "real_osd" not in request.keywords:
        monkeypatch.setattr(render, "_osd_rotation", lambda img, timeout=10: 0)


def dims(tile) -> tuple[int, int]:
    return Image.open(io.BytesIO(tile.jpeg)).size


def test_a3_landscape_two_pages_four_tiles(tmp_path):
    tiles = render_tiles(make_pdf(tmp_path / "a.pdf", (3306, 2338), 2))
    assert [t.name for t in tiles] == ["p1a", "p1b", "p2a", "p2b"]
    for t in tiles:
        assert 1500 < max(dims(t)) <= 2576


def test_a4_portrait_pages_one_tile_each(tmp_path):
    tiles = render_tiles(make_pdf(tmp_path / "a.pdf", (1653, 2338), 3))
    assert [t.name for t in tiles] == ["p1", "p2", "p3"]
    assert page_count(tmp_path / "a.pdf") == 3


def test_max_tiles_keeps_last_page(tmp_path):
    tiles = render_tiles(make_pdf(tmp_path / "a.pdf", (827, 1169), 20, dpi=100), max_tiles=16)
    names = [t.name for t in tiles]
    assert len(tiles) == 16
    assert names[-1] == "p20"
    assert names[:15] == [f"p{i}" for i in range(1, 16)]


def test_oversized_pages_not_split_when_over_max_tiles(tmp_path):
    tiles = render_tiles(make_pdf(tmp_path / "a.pdf", (3306, 2338), 10), max_tiles=16)
    assert [t.name for t in tiles] == [f"p{i}" for i in range(1, 11)]
    assert all(max(dims(t)) <= 2576 for t in tiles)


def test_osd_absent_does_not_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert len(render_tiles(make_pdf(tmp_path / "a.pdf", (1653, 2338), 1))) == 1


@pytest.mark.real_osd
def test_osd_failure_does_not_fail(tmp_path):
    # blank page: tesseract OSD errors out; render must still succeed
    assert len(render_tiles(make_pdf(tmp_path / "a.pdf", (827, 1169), 1, dpi=100))) == 1


def test_osd_rotation_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(render, "_osd_rotation", lambda img, timeout=10: 90)
    tiles = render_tiles(make_pdf(tmp_path / "a.pdf", (1653, 2338), 1))
    w, h = dims(tiles[0])
    assert w == 2338 and abs(h - 1653) <= 1


def test_render_garbage_raises(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"<html>error</html>")
    with pytest.raises(RenderError):
        render_tiles(p)
    with pytest.raises(RenderError):
        page_count(p)


def test_oversized_page_rejected(tmp_path):
    # 3000 px image declared at 20 dpi -> 10800 pt page; at 200 dpi that is ~900 MP
    with pytest.raises(RenderError):
        render_tiles(make_pdf(tmp_path / "a.pdf", (3000, 3000), 1, dpi=20))


def test_too_many_pages_rejected(tmp_path):
    with pytest.raises(RenderError):
        render_tiles(make_pdf(tmp_path / "a.pdf", (100, 140), 61, dpi=20))


@pytest.mark.parametrize("name", ["zero_pages.pdf", "encrypted.pdf"])
def test_unusable_pdf_fixtures_rejected(name):
    with pytest.raises(RenderError):
        render_tiles(FIX / name)


def test_rotated_300dpi_page_keeps_resolution(tmp_path):
    import pypdfium2 as pdfium
    src = make_pdf(tmp_path / "p.pdf", (2480, 3508), 1, dpi=300)  # portrait A4 at 300 dpi
    doc = pdfium.PdfDocument(str(src))
    doc[0].set_rotation(270)  # landscape page, long edges swapped
    out = tmp_path / "r.pdf"
    doc.save(str(out))
    doc.close()
    tiles = render_tiles(out, dpi=200)
    assert [t.name for t in tiles] == ["p1a", "p1b"]
    assert all(abs(max(dims(t)) - 2480) <= 1 for t in tiles)  # 300 dpi, not downgraded to ~212
