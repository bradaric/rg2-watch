import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from urllib.parse import urljoin

from .config import Config
from .store import Store


SUMMARY_MAX = 4096
VIBER_REASON_MAX = 500
KNJAZEVAC_61_REASON = ("Потребна је провера броја гласача ван бирачког места (18 од 58) "
                       "и броја гласова за једну листу (53 од 58).")
_URI_SCHEME = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z][A-Za-z0-9+.-]{0,31}):\S+")
_BARE_DOMAIN = re.compile(
    r"(?<![A-Za-z0-9-])"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:[A-Za-z](?:[A-Za-z0-9-]{0,60}[A-Za-z0-9]))"
    r"(?::\d{1,5})?(?:[/#?]\S*)?",
    re.IGNORECASE,
)


class CaseError(ValueError):
    pass


def normalize_viber_reason(reason: str) -> str:
    if not isinstance(reason, str):
        raise CaseError("short Viber reason is required")
    if any(unicodedata.category(char) == "Cc" and char not in "\t\n\r" for char in reason):
        raise CaseError("short Viber reason contains a control character")
    normalized = " ".join(reason.split())
    if not normalized:
        raise CaseError("short Viber reason must not be empty")
    if len(normalized) > VIBER_REASON_MAX:
        raise CaseError(f"short Viber reason is longer than {VIBER_REASON_MAX} characters")
    if _URI_SCHEME.search(normalized) or _BARE_DOMAIN.search(normalized):
        raise CaseError("short Viber reason must not contain a URL")
    return normalized


def viber_revision(row, url: str, reason: str) -> dict:
    """Validate the one explicitly approved historical correction from a SQLite row."""
    required = {"url", "phase", "case_json", "summary", "summary_sha256",
                "staged_pdf_path", "pdf_sha256", "folder_id", "pdf_file_id",
                "text_file_id", "final_url", "viber_text", "published_at"}
    if not required <= set(row.keys()):
        raise CaseError("unsupported publication database")
    if reason != KNJAZEVAC_61_REASON:
        raise CaseError("reason is not the exact approved Књажевац 61 sentence")
    if row["url"] != url or row["phase"] != "published":
        raise CaseError("target is not the selected published case")
    try:
        case = json.loads(row["case_json"])
    except (TypeError, ValueError) as exc:
        raise CaseError("frozen case metadata is invalid") from exc
    if not isinstance(case, dict) or case.get("municipality") != "Књажевац" \
            or str(case.get("station")) != "61" or case.get("url") != url:
        raise CaseError("target is not Књажевац БМ 61")
    frozen = (row["summary"], row["summary_sha256"], row["staged_pdf_path"],
              row["pdf_sha256"], row["folder_id"], row["pdf_file_id"],
              row["text_file_id"], row["published_at"], case.get("rik_url"))
    if not all(isinstance(value, str) and value for value in frozen):
        raise CaseError("frozen publication metadata is incomplete")
    if hashlib.sha256(row["summary"].encode("utf-8")).hexdigest() != row["summary_sha256"]:
        raise CaseError("frozen summary metadata does not match")
    if len(row["pdf_sha256"]) != 64 or any(c not in "0123456789abcdef" for c in row["pdf_sha256"]):
        raise CaseError("frozen PDF metadata is invalid")
    final = "https://drive.google.com/drive/folders/" + row["folder_id"]
    if row["final_url"] != final:
        raise CaseError("saved case-folder URL does not match the frozen folder")
    old = CaseHandoff.legacy_viber_text(case, row["summary"], final)
    proposed = CaseHandoff.viber_text(case, reason, final)
    if row["viber_text"] not in (old, proposed):
        raise CaseError("saved Viber text is neither the original nor the approved correction")
    return {"case": case, "old": row["viber_text"], "proposed": proposed,
            "changed": row["viber_text"] != proposed}


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

    def _cached_pdf(self, url: str) -> tuple[Path, str, bytes]:
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
        return path, hashlib.sha256(data).hexdigest(), data

    def prepare(self, url: str, draft: bool = True) -> dict:
        case = self._case(url)
        _, digest, _ = self._cached_pdf(url)
        summary, warning = manual_template(case), None
        if draft and self.drafter is not None:
            try:
                summary = self.drafter.draft(case)
            except Exception as exc:
                warning = str(exc)
        public_case = {k: v for k, v in case.items() if k != "note"}
        return {"case": public_case, "pdf_sha256": digest, "summary": summary, "warning": warning}

    def claim(self, url: str, summary: str, viber_reason: str | None = None) -> dict:
        existing = self.store.get_publication(url)
        if existing and existing["phase"] == "published":
            return dict(existing)
        if not isinstance(summary, str) or not summary.strip():
            raise CaseError("public summary must not be empty")
        summary = summary.strip()
        if len(summary.encode("utf-8")) > SUMMARY_MAX:
            raise CaseError(f"public summary is longer than {SUMMARY_MAX} bytes")
        summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
        if existing:
            if existing["viber_reason"] is None:
                if viber_reason is not None:
                    raise CaseError("inputs differ from the frozen pending package; inspect the frozen case")
                normalized_reason = None
            else:
                normalized_reason = normalize_viber_reason(viber_reason)
                if existing["viber_reason"] != normalized_reason:
                    raise CaseError("inputs differ from the frozen pending package; inspect the frozen case")
            if existing["summary_sha256"] != summary_hash:
                raise CaseError("inputs differ from the frozen pending package; inspect the frozen case")
            staged = Path(existing["staged_pdf_path"])
            if not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != existing["pdf_sha256"]:
                raise CaseError("frozen staged PDF is missing or changed")
            return dict(existing)
        normalized_reason = normalize_viber_reason(viber_reason)
        _, digest, pdf = self._cached_pdf(url)
        case = self._case(url)
        stage_dir = Path(self.cfg.db_path).resolve().parent / ".rg2watch-publications"
        stage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(stage_dir, 0o700)
        prefix = hashlib.sha256(url.encode()).hexdigest() + "."
        final = stage_dir / f"{prefix}{summary_hash}.{digest}.pdf"
        # Without a committed SQLite claim no remote operation can have started. Such files are
        # only remnants of a pre-claim crash, not frozen input: discard them before restaging the
        # current explicit request so recovery never depends on caller/browser memory.
        for orphan in stage_dir.glob(prefix + "*.pdf"):
            orphan.unlink()
        fd, tmp = tempfile.mkstemp(dir=stage_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(pdf)
                fh.flush(); os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            if hashlib.sha256(Path(tmp).read_bytes()).hexdigest() != digest:
                raise CaseError("cached PDF changed while it was staged")
            os.replace(tmp, final)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        # Persist the orphan removal and new directory entry before SQLite is allowed to reference
        # the file. The file bytes themselves were fsynced before the rename.
        dir_fd = os.open(stage_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        row, inserted = self.store.claim_publication(
            url, summary, summary_hash, str(final), digest, case, normalized_reason)
        if not inserted:
            if row["summary_sha256"] != summary_hash or row["pdf_sha256"] != digest:
                if str(final) != row["staged_pdf_path"]:
                    final.unlink(missing_ok=True)
                raise CaseError("inputs differ from the frozen pending package; inspect the frozen case")
            staged = Path(row["staged_pdf_path"])
            if not staged.is_file() or hashlib.sha256(staged.read_bytes()).hexdigest() != row["pdf_sha256"]:
                raise CaseError("frozen package is incomplete: staged PDF is missing or changed")
            return dict(row)
        return dict(row)

    @staticmethod
    def legacy_viber_text(case: dict, concern: str, folder_url: str) -> str:
        one_line = " ".join(concern.split())[:500]
        return ("Могућа неправилност за људску проверу — није утврђена чињеница.\n"
                f"Бирачко место: {case['municipality']}, {case['station']}\n"
                f"Разлог: {one_line}\nРИК: {case['rik_url']}\n"
                f"Јавни предмет (свако са везом може да отвори): {folder_url}")

    @staticmethod
    def viber_text(case: dict, reason: str, folder_url: str) -> str:
        return ("Могућа неправилност за проверу.\n"
                f"{case['municipality']}, БМ {case['station']}: {reason}\n"
                f"Записник и опис: {folder_url}\n"
                f"РИК: {case['rik_url']}")
