"""Drive holds the receipts themselves, in a folder tree a human can navigate
without this app:

    HSA_Vault/
      2026/2026-03-14__CVS_Pharmacy__42.18__a1b2c3d4.jpg
      _exports/HSA_Audit_Packet_2026.pdf
      _archive/          <- soft-deleted receipts, never hard-deleted
"""

import io
import mimetypes

from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from .util import audit, retry

FOLDER_MIME = "application/vnd.google-apps.folder"
EXPORTS = "_exports"
ARCHIVE = "_archive"


class DriveClient:
    def __init__(self, root_folder_id: str, credentials):
        self.root = root_folder_id
        self._api = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self._folder_cache: dict[str, str] = {}

    # -- folders ------------------------------------------------------------

    @retry()
    def _query(self, q: str, fields: str = "files(id,name,mimeType,parents,webViewLink)") -> list[dict]:
        out, token = [], None
        while True:
            resp = (
                self._api.files()
                .list(
                    q=q,
                    fields=f"nextPageToken,{fields}",
                    pageToken=token,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageSize=200,
                )
                .execute()
            )
            out.extend(resp.get("files", []))
            token = resp.get("nextPageToken")
            if not token:
                return out

    @retry()
    def _create_folder(self, name: str, parent: str) -> str:
        created = (
            self._api.files()
            .create(
                body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent]},
                fields="id",
                supportsAllDrives=True,
            )
            .execute()
        )
        audit("drive.folder_created", name=name, parent=parent, id=created["id"])
        return created["id"]

    def folder(self, name: str, parent: str | None = None) -> str:
        """Get-or-create a subfolder. Cached per session."""
        parent = parent or self.root
        key = f"{parent}/{name}"
        if key in self._folder_cache:
            return self._folder_cache[key]
        q = (
            f"'{parent}' in parents and name = '{name}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        found = self._query(q)
        folder_id = found[0]["id"] if found else self._create_folder(name, parent)
        self._folder_cache[key] = folder_id
        return folder_id

    def year_folder(self, year: int) -> str:
        return self.folder(str(year))

    def exports_folder(self) -> str:
        return self.folder(EXPORTS)

    def archive_folder(self) -> str:
        return self.folder(ARCHIVE)

    # -- files --------------------------------------------------------------

    @retry()
    def upload(self, data: bytes, filename: str, folder_id: str, mime: str | None = None) -> dict:
        """Uploads the ORIGINAL bytes, untouched. Downscaling only ever happens
        on the copy sent to the model."""
        mime = mime or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        created = (
            self._api.files()
            .create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media,
                fields="id,name,webViewLink",
                supportsAllDrives=True,
            )
            .execute()
        )
        audit("drive.upload", name=filename, id=created["id"], bytes=len(data))
        return created

    @retry()
    def download(self, file_id: str) -> bytes:
        request = self._api.files().get_media(fileId=file_id, supportsAllDrives=True)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buffer.getvalue()

    @retry()
    def move(self, file_id: str, new_parent: str) -> None:
        meta = self._api.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
        self._api.files().update(
            fileId=file_id,
            addParents=new_parent,
            removeParents=",".join(meta.get("parents", [])),
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
        audit("drive.move", id=file_id, to=new_parent)

    @retry()
    def rename(self, file_id: str, name: str) -> dict:
        updated = (
            self._api.files()
            .update(fileId=file_id, body={"name": name}, fields="id,name,webViewLink", supportsAllDrives=True)
            .execute()
        )
        audit("drive.rename", id=file_id, name=name)
        return updated

    @retry()
    def metadata(self, file_id: str) -> dict:
        return (
            self._api.files()
            .get(fileId=file_id, fields="id,name,mimeType,webViewLink,parents", supportsAllDrives=True)
            .execute()
        )

    def list_folder(self, folder_id: str) -> list[dict]:
        return self._query(
            f"'{folder_id}' in parents and mimeType != '{FOLDER_MIME}' and trashed = false"
        )

    def list_all_receipt_files(self) -> list[dict]:
        """Everything under the year folders — i.e. what should be in the Sheet."""
        folders = self._query(
            f"'{self.root}' in parents and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        files = []
        for folder in folders:
            if folder["name"].startswith("_"):
                continue
            for f in self.list_folder(folder["id"]):
                f["folder"] = folder["name"]
                files.append(f)
        return files
