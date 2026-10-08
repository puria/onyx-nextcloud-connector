"""Validate the patched Nextcloud connector inside the real Onyx environment.

Runs in an Onyx backend container (cwd /app) against an in-process fake
Nextcloud WebDAV server (httpx.MockTransport). No network, no credentials.

Usage:
    docker run --rm --network onyx_default \
        --env-file ~/.config/onyx/deployment/.env \
        -v $PWD/onyx-patch/tests/check_connector.py:/tmp/check_connector.py \
        onyx-nextcloud-backend:v4.9.0 python /tmp/check_connector.py
"""

# ruff: noqa: E402 - the connector module is imported mid-file on purpose so the
# fake WebDAV transport can be installed between the import and first use.

import hashlib
import inspect
import io
import sys

import httpx

# --- 1. registry / enum wiring -------------------------------------------------
from onyx.configs.constants import DocumentSource
from onyx.connectors.factory import identify_connector_class
from onyx.connectors.nextcloud.client import NextcloudError, normalize_etag
from onyx.connectors.nextcloud.config import NextcloudConnectorConfig
from onyx.connectors.nextcloud.connector import NextcloudConnector
from onyx.connectors.registry import CONNECTOR_CLASS_MAP

assert normalize_etag('"same"') == normalize_etag('W/"same"') == "same"
# Onyx's extractor consults the key-value store (DB) for the optional
# Unstructured API key, so the SQL engine must be initialized as it is in the
# real indexing worker.
from onyx.db.engine.sql_engine import SqlEngine
from reportlab.pdfgen import canvas

SqlEngine.init_engine(pool_size=2, max_overflow=2)
assert DocumentSource.NEXTCLOUD.value == "nextcloud", DocumentSource.NEXTCLOUD
assert DocumentSource.NEXTCLOUD in CONNECTOR_CLASS_MAP, "not registered"
cls = identify_connector_class(DocumentSource.NEXTCLOUD)
assert cls is NextcloudConnector, cls

# config fields must equal __init__ kwargs exactly (upstream invariant)
config_fields = set(NextcloudConnectorConfig.model_fields)
init_params = set(inspect.signature(NextcloudConnector.__init__).parameters) - {"self"}
assert config_fields == init_params, config_fields ^ init_params
print("wiring OK: enum + registry + config/__init__ signature match")

# --- 2. fake Nextcloud ---------------------------------------------------------
NC_PREFIX = "/remote.php/dav/files/me"
FILES: dict[str, dict] = {}


def add(path, content, etag=None, fileid=None):
    FILES[path] = {
        "content": content,
        "etag": etag or hashlib.md5(content).hexdigest(),
        "fileid": fileid or str(len(FILES) + 1),
    }


def pdf_bytes(text: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, text)
    c.save()
    return buf.getvalue()


def docx_bytes(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


add("Documents/note.md", b"# Notes\nhello from nextcloud", fileid="101")
add("Documents/report.txt", b"plain text report", fileid="102")
add("Documents/word.docx", docx_bytes("docx paragraph text"), fileid="103")
add("Documents/paper.pdf", pdf_bytes("extracted pdf text"), fileid="104")
add("Documents/scan.pdf", pdf_bytes(""), fileid="105")  # blank page -> no text
add("Documents/photo.png", b"\x89PNG\x00", fileid="106")  # unsupported
add("Documents/huge.txt", b"x" * (3 * 1024 * 1024), fileid="107")  # oversize for 1 MiB cap
add("Documents/Sub/deep.md", b"deep nested content", fileid="108")

etag_override: dict[str, str] = {}


def multistatus(folder: str) -> bytes:
    direct = [p for p in FILES if p.rsplit("/", 1)[0] == folder]
    folders = set()
    for path in FILES:
        if folder and not path.startswith(folder + "/"):
            continue
        rest = path if not folder else path[len(folder) + 1 :]
        segment, _, more = rest.partition("/")
        if more:
            folders.add(f"{folder}/{segment}" if folder else segment)
    parts = [
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    ]
    for f in sorted(folders):
        parts.append(
            f"<d:response><d:href>{NC_PREFIX}/{f}/</d:href><d:propstat><d:prop>"
            "<d:resourcetype><d:collection/></d:resourcetype></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    for path in sorted(direct):
        entry = FILES[path]
        parts.append(
            f"<d:response><d:href>{NC_PREFIX}/{path}</d:href><d:propstat><d:prop>"
            "<d:getlastmodified>Mon, 01 Jan 2024 10:00:00 GMT</d:getlastmodified>"
            f'<d:getetag>"{entry["etag"]}"</d:getetag>'
            f"<d:getcontentlength>{len(entry['content'])}</d:getcontentlength>"
            f"<d:resourcetype/><oc:fileid>{entry['fileid']}</oc:fileid>"
            "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
        )
    parts.append("</d:multistatus>")
    return "".join(parts).encode()


def handler(request: httpx.Request) -> httpx.Response:
    rel = request.url.path.split(NC_PREFIX, 1)[-1].strip("/")
    import urllib.parse

    rel = urllib.parse.unquote(rel)
    if request.method == "PROPFIND":
        exists = rel == "" or any(path == rel or path.startswith(rel + "/") for path in FILES)
        if not exists:
            return httpx.Response(404)
        return httpx.Response(207, content=multistatus(rel))
    if request.method == "GET":
        entry = FILES.get(rel)
        if entry is None:
            return httpx.Response(404)
        etag = etag_override.get(rel, entry["etag"])
        return httpx.Response(200, content=entry["content"], headers={"ETag": f'W/"{etag}"'})
    return httpx.Response(405)


import onyx.connectors.nextcloud.client as client_module

real_client = httpx.Client
client_module.httpx.Client = lambda **kwargs: real_client(transport=httpx.MockTransport(handler))

# --- 3. run the connector ------------------------------------------------------
connector = NextcloudConnector(folders=["Documents"], max_file_size_mb=1, batch_size=2)
connector.load_credentials(
    {
        "server_url": "https://cloud.test",
        "username": "me",
        "app_password": "app-password",
    }
)

docs = [doc for batch in connector.load_from_state() for doc in batch]
names = sorted(d.semantic_identifier for d in docs)
print("indexed:", names)
assert names == ["deep.md", "note.md", "paper.pdf", "report.txt", "word.docx"], names

by_name = {d.semantic_identifier: d for d in docs}
note = by_name["note.md"]
assert note.source is DocumentSource.NEXTCLOUD
assert note.id.startswith("nc-") and note.id.endswith("-101"), note.id
assert note.metadata["nc_path"] == "Documents/note.md"
assert note.metadata["nc_folder"] == "Documents"
assert note.metadata["nc_instance"] == "https://cloud.test"
assert "hello from nextcloud" in note.sections[0].text
assert note.doc_updated_at is not None and note.doc_updated_at.tzinfo is not None
assert by_name["paper.pdf"].sections[0].text.strip(), "pdf text not extracted"
assert "docx paragraph text" in by_name["word.docx"].sections[0].text
assert by_name["deep.md"].metadata["nc_path"] == "Documents/Sub/deep.md"
assert by_name["note.md"].id != by_name["report.txt"].id
print("documents OK: ids, metadata, pdf/docx extraction, recursion")

# incremental poll: everything is from 2024, so a later window yields nothing
later = [d for batch in connector.poll_source(1893456000.0, 1893456000.0 + 60) for d in batch]
assert later == [], later
earlier = [d for batch in connector.poll_source(0.0, 1893456000.0) for d in batch]
assert len(earlier) == 5, len(earlier)
print("poll_source OK: incremental window filters by mtime")

# --- 4. change-during-download + auth failure ---------------------------------
etag_override["Documents/note.md"] = "different-etag"
try:
    list(connector.load_from_state())
except Exception as exc:
    print("changed-during-download raises as intended:", type(exc).__name__)
else:
    raise AssertionError("expected failure when the file keeps changing")
etag_override.clear()

client_module.httpx.Client = lambda **kwargs: real_client(
    transport=httpx.MockTransport(lambda request: httpx.Response(401))
)
auth_connector = NextcloudConnector(folders=["Documents"], max_file_size_mb=1)
auth_connector.load_credentials(
    {"server_url": "https://cloud.test", "username": "me", "app_password": "app-password"}
)
try:
    list(auth_connector.load_from_state())
except Exception as exc:
    print("auth failure raises as intended:", type(exc).__name__)
else:
    raise AssertionError("expected auth failure")

# --- 5. validate_connector_settings + empty-folders = whole account ---------
client_module.httpx.Client = lambda **kwargs: real_client(transport=httpx.MockTransport(handler))

settings_connector = NextcloudConnector(folders=["Documents"], max_file_size_mb=1)
settings_connector.load_credentials(
    {"server_url": "https://cloud.test", "username": "me", "app_password": "app-password"}
)
settings_connector.validate_connector_settings()  # must not raise

bad_folder = NextcloudConnector(folders=["DoesNotExist"], max_file_size_mb=1)
bad_folder.load_credentials(
    {"server_url": "https://cloud.test", "username": "me", "app_password": "app-password"}
)
try:
    bad_folder.validate_connector_settings()
except NextcloudError:
    print("validate_connector_settings reports a missing folder as intended")
else:
    raise AssertionError("expected validation failure for a missing folder")

empty_folders = NextcloudConnector(folders=[], max_file_size_mb=1)
empty_folders.load_credentials(
    {"server_url": "https://cloud.test", "username": "me", "app_password": "app-password"}
)
whole_account = [doc for batch in empty_folders.load_from_state() for doc in batch]
names = sorted(d.semantic_identifier for d in whole_account)
assert names == ["deep.md", "note.md", "paper.pdf", "report.txt", "word.docx"], names
print("empty folders OK: indexes the whole account")

print("ALL CONNECTOR CHECKS PASSED")
sys.exit(0)
