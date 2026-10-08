"""Behavioural tests for the sync engine against mocked Nextcloud + Onyx."""

from __future__ import annotations

import logging

import pytest

from bridge.sync import doc_id_for, run_sync
from tests.conftest import make_nc, make_onyx


def _run(config, state, fake_nc, fake_onyx, **kwargs):
    nc = make_nc(fake_nc)
    onyx = make_onyx(fake_onyx)
    try:
        return run_sync(config, state, nc, onyx, **kwargs)
    finally:
        nc.close()
        onyx.close()


def test_scans_and_ingests_supported_file(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/note.md", b"# Hello\nworld\n")
    report = _run(config, state, fake_nc, fake_onyx)

    assert report.scanned == 1 and report.ingested == 1 and report.failed == 0
    assert len(fake_onyx.ingested) == 1
    doc = fake_onyx.ingested[0]["document"]
    assert doc["semantic_identifier"] == "note.md"
    assert doc["sections"][0]["text"].startswith("# Hello")
    assert doc["metadata"]["nc_path"] == "Documents/note.md"
    assert doc["metadata"]["nc_folder"] == "Documents"
    assert doc["external_access"] == {
        "external_user_emails": [],
        "external_user_group_ids": [],
        "is_public": False,
    }
    row = state.get(fake_nc.files["Documents/note.md"]["fileid"])
    assert row.status == "synced" and row.synced_utc


def test_unchanged_file_skipped_on_second_run(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/note.md", b"stable")
    _run(config, state, fake_nc, fake_onyx)
    fake_nc.requests.clear()
    fake_onyx.ingested.clear()

    report = _run(config, state, fake_nc, fake_onyx)

    assert report.unchanged == 1 and report.ingested == 0
    assert fake_onyx.ingested == []
    assert not [r for r in fake_nc.requests if r[0] == "GET"], (
        "unchanged file must not be downloaded"
    )


def test_updated_file_is_reingested(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/note.txt", b"first", etag="v1", fileid="42")
    _run(config, state, fake_nc, fake_onyx)
    fake_nc.add("Documents/note.txt", b"second version", etag="v2", fileid="42")
    fake_onyx.ingested.clear()

    report = _run(config, state, fake_nc, fake_onyx)

    assert report.ingested == 1
    assert fake_onyx.ingested[0]["document"]["sections"][0]["text"] == "second version"
    assert state.get("42").etag == "v2"


def test_rename_updates_metadata_without_duplicate(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/old.md", b"content", etag="e1", fileid="7")
    _run(config, state, fake_nc, fake_onyx)
    # Move: same fileid, new path (Nextcloud keeps the file ID across renames).
    fake_nc.files["Documents/new.md"] = fake_nc.files.pop("Documents/old.md")
    fake_onyx.ingested.clear()

    report = _run(config, state, fake_nc, fake_onyx)

    assert report.ingested == 1 and report.deleted == 0
    assert len(fake_onyx.ingested) == 1
    doc = fake_onyx.ingested[0]["document"]
    assert doc["id"] == doc_id_for(config.nc_url, "7")
    assert doc["metadata"]["nc_path"] == "Documents/new.md"
    assert state.get("7").path == "Documents/new.md"


def test_deleted_file_removed_after_complete_scan(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/gone.md", b"bye", fileid="9")
    _run(config, state, fake_nc, fake_onyx)
    del fake_nc.files["Documents/gone.md"]

    report = _run(config, state, fake_nc, fake_onyx)

    assert report.deleted == 1
    assert fake_onyx.deleted == [doc_id_for(config.nc_url, "9")]
    assert state.get("9") is None


def test_failed_ingestion_is_retryable_and_blocks_deletes(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/keep.md", b"keep", fileid="1")
    _run(config, state, fake_nc, fake_onyx)
    # A new file that will fail to ingest, plus a removed file that must NOT be deleted.
    fake_nc.add("Documents/new.md", b"new", fileid="2")
    del fake_nc.files["Documents/keep.md"]
    fake_onyx.fail_next = 1

    report = _run(config, state, fake_nc, fake_onyx)

    assert report.failed == 1
    assert fake_onyx.deleted == [], "failed run must not delete"
    assert state.get("2") is None, "failed version must stay unsynced"
    assert state.get("1") is not None

    report2 = _run(config, state, fake_nc, fake_onyx)
    assert report2.ingested == 1 and report2.deleted == 1
    assert state.get("2").status == "synced"
    assert state.get("1") is None


def test_incomplete_scan_never_deletes_or_ingests(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/a.md", b"a", fileid="1")
    _run(config, state, fake_nc, fake_onyx)
    fake_onyx.ingested.clear()
    del fake_nc.files["Documents/a.md"]
    fake_nc.add("Documents/b.md", b"b", fileid="2")
    fake_nc.broken_folders.add("Documents")

    report = _run(config, state, fake_nc, fake_onyx)

    assert report.scan_errors and not report.ok
    assert fake_onyx.ingested == [] and fake_onyx.deleted == []
    assert state.get("1") is not None


def test_auth_error_aborts_without_deletions(config, state, fake_nc, fake_onyx, caplog):
    fake_nc.add("Documents/a.md", b"a", fileid="1")
    _run(config, state, fake_nc, fake_onyx)
    del fake_nc.files["Documents/a.md"]
    fake_nc.auth_failure = True

    with caplog.at_level(logging.ERROR):
        report = _run(config, state, fake_nc, fake_onyx)

    assert not report.ok
    assert fake_onyx.deleted == []
    assert any("401" in e for e in report.scan_errors)


def test_change_during_download_is_retried(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/live.txt", b"v1", etag="e1", fileid="5")
    # First GET serves a different ETag (file changed mid-download), then e1.
    fake_nc.etag_override["Documents/live.txt"] = "e9"

    nc = make_nc(fake_nc)
    onyx = make_onyx(fake_onyx)
    original_download = nc.download
    calls = {"n": 0}

    def flaky_download(scanned, max_bytes):
        calls["n"] += 1
        if calls["n"] == 1:
            fake_nc.etag_override["Documents/live.txt"] = "e9"
        else:
            fake_nc.etag_override.pop("Documents/live.txt", None)
        return original_download(scanned, max_bytes)

    nc.download = flaky_download  # type: ignore[method-assign]
    report = run_sync(config, state, nc, onyx)
    nc.close()
    onyx.close()

    assert calls["n"] == 2, "downloaded twice after detecting mid-flight change"
    assert report.ingested == 1


def test_scanned_pdf_without_text_is_reported(config, state, fake_nc, fake_onyx, caplog):
    import io

    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)
    fake_nc.add("Documents/scan.pdf", buffer.getvalue(), fileid="11")

    with caplog.at_level(logging.WARNING):
        report = _run(config, state, fake_nc, fake_onyx)

    assert report.skipped == 1 and fake_onyx.ingested == []
    row = state.get("11")
    assert row.status == "skipped" and "scanned PDF" in row.note
    assert any("scanned PDF" in r.message for r in caplog.records)


def test_unsupported_and_oversized_files_skipped(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/photo.png", b"\x89PNG...", fileid="20")
    fake_nc.add("Documents/big.txt", b"x" * 2048, fileid="21")
    small = config.__class__(**{**config.__dict__, "max_file_bytes": 1024})
    report = _run(small, state, fake_nc, fake_onyx)

    assert report.skipped == 2 and fake_onyx.ingested == []
    assert state.get("21").status == "skipped"
    assert state.get("20") is None


def test_dry_run_makes_no_changes(config, state, fake_nc, fake_onyx):
    fake_nc.add("Documents/a.md", b"a", fileid="1")
    fake_nc.add("Documents/b.md", b"b", fileid="2")
    report = _run(config, state, fake_nc, fake_onyx, dry_run=True)

    assert report.would_ingest == 2
    assert fake_onyx.ingested == []
    assert state.get("1") is None and state.get("2") is None


def test_doc_id_is_stable_and_instance_scoped():
    assert doc_id_for("https://cloud.test", "7") == doc_id_for("https://cloud.test/", "7")
    assert doc_id_for("https://cloud.test", "7") != doc_id_for("https://other.test", "7")


@pytest.mark.parametrize("path", ["Documents", "Documents/Sub"])
def test_recursive_depth_one_scan(config, state, fake_nc, fake_onyx, path):
    fake_nc.add("Documents/Sub/deep.md", b"deep", fileid="31")
    config2 = config.__class__(**{**config.__dict__, "nc_folders": (path,)})
    report = _run(config2, state, fake_nc, fake_onyx)
    assert report.scanned == 1 and report.ingested == 1
