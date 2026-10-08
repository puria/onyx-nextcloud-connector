"""Config validation and WebDAV PROPFIND parsing."""

from __future__ import annotations

import pytest

from bridge.config import Config, ConfigError
from bridge.nextcloud import NextcloudError, parse_propfind

BASE_ENV = {
    "NC_URL": "https://cloud.test",
    "NC_USERNAME": "me",
    "NC_APP_PASSWORD": "pw",
    "NC_FOLDERS": "Documents, Projects/Notes ",
    "ONYX_URL": "https://onyx.test/api",
    "ONYX_API_KEY": "key",
}


def _env(monkeypatch, **overrides):
    for key, value in {**BASE_ENV, **overrides}.items():
        monkeypatch.setenv(key, value)


def test_config_defaults(monkeypatch):
    _env(monkeypatch)
    config = Config.from_env()
    assert config.nc_folders == ("Documents", "Projects/Notes")
    assert config.sync_interval_seconds == 600
    assert config.max_file_bytes == 25 * 1024 * 1024
    assert config.onyx_doc_public is False
    assert config.nc_verify_tls is True
    assert config.nc_dav_root == "https://cloud.test/remote.php/dav/files/me"


def test_config_requires_folders(monkeypatch):
    _env(monkeypatch, NC_FOLDERS="")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_rejects_endpoint_url(monkeypatch):
    _env(monkeypatch, ONYX_URL="https://onyx.test/api/onyx-api/ingestion")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_config_interval_and_size_override(monkeypatch):
    _env(monkeypatch, SYNC_INTERVAL_SECONDS="120", MAX_FILE_MB="5", ONYX_DOC_PUBLIC="1")
    config = Config.from_env()
    assert config.sync_interval_seconds == 120
    assert config.max_file_bytes == 5 * 1024 * 1024
    assert config.onyx_doc_public is True


def test_config_rejects_bad_integer(monkeypatch):
    _env(monkeypatch, MAX_FILE_MB="lots")
    with pytest.raises(ConfigError):
        Config.from_env()


PROPFIND_XML = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/me/Documents/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/me/Documents/Sub/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/me/Documents/My%20File.md</d:href>
    <d:propstat><d:prop>
      <d:getlastmodified>Tue, 02 Jan 2024 12:00:00 GMT</d:getlastmodified>
      <d:getetag>"abc123"</d:getetag>
      <d:getcontentlength>1234</d:getcontentlength>
      <d:resourcetype/>
      <oc:fileid>4242</oc:fileid>
    </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/me/Documents/nofileid.txt</d:href>
    <d:propstat><d:prop><d:getetag>"x"</d:getetag><d:resourcetype/></d:prop>
      <d:status>HTTP/1.1 200 OK</d:status></d:propstat>
  </d:response>
</d:multistatus>"""


def test_parse_propfind_extracts_files_and_folders():
    dav_root = "https://cloud.test/remote.php/dav/files/me"
    entries = parse_propfind(PROPFIND_XML, "Documents", dav_root)
    folders = [e for e in entries if isinstance(e, str)]
    files = [e for e in entries if not isinstance(e, str)]

    assert folders == ["Documents/Sub"]
    assert len(files) == 1  # entry without oc:fileid is ignored
    scanned = files[0]
    assert scanned.path == "Documents/My File.md"
    assert scanned.nc_fileid == "4242"
    assert scanned.etag == "abc123"
    assert scanned.size == 1234
    assert scanned.modified_utc == "2024-01-02T12:00:00Z"


def test_parse_propfind_rejects_invalid_xml():
    with pytest.raises(NextcloudError):
        parse_propfind("<not-xml", "Documents", "https://cloud.test/remote.php/dav/files/me")
