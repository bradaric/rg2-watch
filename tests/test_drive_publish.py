import hashlib
import threading
import time
from pathlib import Path

import pytest

from rg2watch.case_handoff import CaseError, CaseHandoff
from rg2watch.config import Config
from rg2watch.drive_publish import DriveError, DrivePublisher, GoogleDriveAdapter
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


class BlockingShareDrive(FakeDrive):
    def __init__(self):
        super().__init__()
        self.sharing = threading.Event()
        self.release = threading.Event()

    def share_folder(self, folder_id):
        self.sharing.set()
        assert self.release.wait(5)
        super().share_folder(folder_id)


def test_review_started_during_share_waits_until_publication_is_committed(tmp_path):
    first, cases = setup_case(tmp_path)
    drive = BlockingShareDrive()
    publisher = DrivePublisher(cases, drive)
    publish_result = []
    publish_thread = threading.Thread(
        target=lambda: publish_result.append(publisher.publish(URL, "Јавни опис")))
    publish_thread.start()
    assert drive.sharing.wait(5)

    second = Store(first.path)
    review_result = []
    review_thread = threading.Thread(target=lambda: review_result.append(second.set_review(URL, "ok")))
    review_thread.start()
    time.sleep(.05)
    assert review_thread.is_alive()
    assert first.get_publication(URL)["phase"] != "published"

    drive.release.set()
    publish_thread.join(5)
    review_thread.join(5)
    assert publish_result[0]["status"] == "published"
    assert review_result[0]["verdict"] == "ok"
    assert DrivePublisher(cases, drive).publish(URL, "ignored") == publish_result[0]
    assert drive.share_calls == 1


class PermissionRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class PermissionService:
    def __init__(self, by_file):
        self.by_file = by_file

    def permissions(self):
        return self

    def list(self, fileId, fields):
        return PermissionRequest({"permissions": self.by_file[fileId]})


def test_child_public_permission_must_be_inherited_from_expected_folder():
    anyone_from = lambda parent: {
        "type": "anyone", "role": "reader",
        "permissionDetails": [{"inherited": True, "inheritedFrom": parent}],
    }
    service = PermissionService({
        "pdf": [anyone_from("folder")],
        "text": [anyone_from("other-public-folder")],
    })
    adapter = GoogleDriveAdapter(service)
    assert not adapter.children_inherit_public("folder", ["pdf", "text"])
    service.by_file["text"] = [anyone_from("folder")]
    assert adapter.children_inherit_public("folder", ["pdf", "text"])


@pytest.mark.parametrize("failure", ["folder", "pdf", "text"])
def test_drive_failure_before_sharing_leaves_folder_restricted(tmp_path, failure):
    _, cases = setup_case(tmp_path)

    class FailingDrive(FakeDrive):
        def create_folder(self, file_id, name):
            if failure == "folder":
                raise DriveError("injected Drive failure")
            super().create_folder(file_id, name)

        def upload(self, file_id, name, mime, parent, data):
            kind = "pdf" if mime == "application/pdf" else "text"
            if failure == kind:
                raise DriveError("injected Drive failure")
            super().upload(file_id, name, mime, parent, data)

    drive = FailingDrive()
    with pytest.raises(DriveError, match="injected"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert not drive.public and drive.share_calls == 0


def test_crash_before_uploaded_phase_update_recovers_frozen_case(tmp_path, monkeypatch):
    store, cases = setup_case(tmp_path)
    drive = FakeDrive()
    real_advance = store.advance_publication

    def crash_before_update(url, phase):
        monkeypatch.setattr(store, "advance_publication", real_advance)
        raise SimulatedCrash("before uploaded phase update")

    monkeypatch.setattr(store, "advance_publication", crash_before_update)
    with pytest.raises(SimulatedCrash, match="phase update"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert not drive.public
    result = DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert result["status"] == "published"
    assert set(drive.files) == set(drive.ids)


def test_crash_after_id_preallocation_persists_ids_before_any_create(tmp_path):
    store, cases = setup_case(tmp_path)

    class CrashBeforeCreate(FakeDrive):
        def __init__(self):
            super().__init__()
            self.once = True

        def get_file(self, file_id):
            if self.once:
                self.once = False
                raise SimulatedCrash("after ID persistence")
            return super().get_file(file_id)

    drive = CrashBeforeCreate()
    with pytest.raises(SimulatedCrash, match="ID persistence"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    row = store.get_publication(URL)
    assert [row["folder_id"], row["pdf_file_id"], row["text_file_id"]] == drive.ids
    assert drive.files == {}
    assert DrivePublisher(cases, drive).publish(URL, "Јавни опис")["status"] == "published"


def test_crash_after_share_before_finalize_recovers_even_after_ok(tmp_path, monkeypatch):
    store, cases = setup_case(tmp_path)
    drive = FakeDrive()
    real_finalize = store.finalize_publication

    def crash_before_finalize(url, final_url, viber_text):
        monkeypatch.setattr(store, "finalize_publication", real_finalize)
        raise SimulatedCrash("before publication commit")

    monkeypatch.setattr(store, "finalize_publication", crash_before_finalize)
    with pytest.raises(SimulatedCrash, match="publication commit"):
        DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert drive.public
    store.set_review(URL, "ok")
    result = DrivePublisher(cases, drive).publish(URL, "Јавни опис")
    assert result["status"] == "published"
    assert drive.share_calls == 1
