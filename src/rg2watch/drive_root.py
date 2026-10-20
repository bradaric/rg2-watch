"""Durable, account-bound coordination for the app's private Drive root."""
import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .drive_publish import DriveError, FOLDER_MIME


ROOT_NAME = "RG2 Watch"
STATE_VERSION = 1


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
        expected = {"format_version", "root_id", "account_permission_id", "phase"}
        if (not isinstance(value, dict) or set(value) != expected
                or value.get("format_version") != STATE_VERSION
                or not isinstance(value.get("root_id"), str) or not value["root_id"]
                or not isinstance(value.get("account_permission_id"), str)
                or not value["account_permission_id"]
                or value.get("phase") not in ("reserved", "created")):
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

    def _verify(self, state: dict, account: str) -> str:
        if state["account_permission_id"] != account:
            raise DriveError("the authorized Google account differs from the registered Drive root account")
        root = self.drive.get_file(state["root_id"])
        if root is None:
            raise DriveError("the registered Google Drive root is missing or inaccessible; investigate manually")
        my_drive = self.drive.get_my_drive_root_id()
        owners = root.get("owners") or []
        valid = (root.get("id") == state["root_id"] and root.get("name") == ROOT_NAME
                 and root.get("mimeType") == FOLDER_MIME and root.get("parents") == [my_drive]
                 and not root.get("trashed") and len(owners) == 1
                 and owners[0].get("permissionId") == account)
        if not valid:
            raise DriveError("the registered Google Drive root metadata or ownership has changed")
        if self.drive.folder_is_public(state["root_id"]):
            raise DriveError("the RG2 Watch root must remain private; remove public access before retrying")
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
                         "account_permission_id": account, "phase": "reserved"}
                self._write(state)
            if state["phase"] == "reserved":
                found = self.drive.get_file(state["root_id"])
                if found is None:
                    try:
                        self.drive.create_folder(state["root_id"], ROOT_NAME,
                                                 self.drive.get_my_drive_root_id())
                    except Exception:
                        if self.drive.get_file(state["root_id"]) is None:
                            raise
                self._verify(state, account)
                state = {**state, "phase": "created"}
                self._write(state)
            return self._verify(state, account)

    def require_state(self) -> tuple[str, str]:
        """Validate the configured root and return ``(root_id, account_id)``; never creates it."""
        account = self._account()
        with self._locked():
            state = self._read()
            if state is None:
                raise DriveError("Google Drive root is not configured; run `rg2watch drive-root`")
            if state["phase"] != "created":
                raise DriveError("Google Drive root setup is incomplete; run `rg2watch drive-root`")
            return self._verify(state, account), account

    def require(self) -> str:
        return self.require_state()[0]
