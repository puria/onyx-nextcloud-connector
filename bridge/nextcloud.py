"""Nextcloud access over WebDAV only.

All requests go to `{NC_URL}/remote.php/dav/files/{username}/...` with HTTP
basic auth (username + app password). Nextcloud internal storage is never touched.
"""

from __future__ import annotations

import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

DAV = "DAV:"
OC = "http://owncloud.org/ns"

PROPFIND_BODY = f"""<?xml version="1.0" encoding="UTF-8"?>
<d:propfind xmlns:d="{DAV}" xmlns:oc="{OC}">
  <d:prop>
    <d:getlastmodified/>
    <d:getetag/>
    <d:getcontenttype/>
    <d:resourcetype/>
    <d:getcontentlength/>
    <oc:fileid/>
  </d:prop>
</d:propfind>"""


class NextcloudError(Exception):
    """Base error for Nextcloud/WebDAV failures."""


class NextcloudAuthError(NextcloudError):
    """Invalid or missing credentials (HTTP 401/403)."""


@dataclass(frozen=True)
class ScannedFile:
    nc_fileid: str
    path: str  # relative to the WebDAV user root, e.g. "Documents/a.pdf"
    etag: str
    size: int
    modified_utc: str


@dataclass
class ScanResult:
    files: list[ScannedFile]
    folders_visited: int
    errors: list[str]

    @property
    def complete(self) -> bool:
        return not self.errors


def quote_path(path: str) -> str:
    return "/".join(urllib.parse.quote(part) for part in path.split("/"))


def normalize_etag(value: str) -> str:
    """Normalize strong and weak HTTP ETag representations to the opaque tag."""
    tag = value.strip()
    if tag.startswith("W/"):
        tag = tag[2:].strip()
    if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
        tag = tag[1:-1]
    return tag


def _http_date_to_utc(value: str) -> str:
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class NextcloudClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        app_password: str,
        *,
        verify_tls: bool = True,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._dav_root = f"{base_url.rstrip('/')}/remote.php/dav/files/{username}"
        self._client = httpx.Client(
            auth=(username, app_password),
            verify=verify_tls,
            timeout=timeout,
            follow_redirects=True,
        )
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Request with bounded retries on transient failures (5xx/429/network)."""
        last: httpx.Response | Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
                if response.status_code in (401, 403):
                    raise NextcloudAuthError(
                        f"Nextcloud rejected credentials (HTTP {response.status_code})"
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

    def propfind(self, folder: str) -> list[ScannedFile | str]:
        """PROPFIND with Depth: 1. Returns files, and subfolder paths for recursion."""
        url = f"{self._dav_root}/{quote_path(folder)}"
        response = self._request("PROPFIND", url, content=PROPFIND_BODY, headers={"Depth": "1"})
        if response.status_code not in (207, 200):
            raise NextcloudError(
                f"PROPFIND of folder {folder!r} failed with HTTP {response.status_code}"
            )
        return parse_propfind(response.text, folder, self._dav_root)

    def scan(self, folders: list[str]) -> ScanResult:
        """Recursively scan each configured folder using repeated Depth: 1 PROPFINDs.

        Never raises for a broken folder; collects errors so the caller can abort
        the run without triggering reconciling deletions.
        """
        files: list[ScannedFile] = []
        errors: list[str] = []
        folders_visited = 0
        for root in folders:
            queue = [root]
            while queue:
                current = queue.pop(0)
                folders_visited += 1
                try:
                    entries = self.propfind(current)
                except (NextcloudAuthError, NextcloudError, httpx.HTTPError) as exc:
                    errors.append(f"{current}: {exc}")
                    continue
                for entry in entries:
                    if isinstance(entry, str):
                        queue.append(entry)
                    else:
                        files.append(entry)
        return ScanResult(
            files=sorted(files, key=lambda f: f.path),
            folders_visited=folders_visited,
            errors=errors,
        )

    def download(self, scanned: ScannedFile, max_bytes: int) -> tuple[str, str]:
        """Stream the file to a temp path, enforcing the size cap.

        Returns (temp_path, response_etag). The caller verifies that the ETag the
        server served matches `scanned.etag`; a mismatch means the file changed
        while being downloaded and must be retried.
        """
        url = f"{self._dav_root}/{quote_path(scanned.path)}"
        response = self._request("GET", url)
        if response.status_code != 200:
            raise NextcloudError(
                f"Download of {scanned.path!r} failed with HTTP {response.status_code}"
            )
        response_etag = normalize_etag(response.headers.get("ETag") or "")
        temp = tempfile.NamedTemporaryFile(prefix="nc-onyx-", suffix=".download", delete=False)
        total = 0
        try:
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise NextcloudError(
                        f"Download of {scanned.path!r} exceeded {max_bytes} bytes; aborting"
                    )
                temp.write(chunk)
        except (httpx.HTTPError, OSError) as exc:
            temp.close()
            _safe_unlink(temp.name)
            raise NextcloudError(f"Download of {scanned.path!r} failed mid-stream: {exc}") from exc
        except NextcloudError:
            temp.close()
            _safe_unlink(temp.name)
            raise
        temp.close()
        return temp.name, response_etag

    @property
    def dav_root(self) -> str:
        return self._dav_root


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def parse_propfind(xml_text: str, folder: str, dav_root: str) -> list[ScannedFile | str]:
    """Parse a multistatus response into ScannedFile entries and subfolder names."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise NextcloudError(f"Invalid PROPFIND XML for folder {folder!r}: {exc}") from exc
    results: list[ScannedFile | str] = []
    for response in root.findall(f"{{{DAV}}}response"):
        href = response.findtext(f"{{{DAV}}}href") or ""
        props = response.find(f"{{{DAV}}}propstat/{{{DAV}}}prop")
        if props is None:
            continue
        resourcetype = props.find(f"{{{DAV}}}resourcetype")
        is_dir = resourcetype is not None and resourcetype.find(f"{{{DAV}}}collection") is not None
        rel = strip_dav_root(urllib.parse.unquote(href), dav_root)
        if is_dir:
            if rel and rel.rstrip("/") != folder.rstrip("/"):
                results.append(rel.rstrip("/"))
            continue
        fileid = props.findtext(f"{{{OC}}}fileid")
        etag = normalize_etag(props.findtext(f"{{{DAV}}}getetag") or "")
        if not fileid or not etag:
            continue
        size_text = props.findtext(f"{{{DAV}}}getcontentlength") or "0"
        modified_raw = props.findtext(f"{{{DAV}}}getlastmodified") or ""
        results.append(
            ScannedFile(
                nc_fileid=fileid,
                path=rel,
                etag=etag,
                size=int(size_text),
                modified_utc=_http_date_to_utc(modified_raw),
            )
        )
    return results


def strip_dav_root(href: str, dav_root: str) -> str:
    from urllib.parse import urlsplit

    path = urlsplit(href).path
    root_path = urlsplit(dav_root).path.rstrip("/")
    if root_path and path.startswith(root_path + "/"):
        return path[len(root_path) + 1 :]
    return path.lstrip("/")
