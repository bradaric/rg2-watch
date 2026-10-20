import hashlib
import json
import logging
import multiprocessing
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
from rg2watch.publication_service import PublicationJobs
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
        self.share_targets = []
        self.crash = crash
        self.steps = []
        self.folder_creates = []

    def generate_ids(self, count):
        if count == 1:
            return ["app-root-id"]
        assert count == 3
        return self.ids.copy()

    def account_permission_id(self):
        return self.account

    def get_file(self, file_id):
        return self.files.get(file_id)

    def create_root_folder(self, file_id, name):
        self.files.setdefault(file_id, {"id": file_id, "name": name,
                                       "mimeType": ROOT_MIME,
                                       "parents": [self.my_drive_root],
                                       "owners": [{"permissionId": self.account}],
                                       "trashed": False})
        return self.files[file_id]

    def create_folder(self, file_id, name, parent):
        self.folder_creates.append((file_id, name, parent))
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
        self.share_targets.append(folder_id)
        self.public = True
        self._step("share")

    def children_have_public_reader(self, child_ids):
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

    def get_file(self, file_id):
        self.get_calls.append(file_id)
        return self.files.get(file_id)

    def create_root_folder(self, file_id, name):
        self.created += 1
        self.files.setdefault(file_id, {"id": file_id, "name": name, "mimeType": ROOT_MIME,
                                        "parents": ["my-drive-root"],
                                        "owners": [{"permissionId": self.account}],
                                        "trashed": False})
        return self.files[file_id]

    def create_folder(self, file_id, name, parent):
        raise AssertionError("root setup must use root-only default placement")

    def folder_is_public(self, file_id):
        return bool(self.files[file_id].get("public"))


class ProcessRootDrive:
    """Process-safe fake whose remote state survives a worker's hard exit."""
    def __init__(self, state, crash_stage=None):
        self.state = state
        self.crash_stage = crash_stage

    def account_permission_id(self):
        return "process-account"

    def generate_ids(self, count):
        self.state["generated"] += 1
        return ["process-root-id"]

    def get_file(self, file_id):
        if self.crash_stage == "reserved" and file_id == "process-root-id":
            os._exit(71)
        return self.state["files"].get(file_id)

    def create_root_folder(self, file_id, name):
        files = dict(self.state["files"])
        files[file_id] = {
            "id": file_id, "name": name, "mimeType": ROOT_MIME, "parents": ["my-drive-root"],
            "owners": [{"permissionId": "process-account"}], "trashed": False,
        }
        self.state["files"] = files
        self.state["created"] += 1
        if self.crash_stage == "created":
            os._exit(72)

    def folder_is_public(self, file_id):
        return False


def _setup_root_process(token, db, state, start, ready, output, crash_stage=None):
    store = Store(db)
    try:
        if ready is not None:
            ready.put(True)
        if start is not None:
            start.wait()
        root_id = DriveRoot(token, ProcessRootDrive(state, crash_stage)).setup()
        if output is not None:
            output.put(root_id)
    finally:
        store.close()


def test_drive_root_is_shared_by_token_path_and_durable(tmp_path):
    token = tmp_path / "drive-token.json"
    drive = RootDrive()
    first = DriveRoot(token, drive).setup()
    second = DriveRoot(token.resolve(), drive).setup()
    assert first == second == "shared-root-id"
    assert drive.generated == drive.created == 1
    state_path = Path(str(token.resolve()) + ".root.json")
    state = json.loads(state_path.read_text())
    assert state == {"format_version": 2, "root_id": first,
                     "account_permission_id": "account-1", "phase": "created",
                     "my_drive_parent_id": "my-drive-root"}
    assert os.stat(state_path).st_mode & 0o777 == 0o600


def test_reserved_root_recovers_same_id_after_create_crash(tmp_path):
    token = tmp_path / "drive-token.json"
    state_path = Path(str(token.resolve()) + ".root.json")
    state_path.write_text(json.dumps({"format_version": 1, "root_id": "shared-root-id",
                                      "account_permission_id": "account-1", "phase": "reserved"}))
    drive = RootDrive()
    assert DriveRoot(token, drive).setup() == "shared-root-id"
    assert drive.generated == 0 and drive.created == 1
    assert json.loads(state_path.read_text()) == {
        "format_version": 2, "root_id": "shared-root-id",
        "account_permission_id": "account-1", "phase": "created",
        "my_drive_parent_id": "my-drive-root",
    }


def test_version_one_reserved_state_recovers_when_builtin_root_is_inaccessible(tmp_path):
    token = tmp_path / "drive-token.json"
    state_path = Path(str(token.resolve()) + ".root.json")
    state_path.write_text(json.dumps({"format_version": 1, "root_id": "shared-root-id",
                                      "account_permission_id": "account-1",
                                      "phase": "reserved"}))

    class LimitedScopeDrive(RootDrive):
        def get_my_drive_root_id(self):
            raise DriveError("Google Drive My Drive root could not be inspected")

    drive = LimitedScopeDrive()
    assert DriveRoot(token, drive).setup() == "shared-root-id"
    assert drive.generated == 0
    assert "root" not in drive.get_calls


def test_version_one_created_state_without_parent_fails_closed(tmp_path):
    token = tmp_path / "drive-token.json"
    Path(str(token.resolve()) + ".root.json").write_text(json.dumps({
        "format_version": 1, "root_id": "shared-root-id",
        "account_permission_id": "account-1", "phase": "created",
    }))
    drive = RootDrive(files={"shared-root-id": {
        "id": "shared-root-id", "name": "RG2 Watch", "mimeType": ROOT_MIME,
        "parents": ["my-drive-root"], "owners": [{"permissionId": "account-1"}],
        "trashed": False,
    }})

    with pytest.raises(DriveError, match="manual"):
        DriveRoot(token, drive).setup()
    assert drive.get_calls == [] and drive.generated == drive.created == 0


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


def test_separate_processes_contend_and_share_one_root_across_databases(tmp_path):
    token = tmp_path / "shared-token.json"
    with multiprocessing.Manager() as manager:
        state = manager.dict(files={}, generated=0, created=0)
        start = multiprocessing.Event()
        ready = multiprocessing.Queue()
        output = multiprocessing.Queue()
        workers = [multiprocessing.Process(
            target=_setup_root_process,
            args=(token, tmp_path / f"election-{index}.sqlite", state, start, ready, output),
        ) for index in range(2)]
        for worker in workers:
            worker.start()
        assert ready.get(timeout=5) is True
        assert ready.get(timeout=5) is True
        start.set()
        for worker in workers:
            worker.join(10)
            assert worker.exitcode == 0
        assert sorted([output.get(timeout=1), output.get(timeout=1)]) == [
            "process-root-id", "process-root-id"]
        assert state["generated"] == 1
        assert state["created"] == 1


@pytest.mark.parametrize("crash_stage,exitcode", [("reserved", 71), ("created", 72)])
def test_fresh_process_recovers_root_after_durable_crash_point(
        tmp_path, crash_stage, exitcode):
    token = tmp_path / "crash-token.json"
    state_path = Path(str(token.resolve()) + ".root.json")
    with multiprocessing.Manager() as manager:
        state = manager.dict(files={}, generated=0, created=0)
        crashed = multiprocessing.Process(
            target=_setup_root_process,
            args=(token, tmp_path / "crashed.sqlite", state, None, None, None, crash_stage),
        )
        crashed.start()
        crashed.join(10)
        assert crashed.exitcode == exitcode
        durable = json.loads(state_path.read_text(encoding="utf-8"))
        assert durable["root_id"] == "process-root-id"
        assert durable["phase"] == "reserved"

        output = multiprocessing.Queue()
        recovery = multiprocessing.Process(
            target=_setup_root_process,
            args=(token, tmp_path / "recovery.sqlite", state, None, None, output),
        )
        recovery.start()
        recovery.join(10)
        assert recovery.exitcode == 0
        assert output.get(timeout=1) == "process-root-id"
        assert state["generated"] == 1
        assert state["created"] == 1
        assert set(state["files"]) == {"process-root-id"}
        assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "created"


def test_root_recovers_after_remote_create_before_created_state_write(tmp_path):
    token = tmp_path / "drive-token.json"

    class CrashAfterCreate(RootDrive):
        def create_root_folder(self, file_id, name):
            super().create_root_folder(file_id, name)
            raise SimulatedCrash("after remote root create")

    drive = CrashAfterCreate()
    with pytest.raises(SimulatedCrash):
        DriveRoot(token, drive).setup()
    state = json.loads(Path(str(token.resolve()) + ".root.json").read_text())
    assert state["phase"] == "reserved"
    assert DriveRoot(token, drive).setup() == "shared-root-id"
    assert drive.generated == drive.created == 1


def test_root_reconciles_uncertain_create_using_same_preallocated_id(tmp_path):
    token = tmp_path / "drive-token.json"

    class UncertainCreate(RootDrive):
        def create_root_folder(self, file_id, name):
            super().create_root_folder(file_id, name)
            raise DriveError("simulated 409 or uncertain response")

    drive = UncertainCreate()
    assert DriveRoot(token, drive).setup() == "shared-root-id"
    assert drive.generated == drive.created == 1
    assert set(drive.files) == {"shared-root-id"}


def test_retry_after_created_state_write_does_not_create_another_root(tmp_path, monkeypatch):
    token = tmp_path / "drive-token.json"
    drive = RootDrive()
    root = DriveRoot(token, drive)
    real_write = root._write

    def crash_after_write(value):
        real_write(value)
        if value["phase"] == "created":
            raise SimulatedCrash("after created state write")

    monkeypatch.setattr(root, "_write", crash_after_write)
    with pytest.raises(SimulatedCrash, match="state write"):
        root.setup()

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


@pytest.mark.parametrize("change", [
    "name", "parent", "missing_parent", "multiple_parents", "owner", "missing_owner",
    "trashed", "public", "missing",
])
def test_modified_or_inaccessible_root_fails_closed(tmp_path, change):
    token = tmp_path / "drive-token.json"
    drive = RootDrive()
    root_id = DriveRoot(token, drive).setup()
    root = drive.files[root_id]
    if change == "name": root["name"] = "renamed"
    elif change == "parent": root["parents"] = ["elsewhere"]
    elif change == "missing_parent": root["parents"] = []
    elif change == "multiple_parents": root["parents"] = ["my-drive-root", "elsewhere"]
    elif change == "owner": root["owners"] = [{"permissionId": "somebody-else"}]
    elif change == "missing_owner": root["owners"] = []
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
    assert drive.folder_creates[0][0] == "folder-id"
    assert drive.folder_creates[0][2] == "app-root-id"
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
    with pytest.raises(DriveError, match="inspect.*confirm.*empty.*same.*manually"):
        pub.publish(URL, "Замрзнут јавни опис")
    assert store.get_publication(URL)["drive_account_id"] == drive.account
    assert store.get_publication(URL)["legacy_parent_recovery"] == 1
    assert not drive.public and drive.share_calls == 0

    # Account binding must not erase the durable fact that this is the operator's
    # legacy recovery. Every retry before the manual move gives the same guidance.
    with pytest.raises(DriveError, match="inspect.*confirm.*empty.*same.*manually"):
        pub.publish(URL, "Замрзнут јавни опис")
    assert not drive.public and drive.share_calls == 0

    # This simulates the operator moving the same empty folder in Google Drive's UI.
    drive.files["folder-id"]["parents"] = [root_id]
    result = pub.publish(URL, "Замрзнут јавни опис")
    assert result["status"] == "published"
    assert set(drive.ids) <= set(drive.files)
    assert drive.share_calls == 1 and not drive.root_public
    assert drive.share_targets == ["folder-id"]


def test_nonlegacy_case_parent_mismatch_requires_investigation_not_move(tmp_path):
    _, cases = setup_case(tmp_path)
    drive = FakeDrive("folder")
    pub = make_publisher(cases, drive, tmp_path)
    with pytest.raises(SimulatedCrash):
        pub.publish(URL, "Јавни опис")
    drive.files["folder-id"]["parents"] = [drive.my_drive_root]

    with pytest.raises(DriveError) as caught:
        pub.publish(URL, "Јавни опис")

    message = str(caught.value)
    assert "investigate" in message
    assert "empty" not in message
    assert "move" not in message
    assert not drive.public


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


class ApiBackedPublicationService:
    """Drive-v3-shaped fake for the post-share child-permission failure."""
    def __init__(self, files, permissions, account_id):
        self.file_rows = files
        self.permission_rows = permissions
        self.account_id = account_id
        self.permission_creates = []
        self.files_api = self.Files(self)
        self.permissions_api = self.Permissions(self)
        self.about_api = self.About(self)

    def files(self):
        return self.files_api

    def permissions(self):
        return self.permissions_api

    def about(self):
        return self.about_api

    class Files:
        def __init__(self, service):
            self.service = service

        def get(self, fileId, fields):
            return PermissionRequest(dict(self.service.file_rows[fileId]))

        def list(self, q, spaces, fields, pageSize, **kwargs):
            folder_id = q.split("'")[1]
            children = [dict(row) for row in self.service.file_rows.values()
                        if row.get("parents") == [folder_id]]
            return PermissionRequest({"files": children})

    class Permissions:
        def __init__(self, service):
            self.service = service

        def list(self, fileId, fields):
            return PermissionRequest({"permissions": self.service.permission_rows[fileId]})

        def create(self, fileId, body, fields):
            self.service.permission_creates.append((fileId, dict(body)))
            self.service.permission_rows[fileId].append(
                {"id": "created-public-permission", **body})
            return PermissionRequest({"id": "created-public-permission"})

    class About:
        def __init__(self, service):
            self.service = service

        def get(self, fields):
            return PermissionRequest({"user": {"permissionId": self.service.account_id}})


def _wait_for_job(jobs, job_id):
    for _ in range(200):
        result = jobs.job(job_id)
        if result["state"] != "running":
            return result
        time.sleep(.01)
    raise AssertionError("publication job did not finish")


def test_api_backed_post_share_failure_is_correlated_bounded_and_non_mutating(
        tmp_path, caplog):
    store, cases = setup_case(tmp_path)
    secrets = {
        "id": "drive-id-secret-9a8b",
        "url": URL,
        "pdf": "pdf-secret-7c6d",
        "summary": "summary-secret-5e4f",
        "provider": "provider-secret-3a2b",
    }
    claimed = cases.claim(URL, "Јавни опис " + secrets["summary"])
    ids = ["folder-" + secrets["id"], "pdf-" + secrets["id"],
           "text-" + secrets["id"]]
    store.save_drive_ids(URL, *ids)
    row = dict(store.get_publication(URL))
    pdf = Path(row["staged_pdf_path"]).read_bytes() + secrets["pdf"].encode()
    Path(row["staged_pdf_path"]).write_bytes(pdf)
    store._db.execute("UPDATE publications SET pdf_sha256 = ? WHERE url = ?",
                      (hashlib.sha256(pdf).hexdigest(), URL))
    store._db.commit()
    row = dict(store.get_publication(URL))
    case = json.loads(row["case_json"])
    account = "account-" + secrets["id"]
    root_id = "root-" + secrets["id"]
    folder_name = f"RG2 предмет — {case['municipality']} БМ {case['station']}"
    case_text = DrivePublisher._case_text(case, row["summary"], row["pdf_sha256"])
    files = {
        root_id: {"id": root_id, "name": "RG2 Watch", "mimeType": ROOT_MIME,
                  "parents": ["my-drive-" + secrets["id"]],
                  "owners": [{"permissionId": account}], "trashed": False},
        ids[0]: {"id": ids[0], "name": folder_name, "mimeType": ROOT_MIME,
                 "parents": [root_id], "owners": [{"permissionId": account}],
                 "trashed": False},
        ids[1]: {"id": ids[1], "name": "документ.pdf", "mimeType": "application/pdf",
                 "parents": [ids[0]], "md5Checksum": hashlib.md5(pdf).hexdigest(),
                 "trashed": False},
        ids[2]: {"id": ids[2], "name": "опис-предмета.txt", "mimeType": "text/plain",
                 "parents": [ids[0]], "md5Checksum": hashlib.md5(case_text).hexdigest(),
                 "trashed": False},
    }
    owner = {"id": "permission-" + secrets["provider"], "type": "user", "role": "owner",
             "emailAddress": secrets["provider"]}
    permissions = {root_id: [owner.copy()], ids[0]: [owner.copy()],
                   ids[1]: [owner.copy()], ids[2]: [owner.copy()]}
    service = ApiBackedPublicationService(files, permissions, account)
    adapter = GoogleDriveAdapter(service)
    token = tmp_path / "api-backed-token.json"
    Path(str(token.resolve()) + ".root.json").write_text(json.dumps({
        "format_version": 2, "root_id": root_id, "account_permission_id": account,
        "phase": "created", "my_drive_parent_id": "my-drive-" + secrets["id"],
    }))
    publisher = DrivePublisher(cases, adapter, DriveRoot(token, adapter))
    jobs = PublicationJobs(cases, publisher, workers=1)
    try:
        with caplog.at_level(logging.INFO):
            job_id = jobs.submit_publish(URL, row["summary"], True)
            result = _wait_for_job(jobs, job_id)
    finally:
        jobs.close()

    assert result == {"id": job_id, "state": "error", "kind": "publication",
                      "error": "Drive account policy did not expose both folder children"}
    pending = store.get_publication(URL)
    assert pending["phase"] == "uploaded"
    assert [pending["folder_id"], pending["pdf_file_id"], pending["text_file_id"]] == ids
    assert service.permission_creates == [(ids[0], {"type": "anyone", "role": "reader"})]
    logs = caplog.text
    for expected in ("publication start", "stage=uploaded", "stage=folder-public",
                     "child=pdf", "child=text", "type=user role=owner",
                     "anyone_reader=false", "publication fail"):
        assert expected in logs
    assert logs.count("diagnostic_id=" + job_id) >= 7
    assert "exception=DriveError" in logs
    for secret in secrets.values():
        assert secret not in logs
    assert secrets["provider"] not in result["error"]


def test_adapter_creates_app_root_with_default_my_drive_placement():
    class FilesService:
        def __init__(self):
            self.request = None

        def files(self):
            return self

        def create(self, **kwargs):
            self.request = kwargs
            return PermissionRequest({"id": "reserved-root", "parents": ["my-drive-root"]})

    service = FilesService()
    created = GoogleDriveAdapter(service).create_root_folder("reserved-root", "RG2 Watch")

    assert service.request["body"] == {
        "id": "reserved-root", "name": "RG2 Watch", "mimeType": ROOT_MIME,
    }
    assert service.request["fields"] == "id,parents"
    assert created == {"id": "reserved-root", "parents": ["my-drive-root"]}


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


def test_my_drive_children_report_effective_anyone_reader_without_permission_details():
    anyone_reader = {"id": "anyoneWithLink", "type": "anyone", "role": "reader"}
    service = PermissionService({
        "pdf": [anyone_reader],
        "text": [{"id": "owner", "type": "user", "role": "owner"}],
    })
    adapter = GoogleDriveAdapter(service)
    assert not adapter.children_have_public_reader(["pdf", "text"])
    service.by_file["text"] = [anyone_reader]
    assert adapter.children_have_public_reader(["pdf", "text"])


def test_child_effective_access_requires_anyone_reader_role():
    service = PermissionService({
        "pdf": [{"id": "anyoneWithLink", "type": "anyone", "role": "writer"}],
        "text": [{"id": "anyoneWithLink", "type": "anyone", "role": "reader"}],
    })
    assert not GoogleDriveAdapter(service).children_have_public_reader(["pdf", "text"])


def test_child_permission_logs_are_bounded_and_enum_only(caplog):
    malicious_type = "domain\nAuthorization: Bearer secret-type"
    malicious_role = "owner secret-role"
    permissions = [
        {"id": f"permission-{index}", "type": malicious_type, "role": malicious_role,
         "emailAddress": f"person-{index}@secret.example"}
        for index in range(12)
    ]
    service = PermissionService({
        "pdf-secret-id": permissions,
        "text-secret-id": [{"type": "anyone", "role": "commenter"}],
    })

    with caplog.at_level(logging.INFO, logger="rg2watch.drive_publish"):
        assert not GoogleDriveAdapter(service).children_have_public_reader(
            ["pdf-secret-id", "text-secret-id"])

    logs = caplog.text
    assert "child=pdf count=12" in logs
    assert "child=text count=1" in logs
    assert logs.count("type=other role=other") == 10
    assert "type=anyone role=commenter" in logs
    assert "anyone_reader=false" in logs
    for forbidden in (malicious_type, malicious_role, "@secret.example", "permission-",
                      "pdf-secret-id", "text-secret-id"):
        assert forbidden not in logs


def test_child_permission_api_error_logs_only_sanitized_class(caplog):
    provider_secret = "provider response secret-token-value"

    class FailingRequest:
        def execute(self):
            raise RuntimeError(provider_secret)

    class FailingPermissions:
        def permissions(self):
            return self

        def list(self, fileId, fields):
            return FailingRequest()

    with caplog.at_level(logging.INFO, logger="rg2watch.drive_publish"):
        with pytest.raises(DriveError) as caught:
            GoogleDriveAdapter(FailingPermissions()).children_have_public_reader(
                ["secret-child-id", "unreached-child-id"])

    assert str(caught.value) == "Drive permissions could not be inspected"
    assert "child-check failed" in caplog.text
    assert "child=pdf" in caplog.text
    assert "exception=DriveError" in caplog.text
    for forbidden in (provider_secret, "secret-token-value", "secret-child-id"):
        assert forbidden not in caplog.text


def test_direct_publication_attempts_get_distinct_context_and_reset(tmp_path, caplog):
    import re
    from rg2watch.drive_publish import _publication_diagnostic_id

    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    _, first_cases = setup_case(tmp_path / "first")
    _, second_cases = setup_case(tmp_path / "second")
    with caplog.at_level(logging.INFO):
        make_publisher(first_cases, FakeDrive(), tmp_path / "first").publish(URL, "Јавни опис")
        make_publisher(second_cases, FakeDrive(), tmp_path / "second").publish(URL, "Јавни опис")

    ids = set(re.findall(r"diagnostic_id=([0-9a-f]{32})", caplog.text))
    assert len(ids) == 2
    assert _publication_diagnostic_id.get() is None


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
