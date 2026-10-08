"""Shared fixtures: a fake Nextcloud WebDAV server and a fake Onyx API."""

from __future__ import annotations

import hashlib
import json
import urllib.parse

import httpx
import pytest

from bridge.config import Config
from bridge.nextcloud import NextcloudClient
from bridge.onyx import OnyxClient
from bridge.state import SyncState

NC_PREFIX = "/remote.php/dav/files/me"


def _unquote(value: str) -> str:
    return urllib.parse.unquote(value)


class FakeNextcloud:
    """In-memory WebDAV tree.

    files: path -> {"content": bytes, "etag": str, "fileid": str}
    """

    def __init__(self) -> None:
        self.files: dict[str, dict] = {}
        self.broken_folders: set[str] = set()
        self.auth_failure = False
        self.etag_override: dict[str, str] = {}  # path -> etag served on GET
        self.requests: list[tuple[str, str]] = []

    def add(self, path: str, content: bytes, etag: str | None = None, fileid: str | None = None):
        self.files[path] = {
            "content": content,
            "etag": etag or hashlib.md5(content).hexdigest(),
            "fileid": fileid or str(len(self.files) + 1),
        }
        return self.files[path]

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.method, str(request.url)))
            if self.auth_failure:
                return httpx.Response(401, text="unauthorized")
            rel = urllib.parse.unquote(request.url.path.split(NC_PREFIX, 1)[-1]).strip("/")

            if request.method == "PROPFIND":
                if rel in self.broken_folders:
                    return httpx.Response(404, text="not found")
                direct_files = [p for p in self.files if _parent(p) == rel]
                subfolders: set[str] = set()
                for path in self.files:
                    if not rel:
                        rest = path
                    elif path.startswith(rel + "/"):
                        rest = path[len(rel) + 1 :]
                    else:
                        continue
                    segment, _, remainder = rest.partition("/")
                    if remainder:
                        subfolders.add(segment if not rel else f"{rel}/{segment}")
                return httpx.Response(207, text=self._multistatus(direct_files, subfolders))

            if request.method == "GET":
                if rel not in self.files:
                    return httpx.Response(404, text="gone")
                entry = self.files[rel]
                etag = self.etag_override.get(rel, entry["etag"])
                return httpx.Response(
                    200,
                    content=entry["content"],
                    headers={"ETag": f'W/"{etag}"'},
                )
            return httpx.Response(405)

        return httpx.MockTransport(handler)

    def _multistatus(self, files: list[str], folders: set[str]) -> str:
        parts = [
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        ]
        for folder in sorted(folders):
            parts.append(
                f"<d:response><d:href>{NC_PREFIX}/{folder}/</d:href>"
                "<d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>"
                "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            )
        for path in sorted(files):
            entry = self.files[path]
            parts.append(
                f"<d:response><d:href>{NC_PREFIX}/{path}</d:href><d:propstat><d:prop>"
                "<d:getlastmodified>Mon, 01 Jan 2024 10:00:00 GMT</d:getlastmodified>"
                f'<d:getetag>"{entry["etag"]}"</d:getetag>'
                f"<d:getcontentlength>{len(entry['content'])}</d:getcontentlength>"
                "<d:resourcetype/>"
                f"<oc:fileid>{entry['fileid']}</oc:fileid>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
            )
        parts.append("</d:multistatus>")
        return "".join(parts)


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


class FakeOnyx:
    def __init__(self) -> None:
        self.ingested: list[dict] = []
        self.deleted: list[str] = []
        self.fail_next = 0
        self.auth_failure = False

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if self.auth_failure:
                return httpx.Response(401, text="unauthorized")
            if request.method == "POST" and request.url.path.endswith("/onyx-api/ingestion"):
                if self.fail_next > 0:
                    self.fail_next -= 1
                    return httpx.Response(500, text="boom")
                self.ingested.append(json.loads(request.content))
                return httpx.Response(200, json={"document_id": "x", "already_existed": False})
            if request.method == "DELETE" and "/onyx-api/ingestion/" in request.url.path:
                self.deleted.append(_unquote(request.url.path.rsplit("/", 1)[-1]))
                return httpx.Response(200, json={})
            return httpx.Response(404)

        return httpx.MockTransport(handler)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        nc_url="https://cloud.test",
        nc_username="me",
        nc_app_password="secret",
        nc_folders=("Documents",),
        nc_verify_tls=True,
        onyx_url="https://onyx.test/api",
        onyx_api_key="key",
        onyx_doc_public=False,
        onyx_verify_tls=True,
        sync_interval_seconds=600,
        max_file_bytes=10 * 1024 * 1024,
        state_path=tmp_path / "state.db",
        log_level="INFO",
    )


@pytest.fixture
def fake_nc() -> FakeNextcloud:
    return FakeNextcloud()


@pytest.fixture
def fake_onyx() -> FakeOnyx:
    return FakeOnyx()


def make_nc(fake: FakeNextcloud) -> NextcloudClient:
    client = NextcloudClient("https://cloud.test", "me", "secret", max_retries=1)
    client._client.close()
    client._client = httpx.Client(transport=fake.transport(), auth=("me", "secret"))
    return client


def make_onyx(fake: FakeOnyx) -> OnyxClient:
    client = OnyxClient("https://onyx.test/api", "key", max_retries=1)
    client._client.close()
    client._client = httpx.Client(transport=fake.transport())
    return client


@pytest.fixture
def state(tmp_path) -> SyncState:
    sync_state = SyncState(tmp_path / "state.db")
    yield sync_state
    sync_state.close()
