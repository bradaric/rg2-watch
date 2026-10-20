"""Crash-safe creation of one public Google Drive folder per reviewed document URL."""
import hashlib
import io
import json
from pathlib import Path

from .case_handoff import CaseError, CaseHandoff


FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


class DriveError(RuntimeError):
    pass


def authorize_drive(client_secrets: Path, token_path: Path) -> None:
    """Explicit interactive installed-app authorization used only by ``drive-auth``."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), [DRIVE_SCOPE])
    credentials = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json(), encoding="utf-8")
    token_path.chmod(0o600)


def configured_drive(token_path: Path):
    """Load a previously authorized Drive service.  Never starts an OAuth browser flow."""
    if not token_path.is_file():
        raise DriveError("Google Drive is not authorized; run `rg2watch drive-auth`")
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        credentials = Credentials.from_authorized_user_file(str(token_path), [DRIVE_SCOPE])
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            token_path.write_text(credentials.to_json(), encoding="utf-8")
            token_path.chmod(0o600)
        if not credentials.valid:
            raise DriveError("Google Drive authorization is invalid; run `rg2watch drive-auth`")
        return GoogleDriveAdapter(build("drive", "v3", credentials=credentials,
                                        cache_discovery=False))
    except DriveError:
        raise
    except Exception as exc:
        raise DriveError("Google Drive authorization could not be loaded; run `rg2watch drive-auth`") from exc


class GoogleDriveAdapter:
    """Small wrapper around a configured Drive v3 service; it never performs OAuth."""
    def __init__(self, service):
        self.service = service

    def generate_ids(self, count: int) -> list[str]:
        try:
            data = self.service.files().generateIds(
                count=count, space="drive", type="files").execute()
        except Exception as exc:
            raise DriveError("Drive file ID preallocation did not complete; retry safely") from exc
        ids = data.get("ids", [])
        if len(ids) != count:
            raise DriveError("Drive did not preallocate the requested file IDs")
        return ids

    def account_permission_id(self) -> str:
        try:
            value = self.service.about().get(fields="user(permissionId)").execute()
        except Exception as exc:
            raise DriveError("Google Drive account identity could not be verified") from exc
        permission_id = (value.get("user") or {}).get("permissionId")
        if not permission_id:
            raise DriveError("Google Drive did not provide an authorized account identity")
        return permission_id

    def get_my_drive_root_id(self) -> str:
        root = self.get_file("root")
        if root is None or not root.get("id"):
            raise DriveError("Google Drive My Drive root could not be inspected")
        return root["id"]

    def get_file(self, file_id: str):
        try:
            return self.service.files().get(
                fileId=file_id,
                fields="id,name,mimeType,parents,owners(permissionId),md5Checksum,trashed").execute()
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise DriveError("Drive file metadata could not be inspected") from exc

    def create_folder(self, file_id: str, name: str, parent: str):
        try:
            self.service.files().create(body={"id": file_id, "name": name, "mimeType": FOLDER_MIME,
                                              "parents": [parent]},
                                        fields="id").execute()
        except Exception as exc:
            raise DriveError("Drive folder creation did not complete; retry to reconcile it") from exc

    def upload(self, file_id: str, name: str, mime: str, parent: str, data: bytes):
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        try:
            self.service.files().create(
                body={"id": file_id, "name": name, "mimeType": mime, "parents": [parent]},
                media_body=media, fields="id,md5Checksum").execute()
        except Exception as exc:
            raise DriveError("Drive file upload did not complete; retry to reconcile it") from exc

    def list_children(self, folder_id: str) -> list[dict]:
        try:
            children, page_token = [], None
            while True:
                value = self.service.files().list(
                    q=f"'{folder_id}' in parents and trashed = false", spaces="drive",
                    fields="nextPageToken,files(id,name,mimeType,parents,trashed)", pageSize=1000,
                    **({"pageToken": page_token} if page_token else {})).execute()
                children.extend(value.get("files", []))
                page_token = value.get("nextPageToken")
                if not page_token:
                    return children
        except Exception as exc:
            raise DriveError("Drive case folder contents could not be inspected") from exc

    def _permissions(self, file_id: str) -> list[dict]:
        try:
            return self.service.permissions().list(
                fileId=file_id, fields="permissions(id,type,role)").execute().get(
                    "permissions", [])
        except Exception as exc:
            raise DriveError("Drive permissions could not be inspected") from exc

    def folder_is_public(self, folder_id: str) -> bool:
        return any(p.get("type") == "anyone" for p in self._permissions(folder_id))

    def share_folder(self, folder_id: str):
        try:
            self.service.permissions().create(fileId=folder_id,
                                              body={"type": "anyone", "role": "reader"},
                                              fields="id").execute()
        except Exception as exc:
            raise DriveError("Drive public permission did not complete; retry to reconcile it") from exc

    def children_have_public_reader(self, child_ids: list[str]) -> bool:
        for child in child_ids:
            permissions = self._permissions(child)
            if not any(
                p.get("type") == "anyone" and p.get("role") == "reader"
                for p in permissions
            ):
                return False
        return True


class LazyConfiguredDrive:
    """Delay credential refresh/client construction until an explicit Publish worker runs."""
    def __init__(self, token_path: Path):
        self.token_path = token_path
        self._drive = None

    def __getattr__(self, name):
        if self._drive is None:
            self._drive = configured_drive(self.token_path)
        return getattr(self._drive, name)


class DrivePublisher:
    def __init__(self, cases: CaseHandoff, drive, root):
        self.cases, self.drive, self.root, self.store = cases, drive, root, cases.store

    def publish(self, url: str, summary: str) -> dict:
        completed = self.store.get_publication(url)
        if completed and completed["phase"] == "published":
            return self._result(completed)
        lock = self.store.case_locks.publish(url, blocking=False)
        if lock is None:
            raise DriveError("publication is already in progress")
        with lock:
            completed = self.store.get_publication(url)
            if completed and completed["phase"] == "published":
                return self._result(completed)
            root_id, account_id = self.root.registration()
            publication = self.cases.claim(url, summary)
            case = json.loads(publication["case_json"])
            folder_name = f"RG2 предмет — {case['municipality']} БМ {case['station']}"
            bound = publication.get("drive_account_id")
            legacy_pending = bound is None and bool(publication["folder_id"])
            if bound is not None and bound != account_id:
                raise DriveError("the pending publication belongs to a different Google Drive account")
            if bound is None and not publication["folder_id"]:
                self.store.bind_publication_account(url, account_id)
                publication = dict(self.store.get_publication(url))
            self.root.verify_registration(root_id, account_id)
            if bound is None and publication["folder_id"]:
                legacy = self.drive.get_file(publication["folder_id"])
                owners = (legacy or {}).get("owners") or []
                if (not legacy or legacy.get("id") != publication["folder_id"]
                        or legacy.get("name") != folder_name
                        or legacy.get("mimeType") != FOLDER_MIME or legacy.get("trashed")
                        or len(owners) != 1 or owners[0].get("permissionId") != account_id):
                    raise DriveError("legacy pending Drive folder ownership could not be established; "
                                     "investigate it manually")
                self.store.bind_publication_account(url, account_id)
                publication = dict(self.store.get_publication(url))
            if not publication["folder_id"]:
                ids = self.drive.generate_ids(3)
                self.store.save_drive_ids(url, *ids)
                publication = dict(self.store.get_publication(url))
            pdf_name = "документ.pdf"
            text_name = "опис-предмета.txt"
            staged = Path(publication["staged_pdf_path"])
            try:
                pdf = staged.read_bytes()
            except OSError as exc:
                raise CaseError("frozen staged PDF is missing or changed") from exc
            if hashlib.sha256(pdf).hexdigest() != publication["pdf_sha256"]:
                raise CaseError("frozen staged PDF is missing or changed")
            case_text = self._case_text(case, publication["summary"], publication["pdf_sha256"])
            expected = (
                (publication["folder_id"], folder_name, FOLDER_MIME, [root_id], None),
                (publication["pdf_file_id"], pdf_name, "application/pdf",
                 [publication["folder_id"]], pdf),
                (publication["text_file_id"], text_name, "text/plain",
                 [publication["folder_id"]], case_text),
            )
            for file_id, name, mime, parents, data in expected:
                found = self.drive.get_file(file_id)
                if found is None:
                    try:
                        if mime == FOLDER_MIME:
                            self.drive.create_folder(file_id, name, root_id)
                        else:
                            self.drive.upload(file_id, name, mime, parents[0], data)
                    except Exception:
                        # An uncertain response or 409 may mean this exact preallocated ID exists.
                        found = self.drive.get_file(file_id)
                        if found is None:
                            raise
                    found = self.drive.get_file(file_id)
                self._verify(found, file_id, name, mime, parents, data,
                             legacy_parent_recovery=legacy_pending and mime == FOLDER_MIME)
                if mime == FOLDER_MIME:
                    owners = found.get("owners") or []
                    if len(owners) != 1 or owners[0].get("permissionId") != account_id:
                        raise DriveError("Drive case folder owner does not match the authorized account")
                    public = self.drive.folder_is_public(file_id)
                    if public and publication["phase"] != "uploaded":
                        raise DriveError("the pending Drive case folder became public before verification")
                    visible = {child.get("id") for child in self.drive.list_children(file_id)}
                    allowed = {publication["pdf_file_id"], publication["text_file_id"]}
                    if not visible <= allowed:
                        raise DriveError("the Drive case folder contains an unexpected app-visible child; "
                                         "inspect it manually")
            self.store.advance_publication(url, "uploaded")
            with self.store.case_locks.decision(url):
                checked_root, checked_account = self.root.require_state()
                if checked_root != root_id or checked_account != account_id:
                    raise DriveError("Google Drive root identity changed during publication")
                already_public = self.drive.folder_is_public(publication["folder_id"])
                if not already_public:
                    if self.store.latest_verdict(url) != "escalate":
                        raise DriveError("latest verdict must still be escalate before public sharing")
                    try:
                        self.drive.share_folder(publication["folder_id"])
                    except Exception:
                        if not self.drive.folder_is_public(publication["folder_id"]):
                            raise
                if not self.drive.folder_is_public(publication["folder_id"]):
                    raise DriveError("Drive did not confirm anyone-with-link reader access")
                children = [publication["pdf_file_id"], publication["text_file_id"]]
                if not self.drive.children_have_public_reader(children):
                    raise DriveError("Drive account policy did not expose both folder children")
                final = "https://drive.google.com/drive/folders/" + publication["folder_id"]
                viber = self.cases.viber_text(case, publication["summary"], final)
                self.store.finalize_publication(url, final, viber)
            staged.unlink(missing_ok=True)
            return self._result(self.store.get_publication(url))

    @staticmethod
    def _case_text(case: dict, summary: str, pdf_sha: str) -> bytes:
        text = (f"Избори: {case['election']}\nОпштина: {case['municipality']}\n"
                f"Бирачко место: {case['station']}\nВерзија документа: {case['url']}\n"
                f"РИК: {case['rik_url']}\nSHA-256 PDF датотеке: {pdf_sha}\n\n"
                f"Опис који је одобрио оператор:\n{summary}\n")
        return text.encode("utf-8")

    @staticmethod
    def _verify(found, file_id, name, mime, parents, data, legacy_parent_recovery=False):
        if not found or found.get("id") != file_id or found.get("name") != name or \
                found.get("mimeType") != mime or found.get("parents", []) != parents or found.get("trashed"):
            if mime == FOLDER_MIME and found and found.get("parents") != parents:
                if legacy_parent_recovery:
                    raise DriveError("inspect the existing case folder for any files, confirm it is "
                                     "empty, then move that same folder manually into the private "
                                     "RG2 Watch folder and retry")
                raise DriveError("the Drive case folder has an unexpected parent; investigate it "
                                 "manually without moving or replacing it")
            raise DriveError("remote file metadata does not match the frozen case")
        if data is not None:
            expected = hashlib.md5(data).hexdigest()
            if found.get("md5Checksum") != expected:
                raise DriveError("remote file checksum does not match the frozen case")

    @staticmethod
    def _result(row) -> dict:
        return {"status": row["phase"], "url": row["final_url"],
                "viber_text": row["viber_text"], "document_url": row["url"]}
