import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urljoin

from .config import Config
from .store import Store


SUMMARY_MAX = 4096


class CaseError(ValueError):
    pass


def manual_template(case: dict) -> str:
    return (f"Избори: {case['election']}\n"
            f"Општина и бирачко место: {case['municipality']}, {case['station']}\n"
            f"Верзија документа: {case['url']}\n"
            f"РИК: {case['rik_url']}\n\n"
            "Разлог за пажљиву људску проверу: [унесите опрезан, проверен опис].")


class CaseHandoff:
    def __init__(self, store: Store, cfg: Config, drafter=None):
        self.store, self.cfg, self.drafter = store, cfg, drafter

    def _case(self, url: str, require_escalate: bool = True) -> dict:
        row = self.store.case_context(url)
        if row is None:
            raise CaseError("unknown document URL")
        if require_escalate and row["verdict"] != "escalate":
            raise CaseError("latest verdict must be escalate")
        base = row["election_url"] or ""
        rik = urljoin(base.rstrip("/") + "/", url) if base else url
        return {"url": url, "parent": row["parent"], "election": row["election_title"] or row["parent"],
                "municipality": row["municipality"] or "(непозната)",
                "station": row["station_no"] or "?", "label": row["label"] or "документ",
                "rik_url": rik, "note": row["note"] or ""}

    def _cached_pdf(self, url: str) -> tuple[Path, str]:
        cache_dir = ((Path("data") if str(self.cfg.db_path) == ":memory:" else
                      Path(self.cfg.db_path).parent) / "pdfs").resolve()
        path = (cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".pdf")).resolve()
        try:
            path.relative_to(cache_dir)
            st = path.stat()
            if not path.is_file() or st.st_size > self.cfg.max_pdf_bytes:
                raise CaseError("cached PDF is missing, invalid, or too large")
            data = path.read_bytes()
        except OSError as exc:
            raise CaseError("cached PDF is missing") from exc
        if not data.startswith(b"%PDF"):
            raise CaseError("cached PDF is malformed")
        return path, hashlib.sha256(data).hexdigest()

    def prepare(self, url: str, draft: bool = True) -> dict:
        case = self._case(url)
        _, digest = self._cached_pdf(url)
        summary, warning = manual_template(case), None
        if draft and self.drafter is not None:
            try:
                summary = self.drafter.draft(case)
            except Exception as exc:
                warning = str(exc)
        return {"case": case, "pdf_sha256": digest, "summary": summary, "warning": warning}

    def claim(self, url: str, summary: str) -> dict:
        existing = self.store.get_publication(url)
        if existing and existing["phase"] == "published":
            return dict(existing)
        if not isinstance(summary, str) or not summary.strip():
            raise CaseError("public summary must not be empty")
        summary = summary.strip()
        if len(summary.encode("utf-8")) > SUMMARY_MAX:
            raise CaseError(f"public summary is longer than {SUMMARY_MAX} bytes")
        case = self._case(url)
        cached, digest = self._cached_pdf(url)
        summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        if existing:
            if existing["summary_sha256"] != summary_hash or existing["pdf_sha256"] != digest:
                raise CaseError("inputs differ from the frozen pending package; inspect the frozen case")
            staged = Path(existing["staged_pdf_path"])
            if not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != digest:
                raise CaseError("frozen staged PDF is missing or changed")
            return dict(existing)
        stage_dir = Path(self.cfg.db_path).resolve().parent / ".rg2watch-publications"
        stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(stage_dir, 0o700)
        final = stage_dir / (hashlib.sha256(url.encode()).hexdigest() + ".pdf")
        fd, tmp = tempfile.mkstemp(dir=stage_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(cached.read_bytes())
                fh.flush(); os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            if hashlib.sha256(Path(tmp).read_bytes()).hexdigest() != digest:
                raise CaseError("cached PDF changed while it was staged")
            os.replace(tmp, final)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        row = self.store.claim_publication(url, summary, summary_hash, str(final), digest, case)
        return dict(row)

    @staticmethod
    def viber_text(case: dict, concern: str, folder_url: str) -> str:
        one_line = " ".join(concern.split())[:500]
        return ("Могућа неправилност за људску проверу — није утврђена чињеница.\n"
                f"Бирачко место: {case['municipality']}, {case['station']}\n"
                f"Разлог: {one_line}\nРИК: {case['rik_url']}\n"
                f"Јавни предмет (свако са везом може да отвори): {folder_url}")
