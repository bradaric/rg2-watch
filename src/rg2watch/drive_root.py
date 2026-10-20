"""Durable, account-bound coordination for the app's private Drive root."""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .drive_publish import DriveError, FOLDER_MIME


ROOT_NAME = "RG2 Watch"
STATE_VERSION = 2


class DriveRoot:
    """One private app-created root shared by every DB using a token path."""

    def __init__(self, token_path: Path, drive):
        token = Path(token_path).expanduser().resolve()
        self.drive = drive
        self.state_path = Path(str(token) + ".root.json")
        self.lock_path = Path(str(token) + ".root.lock")

    @contextmanager
    def _locked(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(self.lock_path, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _read(self) -> dict | None:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise DriveError("Google Drive root state is invalid; investigate it manually") from exc
        base = {"format_version", "root_id", "account_permission_id", "phase"}
        valid_base = (isinstance(value, dict)
                      and isinstance(value.get("root_id"), str) and bool(value["root_id"])
                      and isinstance(value.get("account_permission_id"), str)
                      and bool(value["account_permission_id"])
                      and value.get("phase") in ("reserved", "created"))
        if not valid_base:
            raise DriveError("Google Drive root state is invalid; investigate it manually")
        if value.get("format_version") == 1 and set(value) == base:
            if value["phase"] == "reserved":
                return value
            raise DriveError("Google Drive root state has no verified parent; investigate it manually")
        expected = base | {"my_drive_parent_id"}
        parent = value.get("my_drive_parent_id")
        valid_parent = (value["phase"] == "reserved" and parent is None
                        or value["phase"] == "created"
                        and isinstance(parent, str) and bool(parent))
        if (value.get("format_version") != STATE_VERSION or set(value) != expected
                or not valid_parent):
            raise DriveError("Google Drive root state is invalid; investigate it manually")
        return value

    def _write(self, value: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=self.state_path.name + ".", suffix=".tmp",
                                   dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(value, fh, separators=(",", ":"), sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
            os.chmod(self.state_path, 0o600)
            directory = os.open(self.state_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _account(self) -> str:
        account = self.drive.account_permission_id()
        if not isinstance(account, str) or not account:
            raise DriveError("Google Drive did not provide an authorized account identity")
        return account

    def _verify_observed(self, state: dict, account: str,
                         expected_parent: str | None = None) -> str:
        if state["account_permission_id"] != account:
            raise DriveError("the authorized Google account differs from the registered Drive root account")
        root = self.drive.get_file(state["root_id"])
        if root is None:
            raise DriveError("the registered Google Drive root is missing or inaccessible; investigate manually")
        owners = root.get("owners") or []
        parents = root.get("parents") or []
        valid = (root.get("id") == state["root_id"] and root.get("name") == ROOT_NAME
                 and root.get("mimeType") == FOLDER_MIME
                 and isinstance(parents, list)
                 and len(parents) == 1 and isinstance(parents[0], str) and bool(parents[0])
                 and not root.get("trashed") and isinstance(owners, list) and len(owners) == 1
                 and isinstance(owners[0], dict)
                 and owners[0].get("permissionId") == account)
        if expected_parent is not None:
            valid = valid and parents == [expected_parent]
        if not valid:
            raise DriveError("the registered Google Drive root metadata or ownership has changed")
        if self.drive.folder_is_public(state["root_id"]):
            raise DriveError("the RG2 Watch root must remain private; remove public access before retrying")
        return parents[0]

    def _verify(self, state: dict, account: str) -> str:
        self._verify_observed(state, account, state["my_drive_parent_id"])
        return state["root_id"]

    def setup(self) -> str:
        account = self._account()
        with self._locked():
            state = self._read()
            if state is not None and state["account_permission_id"] != account:
                raise DriveError("the authorized Google account differs from the registered Drive root account")
            if state is None:
                ids = self.drive.generate_ids(1)
                if len(ids) != 1:
                    raise DriveError("Drive did not preallocate the root folder ID")
                state = {"format_version": STATE_VERSION, "root_id": ids[0],
                         "account_permission_id": account, "phase": "reserved",
                         "my_drive_parent_id": None}
                self._write(state)
            if state["phase"] == "reserved":
                found = self.drive.get_file(state["root_id"])
                if found is None:
                    try:
                        self.drive.create_root_folder(state["root_id"], ROOT_NAME)
                    except Exception:
                        if self.drive.get_file(state["root_id"]) is None:
                            raise
                # This binds the app-created folder's observed sole parent. It does
                # not require inspecting Google's built-in My Drive root item.
                parent = self._verify_observed(state, account)
                state = {"format_version": STATE_VERSION, "root_id": state["root_id"],
                         "account_permission_id": account, "phase": "created",
                         "my_drive_parent_id": parent}
                self._write(state)
            return self._verify(state, account)

    def registration(self) -> tuple[str, str]:
        """Return the registered root/account after identity checks, without Drive file access."""
        account = self._account()
        with self._locked():
            state = self._read()
            if state is None:
                raise DriveError("Google Drive root is not configured; run `rg2watch drive-root`")
            if state["phase"] != "created":
                raise DriveError("Google Drive root setup is incomplete; run `rg2watch drive-root`")
            if state["account_permission_id"] != account:
                raise DriveError("the authorized Google account differs from the registered Drive root account")
            return state["root_id"], account

    def verify_registration(self, root_id: str, account: str) -> str:
        """Fail closed if a previously checked registration or its Drive folder changed."""
        with self._locked():
            state = self._read()
            if (state is None or state["phase"] != "created" or state["root_id"] != root_id
                    or state["account_permission_id"] != account):
                raise DriveError("Google Drive root registration changed during publication")
            return self._verify(state, account)

    def require_state(self) -> tuple[str, str]:
        """Validate the configured root and return ``(root_id, account_id)``; never creates it."""
        root_id, account = self.registration()
        return self.verify_registration(root_id, account), account

    def require(self) -> str:
        return self.require_state()[0]
