import hashlib
import json
import os
from types import SimpleNamespace
import threading
import time
from pathlib import Path

import pytest

from rg2watch.case_handoff import CaseError, CaseHandoff
from rg2watch.config import Config
from rg2watch.drive_publish import DriveError, DrivePublisher, GoogleDriveAdapter
from rg2watch.drive_root import DriveRoot
from rg2watch.store import Store
from rg2watch import cli
from test_browse import BASE, P, add, doc


URL = BASE + "/extfile/drive.pdf"
ROOT_MIME = "application/vnd.google-apps.folder"


class SimulatedCrash(BaseException):
    pass


class FakeDrive:
    def __init__(self, crash=None):
        self.ids = ["folder-id", "pdf-id", "text-id"]
        self.account = "account-1"
        self.my_drive_root = "my-drive-root"
        self.files = {"app-root-id": {"id": "app-root-id", "name": "RG2 Watch",
                                      "mimeType": ROOT_MIME, "parents": [self.my_drive_root],
                                      "owners": [{"permissionId": self.account}], "trashed": False}}
        self.public = False
        self.root_public = False
        self.share_calls = 0
        self.crash = crash
        self.steps = []

    def generate_ids(self, count):
        if count == 1:
            return ["app-root-id"]
        assert count == 3
        return self.ids.copy()

    def account_permission_id(self):
        return self.account

    def get_my_drive_root_id(self):
        return self.my_drive_root

    def get_file(self, file_id):
        return self.files.get(file_id)

    def create_folder(self, file_id, name, parent):
        self.files.setdefault(file_id, {"id": file_id, "name": name,
                                       "mimeType": "application/vnd.google-apps.folder",
                                       "parents": [parent], "owners": [{"permissionId": self.account}],
                                       "trashed": False})
        if file_id != "app-root-id":
            self._step("folder")

    def upload(self, file_id, name, mime, parent, data):
        self.files.setdefault(file_id, {"id": file_id, "name": name, "mimeType": mime,
                                       "parents": [parent],
                                       "md5Checksum": hashlib.md5(data).hexdigest()})
        self._step("pdf" if mime == "application/pdf" else "text")

    def folder_is_public(self, folder_id):
        return self.root_public if folder_id == "app-root-id" else self.public

    def list_children(self, folder_id):
        return [f for f in self.files.values() if f.get("parents") == [folder_id]]

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


class RootDrive:
    """In-memory Drive used only for root-coordinator tests (never contacts Google)."""
    def __init__(self, account="account-1", files=None):
        self.account = account
        self.files = files if files is not None else {}
        self.generated = 0
        self.created = 0
        self.get_calls = []

    def account_permission_id(self):
        return self.account

    def generate_ids(self, count):
        self.generated += 1
        return ["shared-root-id"]

    def get_my_drive_root_id(self):
        return "my-drive-root"

    def get_file(self, file_id):
        self.get_calls.append(file_id)
        return self.files.get(file_id)

    def create_folder(self, file_id, name, parent):
        self.created += 1
        self.files.setdefault(file_id, {"id": file_id, "name": name, "mimeType": ROOT_MIME,
                                        "parents": [parent], "owners": [{"permissionId": self.account}],
                                        "trashed": False})

    def folder_is_public(self, file_id):
        return bool(self.files[file_id].get("public"))


def test_drive_root_is_shared_by_token_path_and_durable(tmp_path):
    token = tmp_path / "drive-token.json"
    drive = RootDrive()
    first = DriveRoot(token, drive).setup()
    second = DriveRoot(token.resolve(), drive).setup()
    assert first == second == "shared-root-id"
    assert drive.generated == drive.created == 1
    state_path = Path(str(token.resolve()) + ".root.json")
    state = json.loads(state_path.read_text())
    assert state == {"format_version": 1, "root_id": first,
                     "account_permission_id": "account-1", "phase": "created"}
    assert os.stat(state_path).st_mode & 0o777 == 0o600


def test_reserved_root_recovers_same_id_after_create_crash(tmp_path):
    token = tmp_path / "drive-token.json"
    state_path = Path(str(token.resolve()) + ".root.json")
    state_path.write_text(json.dumps({"format_version": 1, "root_id": "shared-root-id",
                                      "account_permission_id": "account-1", "phase": "reserved"}))
    drive = RootDrive()
    assert DriveRoot(token, drive).setup() == "shared-root-id"
    assert drive.generated == 0 and drive.created == 1


def test_competing_databases_share_one_root_for_the_same_token(tmp_path):
    token = tmp_path / "shared-token.json"
    drive = RootDrive()
    results = []
    start = threading.Barrier(3)

    def setup_from_separate_database():
        # Store paths deliberately differ; root coordination depends only on the token path.
        Store(tmp_path / f"election-{threading.get_ident()}.sqlite").close()
        start.wait()
        results.append(DriveRoot(token, drive).setup())

    workers = [threading.Thread(target=setup_from_separate_database) for _ in range(2)]
    for worker in workers: worker.start()
    start.wait()
    for worker in workers: worker.join(5)
    assert results == ["shared-root-id", "shared-root-id"]
    assert drive.generated == drive.created == 1


def test_root_recovers_after_remote_create_before_created_state_write(tmp_path):
    token = tmp_path / "drive-token.json"

    class CrashAfterCreate(RootDrive):
        def create_folder(self, file_id, name, parent):
            super().create_folder(file_id, name, parent)
            raise SimulatedCrash("after remote root create")

    drive = CrashAfterCreate()
    with pytest.raises(SimulatedCrash):
        DriveRoot(token, drive).setup()
    state = json.loads(Path(str(token.resolve()) + ".root.json").read_text())
    assert state["phase"] == "reserved"
    assert DriveRoot(token, drive).setup() == "shared-root-id"
    assert drive.generated == drive.created == 1


def test_account_switch_is_refused_before_drive_file_operations(tmp_path):
    token = tmp_path / "drive-token.json"
    first = RootDrive()
    DriveRoot(token, first).setup()
    switched = RootDrive(account="different-account", files=first.files)
    with pytest.raises(DriveError, match="authorized Google account"):
        DriveRoot(token, switched).setup()
    assert switched.generated == switched.created == 0
    assert switched.get_calls == []


def test_pending_account_mismatch_is_refused_before_any_file_get(tmp_path):
    store, cases = setup_case(tmp_path)

    class TrackingDrive(FakeDrive):
        def __init__(self):
            super().__init__()
            self.get_calls = []

        def get_file(self, file_id):
            self.get_calls.append(file_id)
            return super().get_file(file_id)

    drive = TrackingDrive()
    root = DriveRoot(tmp_path / "drive-token.json", drive)
    root.setup()
    cases.claim(URL, "Јавни опис")
    store.bind_publication_account(URL, "different-account")
    drive.get_calls.clear()
    with pytest.raises(DriveError, match="different Google Drive account"):
        DrivePublisher(cases, drive, root).publish(URL, "Јавни опис")
    assert drive.get_calls == []


@pytest.mark.parametrize("change", ["name", "parent", "owner", "trashed", "public", "missing"])
def test_modified_or_inaccessible_root_fails_closed(tmp_path, change):
    token = tmp_path / "drive-token.json"
    drive = RootDrive()
    root_id = DriveRoot(token, drive).setup()
    root = drive.files[root_id]
    if change == "name": root["name"] = "renamed"
    elif change == "parent": root["parents"] = ["elsewhere"]
    elif change == "owner": root["owners"] = [{"permissionId": "somebody-else"}]
    elif change == "trashed": root["trashed"] = True
    elif change == "public": root["public"] = True
    else: del drive.files[root_id]
    with pytest.raises(DriveError):
        DriveRoot(token, drive).require()
    assert drive.generated == drive.created == 1


def test_drive_root_cli_explicitly_sets_up_and_prints_link(tmp_path, monkeypatch, capsys):
    token = tmp_path / "token.json"
    token.write_text("not-read-by-test")
    drive = RootDrive()
    cfg = Config(drive_token_path=token)
    monkeypatch.setattr(cli, "_config", lambda args: cfg)
    import rg2watch.drive_publish as module
    monkeypatch.setattr(module, "configured_drive", lambda path: drive)
    assert cli._cmd_drive_root(SimpleNamespace()) == 0
    assert capsys.readouterr().out.strip().endswith("/shared-root-id")
    assert cli._parser().parse_args(["drive-root"]).cmd == "drive-root"


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


def make_publisher(cases, drive, tmp_path):
    root = DriveRoot(tmp_path / "drive-token.json", drive)
    root.setup()
    return DrivePublisher(cases, drive, root)


def test_folder_is_shared_only_after_both_verified_uploads(tmp_path):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive()
    result = make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert drive.steps == ["folder", "pdf", "text", "share"]
    assert result["url"] == "https://drive.google.com/drive/folders/folder-id"
    assert result["viber_text"].startswith("Могућа неправилност")
    assert s.get_publication(URL)["phase"] == "published"


@pytest.mark.parametrize("crash", ["folder", "pdf", "text", "share"])
def test_retry_after_each_remote_crash_reuses_ids_and_one_folder(tmp_path, crash):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive(crash)
    with pytest.raises(SimulatedCrash, match="simulated crash"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    result = make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert result["url"].endswith("folder-id")
    assert set(drive.files) == set(drive.ids) | {"app-root-id"}
    assert s.get_publication(URL)["folder_id"] == "folder-id"
    assert drive.share_calls == 1


def test_changed_retry_and_mutated_stage_fail_without_sharing(tmp_path):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive("folder")
    with pytest.raises(SimulatedCrash):
        make_publisher(cases, drive, tmp_path).publish(URL, "Први опис")
    with pytest.raises(CaseError, match="frozen"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Други опис")
    Path(s.get_publication(URL)["staged_pdf_path"]).write_bytes(b"%PDF-changed")
    with pytest.raises(CaseError, match="frozen"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Први опис")
    assert not drive.public


def test_ok_before_share_blocks_but_ok_after_existing_public_is_historical(tmp_path):
    s, cases = setup_case(tmp_path)
    drive = FakeDrive("text")
    with pytest.raises(SimulatedCrash):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    s.set_review(URL, "ok")
    with pytest.raises(DriveError, match="escalate"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert drive.share_calls == 0
    s.set_review(URL, "escalate")
    drive.crash = "share"
    with pytest.raises(SimulatedCrash):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert drive.public
    s.set_review(URL, "ok")
    result = make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert result["url"].endswith("folder-id") and drive.share_calls == 1
    repeated = make_publisher(cases, drive, tmp_path).publish(URL, "ignored changed summary")
    assert repeated == result


def test_remote_metadata_mismatch_fails_closed(tmp_path):
    _, cases = setup_case(tmp_path)
    drive = FakeDrive("folder")
    with pytest.raises(SimulatedCrash):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    drive.files["folder-id"]["name"] = "foreign"
    with pytest.raises(DriveError, match="metadata"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert not drive.public


def test_legacy_pending_folder_requires_manual_move_then_reuses_same_id(tmp_path):
    store, cases = setup_case(tmp_path)
    drive = FakeDrive()
    root = DriveRoot(tmp_path / "drive-token.json", drive)
    root_id = root.setup()
    cases.claim(URL, "Замрзнут јавни опис")
    store.save_drive_ids(URL, *drive.ids)
    case = cases._case(URL)
    drive.files["folder-id"] = {
        "id": "folder-id",
        "name": f"RG2 предмет — {case['municipality']} БМ {case['station']}", "mimeType": ROOT_MIME,
        "parents": [drive.my_drive_root], "owners": [{"permissionId": drive.account}],
        "trashed": False,
    }
    pub = DrivePublisher(cases, drive, root)
    with pytest.raises(DriveError, match="move.*manually"):
        pub.publish(URL, "Замрзнут јавни опис")
    assert store.get_publication(URL)["drive_account_id"] == drive.account
    assert not drive.public and drive.share_calls == 0

    # This simulates the operator moving the same empty folder in Google Drive's UI.
    drive.files["folder-id"]["parents"] = [root_id]
    result = pub.publish(URL, "Замрзнут јавни опис")
    assert result["status"] == "published"
    assert set(drive.ids) <= set(drive.files)
    assert drive.share_calls == 1 and not drive.root_public


def test_visible_unexpected_child_blocks_case_upload_and_share(tmp_path):
    store, cases = setup_case(tmp_path)
    drive = FakeDrive("folder")
    pub = make_publisher(cases, drive, tmp_path)
    with pytest.raises(SimulatedCrash):
        pub.publish(URL, "Јавни опис")
    drive.files["unexpected"] = {"id": "unexpected", "name": "unknown",
                                  "parents": ["folder-id"]}
    with pytest.raises(DriveError, match="unexpected"):
        pub.publish(URL, "Јавни опис")
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
    publisher = make_publisher(cases, drive, tmp_path)
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
    assert make_publisher(cases, drive, tmp_path).publish(URL, "ignored") == publish_result[0]
    assert drive.share_calls == 1


class PermissionRequest:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


def test_adapter_sanitizes_generate_ids_provider_failure():
    class FailingRequest:
        def execute(self):
            raise RuntimeError("provider debug: bearer secret-token-value")

    class FilesService:
        def files(self):
            return self

        def generateIds(self, **kwargs):
            return FailingRequest()

    with pytest.raises(DriveError) as caught:
        GoogleDriveAdapter(FilesService()).generate_ids(1)
    assert str(caught.value) == "Drive file ID preallocation did not complete; retry safely"
    assert "provider debug" not in str(caught.value)
    assert "secret-token-value" not in str(caught.value)


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


def test_adapter_inspects_every_page_of_app_visible_children():
    class FilesService:
        def files(self):
            return self

        def list(self, **kwargs):
            token = kwargs.get("pageToken")
            payload = ({"files": [{"id": "first"}], "nextPageToken": "next"}
                       if token is None else {"files": [{"id": "unexpected"}]})
            return PermissionRequest(payload)

    assert [f["id"] for f in GoogleDriveAdapter(FilesService()).list_children("folder")] == [
        "first", "unexpected"]


@pytest.mark.parametrize("failure", ["folder", "pdf", "text"])
def test_drive_failure_before_sharing_leaves_folder_restricted(tmp_path, failure):
    _, cases = setup_case(tmp_path)

    class FailingDrive(FakeDrive):
        def create_folder(self, file_id, name, parent):
            if failure == "folder":
                raise DriveError("injected Drive failure")
            super().create_folder(file_id, name, parent)

        def upload(self, file_id, name, mime, parent, data):
            kind = "pdf" if mime == "application/pdf" else "text"
            if failure == kind:
                raise DriveError("injected Drive failure")
            super().upload(file_id, name, mime, parent, data)

    drive = FailingDrive()
    with pytest.raises(DriveError, match="injected"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
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
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert not drive.public
    result = make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert result["status"] == "published"
    assert set(drive.files) == set(drive.ids) | {"app-root-id"}


def test_crash_after_id_preallocation_persists_ids_before_any_create(tmp_path):
    store, cases = setup_case(tmp_path)

    class CrashBeforeCreate(FakeDrive):
        def __init__(self):
            super().__init__()
            self.once = True

        def get_file(self, file_id):
            if self.once and file_id == "folder-id":
                self.once = False
                raise SimulatedCrash("after ID persistence")
            return super().get_file(file_id)

    drive = CrashBeforeCreate()
    with pytest.raises(SimulatedCrash, match="ID persistence"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    row = store.get_publication(URL)
    assert [row["folder_id"], row["pdf_file_id"], row["text_file_id"]] == drive.ids
    assert set(drive.files) == {"app-root-id"}
    assert make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")["status"] == "published"


def test_crash_after_share_before_finalize_recovers_even_after_ok(tmp_path, monkeypatch):
    store, cases = setup_case(tmp_path)
    drive = FakeDrive()
    real_finalize = store.finalize_publication

    def crash_before_finalize(url, final_url, viber_text):
        monkeypatch.setattr(store, "finalize_publication", real_finalize)
        raise SimulatedCrash("before publication commit")

    monkeypatch.setattr(store, "finalize_publication", crash_before_finalize)
    with pytest.raises(SimulatedCrash, match="publication commit"):
        make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert drive.public
    store.set_review(URL, "ok")
    result = make_publisher(cases, drive, tmp_path).publish(URL, "Јавни опис")
    assert result["status"] == "published"
    assert drive.share_calls == 1
