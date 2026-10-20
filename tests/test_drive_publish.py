import hashlib
from pathlib import Path

import pytest

from rg2watch.case_handoff import CaseError, CaseHandoff
from rg2watch.config import Config
from rg2watch.drive_publish import DriveError, DrivePublisher
from rg2watch.store import Store
from test_browse import BASE, P, add, doc


URL = BASE + "/extfile/drive.pdf"


class SimulatedCrash(BaseException):
    pass


class FakeDrive:
    def __init__(self, crash=None):
        self.ids = ["folder-id", "pdf-id", "text-id"]
        self.files = {}
        self.public = False
        self.share_calls = 0
        self.crash = crash
        self.steps = []

    def generate_ids(self, count):
        assert count == 3
        return self.ids.copy()

    def get_file(self, file_id):
        return self.files.get(file_id)

    def create_folder(self, file_id, name):
        self.files.setdefault(file_id, {"id": file_id, "name": name,
                                       "mimeType": "application/vnd.google-apps.folder",
                                       "parents": []})
        self._step("folder")

    def upload(self, file_id, name, mime, parent, data):
        self.files.setdefault(file_id, {"id": file_id, "name": name, "mimeType": mime,
                                       "parents": [parent],
                                       "md5Checksum": hashlib.md5(data).hexdigest()})
        self._step("pdf" if mime == "application/pdf" else "text")

    def folder_is_public(self, folder_id):
        return self.public

    def share_folder(self, folder_id):
        self.share_calls += 1
        self.public = True
        self._step("share")

    def children_inherit_public(self, folder_id, child_ids):
        return self.public and all(i in self.files for i in child_ids)

    def _step(self, name):
        self.steps.append(name)
        if self.crash == name:
            self.crash = None
            raise SimulatedCrash("simulated crash after " + name)


def setup_case(tmp_path):
    db = tmp_path / "watch.sqlite"
    s = Store(db)
    d = doc(URL)
    add(s, d)
    s.set_review(URL, "escalate", "опрезна напомена")
    pdfs = tmp_path / "pdfs"; pdfs.mkdir()
    (pdfs / (hashlib.sha256(URL.encode()).hexdigest() + ".pdf")).write_bytes(b"%PDF-1.4\nexact")
    cfg = Config(db_path=db)
    return s, CaseHandoff(s, cfg)


def test_folder_is_shared_only_after_both_verified_uploads(tmp_path):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive()
    result = DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert drive.steps == ["folder", "pdf", "text", "share"]
    assert result["url"] == "https://drive.google.com/drive/folders/folder-id"
    assert result["viber_text"].startswith("Могућа неправилност")
    assert s.get_publication(URL)["phase"] == "published"


@pytest.mark.parametrize("crash", ["folder", "pdf", "text", "share"])
def test_retry_after_each_remote_crash_reuses_ids_and_one_folder(tmp_path, crash):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive(crash)
    with pytest.raises(SimulatedCrash, match="simulated crash"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    result = DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert result["url"].endswith("folder-id")
    assert set(drive.files) == set(drive.ids)
    assert s.get_publication(URL)["folder_id"] == "folder-id"
    assert drive.share_calls == 1


def test_changed_retry_and_mutated_stage_fail_without_sharing(tmp_path):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive("folder")
    with pytest.raises(SimulatedCrash):
        DrivePublisher(cases, drive).publish(URL, "Први опис")
    with pytest.raises(CaseError, match="frozen"):
        DrivePublisher(cases, drive).publish(URL, "Други опис")
    Path(s.get_publication(URL)["staged_pdf_path"]).write_bytes(b"%PDF-changed")
    with pytest.raises(CaseError, match="frozen"):
        DrivePublisher(cases, drive).publish(URL, "Први опис")
    assert not drive.public


def test_ok_before_share_blocks_but_ok_after_existing_public_is_historical(tmp_path):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive("text")
    with pytest.raises(SimulatedCrash):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    s.set_review(URL, "ok")
    with pytest.raises(DriveError, match="escalate"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert drive.share_calls == 0
    s.set_review(URL, "escalate")
    drive.crash = "share"
    with pytest.raises(SimulatedCrash):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert drive.public
    s.set_review(URL, "ok")
    result = DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert result["url"].endswith("folder-id") and drive.share_calls == 1
    repeated = DrivePublisher(cases, drive).publish(URL, "ignored changed summary")
    assert repeated == result


def test_remote_metadata_mismatch_fails_closed(tmp_path):
    _, cases = setup_case(tmp_path)
    drive = FakeDrive("folder")
    with pytest.raises(SimulatedCrash):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    drive.files["folder-id"]["name"] = "foreign"
    with pytest.raises(DriveError, match="metadata"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert not drive.public
