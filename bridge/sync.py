"""Sync engine: scan -> download -> extract -> upsert -> reconcile deletions."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import SUPPORTED_EXTENSIONS, Config
from .extract import extract_text
from .nextcloud import NextcloudClient, NextcloudError, ScannedFile
from .onyx import OnyxClient
from .state import SyncState

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_ATTEMPTS = 3


@dataclass
class RunReport:
    scanned: int = 0
    unchanged: int = 0
    ingested: int = 0
    would_ingest: int = 0
    deleted: int = 0
    would_delete: int = 0
    skipped: int = 0
    failed: int = 0
    scan_errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.scan_errors and self.failed == 0


def doc_id_for(nc_url: str, nc_fileid: str) -> str:
    instance = hashlib.sha1(nc_url.rstrip("/").encode()).hexdigest()[:8]
    return f"nc-{instance}-{nc_fileid}"


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def split_path(path: str) -> tuple[str, str]:
    parts = path.rsplit("/", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", path)


def build_payload(config: Config, scanned: ScannedFile, doc_id: str, text: str) -> dict:
    folder, _ = split_path(scanned.path)
    webdav_url = f"{config.nc_dav_root}/" + "/".join(
        part for part in scanned.path.split("/") if part
    )
    return {
        "document": {
            "id": doc_id,
            "semantic_identifier": split_path(scanned.path)[1],
            "title": split_path(scanned.path)[1],
            "sections": [{"text": text, "link": webdav_url}],
            "source": "file",
            "from_ingestion_api": True,
            "doc_updated_at": scanned.modified_utc,
            "metadata": {
                "nc_fileid": scanned.nc_fileid,
                "nc_path": scanned.path,
                "nc_folder": folder,
                "nc_instance": config.nc_url,
                "nc_etag": scanned.etag,
                "nc_modified": scanned.modified_utc,
                "nc_link": webdav_url,
                "nc_file_type": split_path(scanned.path)[1].rsplit(".", 1)[-1],
                "nc_public_visibility": "yes" if config.onyx_doc_public else "no",
            },
            "external_access": {
                "external_user_emails": [],
                "external_user_group_ids": [],
                "is_public": config.onyx_doc_public,
            },
        }
    }


class RunLock:
    """Prevents overlapping runs across processes (daemon + manual)."""

    def __init__(self, state_path: Path) -> None:
        self._lock_path = state_path.with_suffix(state_path.suffix + ".lock")

    def __enter__(self) -> RunLock:
        self._fh = open(self._lock_path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            raise RuntimeError("another sync run is already active") from None
        return self

    def __exit__(self, *exc: object) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def run_sync(
    config: Config,
    state: SyncState,
    nc: NextcloudClient,
    onyx: OnyxClient,
    *,
    dry_run: bool = False,
) -> RunReport:
    report = RunReport()

    scan = nc.scan(list(config.nc_folders))
    report.scanned = len(scan.files)
    report.scan_errors = scan.errors
    logger.info(
        "Scan: %d file(s) in %d folder(s), %d error(s)",
        len(scan.files),
        scan.folders_visited,
        len(scan.errors),
    )
    for error in scan.errors:
        logger.error("Scan error: %s", error)
    if not scan.complete:
        logger.error("Scan incomplete: aborting run; nothing ingested or deleted")
        return report

    desired_fileids = {f.nc_fileid for f in scan.files}

    for scanned in scan.files:
        suffix = scanned.path.rsplit(".", 1)[-1].lower() if "." in scanned.path else ""
        if f".{suffix}" not in SUPPORTED_EXTENSIONS:
            logger.info("Skip unsupported format: %s", scanned.path)
            report.skipped += 1
            continue

        row = state.get(scanned.nc_fileid)
        same_version = row is not None and row.etag == scanned.etag
        same_location = row is not None and row.path == scanned.path

        if same_version and same_location:
            if row is not None and row.status == "synced":
                logger.info("Unchanged: %s", scanned.path)
                report.unchanged += 1
                continue
            if row is not None and row.status == "skipped":
                logger.info("Already skipped (%s): %s", row.note, scanned.path)
                report.skipped += 1
                continue

        if scanned.size > config.max_file_bytes:
            logger.warning(
                "Skip oversized (%d bytes > limit %d): %s",
                scanned.size,
                config.max_file_bytes,
                scanned.path,
            )
            report.skipped += 1
            if not dry_run:
                state.upsert(
                    nc_fileid=scanned.nc_fileid,
                    path=scanned.path,
                    etag=scanned.etag,
                    size=scanned.size,
                    modified_utc=scanned.modified_utc,
                    doc_id=doc_id_for(config.nc_url, scanned.nc_fileid),
                    status="skipped",
                    synced_utc=None,
                    note=f"oversized ({scanned.size} bytes)",
                )
            continue

        if dry_run:
            action = "add" if row is None else "update"
            logger.info("Would %s: %s (etag %s)", action, scanned.path, scanned.etag[:12])
            report.would_ingest += 1
            continue

        success = _ingest_one(config, state, nc, onyx, scanned, report)
        if not success:
            report.failed += 1

    # Reconcile deletions only after a complete scan AND a run without failures.
    if not scan.complete or report.failed > 0:
        logger.info(
            "Skipping deletion reconciliation (%s)",
            "incomplete scan" if not scan.complete else "ingestion failures present",
        )
        return report

    orphans = [row for row in state.all_rows() if row.nc_fileid not in desired_fileids]
    for row in orphans:
        if dry_run:
            logger.info("Would delete Onyx document %s (%s)", row.doc_id, row.path)
            report.would_delete += 1
            continue
        try:
            onyx.delete(row.doc_id)
        except Exception as exc:
            logger.error("Delete failed for %s (%s): %s", row.path, row.doc_id, exc)
            report.failed += 1
            continue
        logger.info("Deleted from Onyx: %s (%s)", row.path, row.doc_id)
        state.forget(row.nc_fileid)
        report.deleted += 1

    if not dry_run and not report.failed:
        state.set_meta("last_full_sync_utc", now_utc())
    return report


def _ingest_one(
    config: Config,
    state: SyncState,
    nc: NextcloudClient,
    onyx: OnyxClient,
    scanned: ScannedFile,
    report: RunReport,
) -> bool:
    doc_id = doc_id_for(config.nc_url, scanned.nc_fileid)
    temp_path: str | None = None
    try:
        # Download with mid-flight change detection: the served ETag must still
        # match the scanned ETag, otherwise the file changed while downloading.
        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            temp_path, served_etag = nc.download(scanned, config.max_file_bytes)
            if not served_etag or served_etag == scanned.etag:
                break
            logger.warning(
                "File changed during download (attempt %d/%d): %s",
                attempt,
                MAX_DOWNLOAD_ATTEMPTS,
                scanned.path,
            )
            if attempt == MAX_DOWNLOAD_ATTEMPTS:
                logger.error("Giving up on %s for this run; will retry next run", scanned.path)
                return False
            os.unlink(temp_path)
            temp_path = None

        suffix = "." + scanned.path.rsplit(".", 1)[-1].lower()
        assert temp_path is not None
        result = extract_text(Path(temp_path), suffix)
        if result.text is None:
            note = result.note or "no extractable text"
            logger.warning("Skipping %s: %s", scanned.path, note)
            state.upsert(
                nc_fileid=scanned.nc_fileid,
                path=scanned.path,
                etag=scanned.etag,
                size=scanned.size,
                modified_utc=scanned.modified_utc,
                doc_id=doc_id,
                status="skipped",
                synced_utc=None,
                note=note,
            )
            report.notes.append(f"{scanned.path}: {note}")
            report.skipped += 1
            return True

        payload = build_payload(config, scanned, doc_id, result.text)
        try:
            onyx.ingest(payload)
        except Exception as exc:
            logger.error("Ingestion failed for %s: %s", scanned.path, exc)
            return False
        state.upsert(
            nc_fileid=scanned.nc_fileid,
            path=scanned.path,
            etag=scanned.etag,
            size=scanned.size,
            modified_utc=scanned.modified_utc,
            doc_id=doc_id,
            status="synced",
            synced_utc=now_utc(),
        )
        logger.info("Ingested: %s -> %s", scanned.path, doc_id)
        report.ingested += 1
        return True
    except NextcloudError as exc:
        logger.error("Nextcloud error for %s: %s", scanned.path, exc)
        return False
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
