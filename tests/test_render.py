import io
import shutil
from pathlib import Path

import pytest
from PIL import Image

from rg2watch import render
from rg2watch.render import RenderError, page_count, render_tiles

FIX = Path(__file__).parent / "fixtures"
REAL_OCR_HEADER = render._ocr_header  # conftest stubs the module attribute per test


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
    monkeypatch.setattr(render, "MAX_TILE_PIXELS", 10**9)  # isolate rotation from the area clamp
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


def test_rotated_300dpi_page_keeps_resolution(tmp_path, monkeypatch):
    import pypdfium2 as pdfium
    monkeypatch.setattr(render, "MAX_TILE_PIXELS", 10**9)  # isolate dpi from the area clamp
    src = make_pdf(tmp_path / "p.pdf", (2480, 3508), 1, dpi=300)  # portrait A4 at 300 dpi
    doc = pdfium.PdfDocument(str(src))
    doc[0].set_rotation(270)  # landscape page, long edges swapped
    out = tmp_path / "r.pdf"
    doc.save(str(out))
    doc.close()
    tiles = render_tiles(out, dpi=200)
    assert [t.name for t in tiles] == ["p1a", "p1b"]
    assert all(abs(max(dims(t)) - 2480) <= 1 for t in tiles)  # 300 dpi, not downgraded to ~212


def test_only_kept_pages_are_rendered(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(render, "_osd_rotation", lambda img, timeout=10: seen.append(1) or 0)
    render_tiles(make_pdf(tmp_path / "a.pdf", (827, 1169), 20, dpi=100), max_tiles=16)
    assert len(seen) == 16


def test_tile_loop_exception_becomes_render_error(tmp_path, monkeypatch):
    def boom(img, max_edge):
        raise ValueError("boom")

    monkeypatch.setattr(render, "_jpeg", boom)
    with pytest.raises(RenderError):
        render_tiles(make_pdf(tmp_path / "a.pdf", (827, 1169), 1, dpi=100))


@pytest.mark.real_osd
def test_osd_subprocess_timeout_is_swallowed(monkeypatch):
    import subprocess
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(kw)
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert render._osd_rotation(Image.new("RGB", (50, 50)), timeout=7) == 0
    assert seen["timeout"] == 7


class FakeMonotonic:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now


def test_osd_budget_counts_only_osd_time(tmp_path, monkeypatch):
    FakeMonotonic.now = 0.0
    monkeypatch.setattr(render, "time", FakeMonotonic)
    timeouts = []

    def osd(img, timeout=10):
        timeouts.append(timeout)
        FakeMonotonic.now += 15
        return 0

    real_jpeg = render._jpeg

    def slow_jpeg(img, max_edge):
        FakeMonotonic.now += 100  # rendering/encoding time must not eat the OSD budget
        return real_jpeg(img, max_edge)

    monkeypatch.setattr(render, "_osd_rotation", osd)
    monkeypatch.setattr(render, "_jpeg", slow_jpeg)
    render_tiles(make_pdf(tmp_path / "a.pdf", (827, 1169), 3, dpi=100))
    assert timeouts == [10, 5]  # page 3 skipped: 30 s spent in OSD > 20 s budget


@pytest.mark.parametrize("size,pages", [((3306, 2338), 2), ((2400, 2400), 1), ((5000, 1500), 1)])
def test_tiles_clamped_to_model_pixel_limits(tmp_path, size, pages):
    tiles = render_tiles(make_pdf(tmp_path / "a.pdf", size, pages))
    for t in tiles:
        w, h = dims(t)
        assert max(w, h) <= 2576 and w * h <= 3_750_000


@pytest.mark.parametrize("size,pages,dpi", [((3306, 2338), 2, 200), ((1653, 2338), 3, 200),
                                            ((827, 1169), 20, 100), ((3306, 2338), 10, 200)])
def test_count_tiles_matches_render_without_rendering(tmp_path, monkeypatch, size, pages, dpi):
    pdf = make_pdf(tmp_path / "a.pdf", size, pages, dpi=dpi)
    expected = len(render_tiles(pdf, serbian_only=False))
    monkeypatch.setattr(render.pdfium.PdfPage, "render",
                        lambda *a, **kw: pytest.fail("count_tiles must not render pixels"))
    assert render.count_tiles(pdf, serbian_only=False) == expected  # header OCR renders pages


def stub_ocr(monkeypatch, texts):
    """texts[i] is the header OCR for page i+1 (page 1 is never OCRed); an Exception is raised."""
    calls = []

    def fake(img, timeout=10):
        calls.append(img.size)
        t = texts[len(calls)]
        if isinstance(t, Exception):
            raise t
        return t

    monkeypatch.setattr(render, "_ocr_header", fake)
    return calls


def test_serbian_marker_on_page_5_keeps_four(tmp_path, monkeypatch, caplog):
    calls = stub_ocr(monkeypatch, ["", "a", "b", "c", "JEGYZOKONYV", "PROCES-VERBAL"] + [""] * 5)
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 11, dpi=100)
    assert render.serbian_page_count(pdf) == 4
    assert len(calls) == 4  # stops at the first marker
    calls.clear()
    with caplog.at_level("INFO", logger="rg2watch.render"):
        assert [t.name for t in render_tiles(pdf, dpi=100)] == ["p1", "p2", "p3", "p4"]
    assert "kept Serbian copy: pages 1-4 of 11" in caplog.text
    calls.clear()
    assert render.count_tiles(pdf, dpi=100) == 4
    assert render.count_tiles(pdf, dpi=100, serbian_only=False) == 11


def test_serbian_diacritics_detected(tmp_path, monkeypatch):
    stub_ocr(monkeypatch, ["", "", "JEGYZŐKÖNYV", ""])
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 4, dpi=100)
    assert render.serbian_page_count(pdf) == 2


def test_serbian_no_marker_keeps_all(tmp_path, monkeypatch):
    stub_ocr(monkeypatch, ["", "ZAPISNICI?", "nothing", "here"])  # ZAPISNICI is not a marker
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 4, dpi=100)
    assert render.serbian_page_count(pdf) is None
    assert len(render_tiles(pdf, dpi=100)) == 4


@pytest.mark.parametrize("failure", ["none", "raise"])
def test_serbian_ocr_error_keeps_all(tmp_path, monkeypatch, failure):
    bad = None if failure == "none" else RuntimeError("boom")
    calls = stub_ocr(monkeypatch, ["", bad, "PROCES-VERBAL", ""])
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 4, dpi=100)
    assert render.serbian_page_count(pdf) is None
    calls.clear()
    assert len(render_tiles(pdf, dpi=100)) == 4


def test_serbian_budget_exhausted_keeps_all(tmp_path, monkeypatch):
    stub_ocr(monkeypatch, ["", "", "", "PROCES-VERBAL"])
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 4, dpi=100)
    assert render.serbian_page_count(pdf, budget_s=0) is None


def test_serbian_two_page_pdf_no_ocr(tmp_path, monkeypatch):
    calls = stub_ocr(monkeypatch, ["", "PROCES-VERBAL"])
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 2, dpi=100)
    assert render.serbian_page_count(pdf) is None
    assert calls == []


def test_serbian_only_false_skips_ocr(tmp_path, monkeypatch):
    calls = stub_ocr(monkeypatch, ["", "", "PROCES-VERBAL", ""])
    pdf = make_pdf(tmp_path / "a.pdf", (827, 1169), 4, dpi=100)
    assert len(render_tiles(pdf, dpi=100, serbian_only=False)) == 4
    assert calls == []


def test_serbian_header_strip_is_top_22_percent(tmp_path, monkeypatch):
    calls = stub_ocr(monkeypatch, ["", "", "", ""])
    pdf = make_pdf(tmp_path / "a.pdf", (800, 1000), 3, dpi=150)
    render.serbian_page_count(pdf, dpi=150)
    assert calls and all(abs(h - 0.22 * 1000) <= 2 for _, h in calls)


@pytest.mark.real_osd
def test_real_tesseract_reads_title_marker(tmp_path, monkeypatch):
    if not shutil.which("tesseract"):
        pytest.skip("tesseract not installed")
    from PIL import ImageDraw, ImageFont

    monkeypatch.setattr(render, "_ocr_header", REAL_OCR_HEADER)
    imgs = [Image.new("RGB", (1240, 1754), "white") for _ in range(3)]
    font = ImageFont.load_default(size=90)
    ImageDraw.Draw(imgs[2]).text((100, 120), "PROCES-VERBAL", fill="black", font=font)
    pdf = tmp_path / "m.pdf"
    imgs[0].save(pdf, "PDF", save_all=True, append_images=imgs[1:], resolution=150)
    assert render.serbian_page_count(pdf) == 2
