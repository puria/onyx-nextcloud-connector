"""Minimal Nextcloud WebDAV client.

Only WebDAV is used (`PROPFIND` with `Depth: 1`, `GET`); Nextcloud's internal
storage is never accessed. Credentials are HTTP basic auth with an app password.
"""

from __future__ import annotations

import os
import time
import urllib.parse
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx
from onyx.utils.logger import setup_logger

logger = setup_logger()

DAV_NS = "DAV:"
OC_NS = "http://owncloud.org/ns"

PROPFIND_BODY = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="{DAV_NS}" xmlns:oc="{OC_NS}">
  <d:prop>
    <d:getlastmodified/>
    <d:getetag/>
    <d:getcontentlength/>
    <d:resourcetype/>
    <oc:fileid/>
  </d:prop>
</d:propfind>"""


class NextcloudError(Exception):
    """WebDAV failure for a file or folder."""


class NextcloudAuthError(NextcloudError):
    """Credentials rejected by Nextcloud (HTTP 401/403)."""


@dataclass(frozen=True)
class NextcloudFile:
    file_id: str
    path: str  # relative to the WebDAV user root, e.g. "Documents/report.pdf"
    etag: str
    size: int
    modified_at: float  # seconds since epoch, UTC

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def folder(self) -> str:
        return self.path.rsplit("/", 1)[0] if "/" in self.path else ""


@dataclass(frozen=True)
class DownloadedFile:
    path: str
    etag: str
    content: bytes


def quote_path(path: str) -> str:
    return "/".join(urllib.parse.quote(part) for part in path.split("/") if part)


def normalize_etag(value: str) -> str:
    """Return the opaque ETag, treating HTTP weak/strong forms equivalently.

    Nextcloud may return a strong quoted ETag in PROPFIND and the same value
    prefixed by ``W/`` in GET. The weak marker and quotes are representation,
    not a file-content change.
    """
    tag = value.strip()
    if tag.startswith("W/"):
        tag = tag[2:].strip()
    if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
        tag = tag[1:-1]
    return tag


def parse_http_date(value: str) -> float:
    """HTTP-date to POSIX timestamp; 0.0 when unparseable."""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return 0.0
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC).timestamp()
    return parsed.timestamp()


class NextcloudWebDAVClient:
    SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}

    def __init__(
        self,
        server_url: str,
        username: str,
        app_password: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._server_url = server_url.rstrip("/")
        self._dav_root = f"{self._server_url}/remote.php/dav/files/{username}"
        self._client = httpx.Client(
            auth=(username, app_password),
            verify=verify_ssl,
            timeout=timeout,
            follow_redirects=True,
        )
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> NextcloudWebDAVClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def server_url(self) -> str:
        return self._server_url

    def webdav_url(self, path: str) -> str:
        return f"{self._dav_root}/{quote_path(path)}"

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Request with bounded retries on transient failures (5xx/429/network)."""
        last: httpx.Response | Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
                if response.status_code in (401, 403):
                    raise NextcloudAuthError(
                        "Nextcloud rejected the credentials "
                        f"(HTTP {response.status_code}); check username and app password"
                    )
                if response.status_code < 500 and response.status_code != 429:
                    return response
                last = response
            except httpx.HTTPError as exc:
                last = exc
            if attempt < self._max_retries:
                time.sleep(2**attempt)
        if isinstance(last, httpx.Response):
            return last
        raise NextcloudError(f"{method} request failed: {last}")

    def list_folder(self, folder: str) -> tuple[list[NextcloudFile], list[str]]:
        """One `PROPFIND` with `Depth: 1`: returns (files, subfolder paths)."""
        response = self._request(
            "PROPFIND",
            self.webdav_url(folder),
            content=PROPFIND_BODY,
            headers={"Depth": "1"},
        )
        if response.status_code not in (207, 200):
            raise NextcloudError(
                f"PROPFIND of folder '{folder}' failed with HTTP {response.status_code}"
            )
        return parse_propfind(response.text, folder, self._dav_root)

    def iter_files(self, folders: Iterable[str]) -> Iterator[NextcloudFile]:
        """Recursively yield supported files, folder by folder.

        Streaming (rather than collecting everything first) keeps indexing
        progress visible on large accounts: Onyx receives the first batch as
        soon as the first folder has been listed.
        """
        folders_listed = 0
        for root in folders:
            queue = [root.strip("/")]
            visited: set[str] = set()
            while queue:
                folder = queue.pop(0)
                if folder in visited:
                    continue
                visited.add(folder)
                files, subfolders = self.list_folder(folder)
                folders_listed += 1
                if folders_listed % 25 == 0:
                    logger.info(
                        "Nextcloud scan progress: %d folders listed, currently in '%s'",
                        folders_listed,
                        folder or "/",
                    )
                queue.extend(subfolders)
                for candidate in files:
                    extension = os.path.splitext(candidate.path)[1].lower()
                    if extension in self.SUPPORTED_EXTENSIONS:
                        yield candidate
                    else:
                        logger.debug("Skipping unsupported file type: %s", candidate.path)

    def download(self, file: NextcloudFile, max_bytes: int) -> DownloadedFile:
        """Download a file, enforcing a size cap.

        Returns the bytes and the ETag the server actually served. The caller
        compares that ETag with the one seen during the scan: a mismatch means
        the file changed while being downloaded.
        """
        response = self._request("GET", self.webdav_url(file.path))
        if response.status_code != 200:
            raise NextcloudError(
                f"Download of '{file.path}' failed with HTTP {response.status_code}"
            )
        served_etag = normalize_etag(response.headers.get("ETag") or "")
        content = response.content
        if len(content) > max_bytes:
            raise NextcloudError(
                f"'{file.path}' is {len(content)} bytes, above the {max_bytes} byte limit"
            )
        return DownloadedFile(path=file.path, etag=served_etag, content=content)


def parse_propfind(
    xml_text: str, folder: str, dav_root: str
) -> tuple[list[NextcloudFile], list[str]]:
    """Parse a multistatus response into files and immediate subfolder paths."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise NextcloudError(f"Invalid PROPFIND response for '{folder}': {exc}") from exc

    files: list[NextcloudFile] = []
    subfolders: list[str] = []
    root_path = urllib.parse.urlsplit(dav_root).path.rstrip("/")

    for response in root.findall(f"{{{DAV_NS}}}response"):
        href = response.findtext(f"{{{DAV_NS}}}href") or ""
        properties = response.find(f"{{{DAV_NS}}}propstat/{{{DAV_NS}}}prop")
        if properties is None:
            continue
        relative = urllib.parse.unquote(urllib.parse.urlsplit(href).path)
        if root_path and relative.startswith(root_path + "/"):
            relative = relative[len(root_path) + 1 :]
        relative = relative.strip("/")

        resource_type = properties.find(f"{{{DAV_NS}}}resourcetype")
        is_collection = (
            resource_type is not None and resource_type.find(f"{{{DAV_NS}}}collection") is not None
        )
        if is_collection:
            if relative and relative != folder.strip("/"):
                subfolders.append(relative)
            continue

        file_id = properties.findtext(f"{{{OC_NS}}}fileid")
        etag = normalize_etag(properties.findtext(f"{{{DAV_NS}}}getetag") or "")
        if not file_id or not etag:
            logger.debug("Skipping entry without file id or ETag: %s", relative)
            continue
        size_text = properties.findtext(f"{{{DAV_NS}}}getcontentlength") or "0"
        modified = properties.findtext(f"{{{DAV_NS}}}getlastmodified") or ""
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        files.append(
            NextcloudFile(
                file_id=file_id,
                path=relative,
                etag=etag,
                size=size,
                modified_at=parse_http_date(modified),
            )
        )
    return files, subfolders
