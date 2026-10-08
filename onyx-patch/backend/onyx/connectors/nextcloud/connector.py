"""Nextcloud connector for Onyx.

Indexes files from explicitly configured Nextcloud folders over WebDAV only.
Text is extracted with Onyx's own file processing utility, so PDF/DOCX handling
(and any configured OCR) stays consistent with the rest of the product.

Failure policy:
- scan / auth / download errors raise, so the indexing attempt fails instead of
  publishing an incomplete document list;
- files whose content yields no text (e.g. scanned PDFs) are skipped with a
  warning, mirroring the built-in file connector.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO

from onyx.configs.app_configs import INDEX_BATCH_SIZE
from onyx.configs.constants import DocumentSource
from onyx.connectors.interfaces import (
    GenerateDocumentsOutput,
    LoadConnector,
    PollConnector,
    SecondsSinceUnixEpoch,
)
from onyx.connectors.models import (
    ConnectorMissingCredentialError,
    Document,
    HierarchyNode,
    TextSection,
)
from onyx.connectors.nextcloud.client import (
    NextcloudAuthError,
    NextcloudError,
    NextcloudFile,
    NextcloudWebDAVClient,
)
from onyx.file_processing.extract_file_text import extract_text_and_images
from onyx.utils.logger import setup_logger

logger = setup_logger()

MAX_DOWNLOAD_ATTEMPTS = 3


class NextcloudConnector(LoadConnector, PollConnector):
    def __init__(
        self,
        folders: list[str],
        verify_ssl: bool = True,
        max_file_size_mb: int = 25,
        batch_size: int = INDEX_BATCH_SIZE,
    ) -> None:
        self.folders = [folder.strip("/") for folder in folders if folder.strip()]
        self.verify_ssl = verify_ssl
        self.max_file_size_mb = max_file_size_mb
        self.batch_size = batch_size
        self.client: NextcloudWebDAVClient | None = None
        self._instance_key: str | None = None

    # ------------------------------------------------------------------ auth

    def load_credentials(self, credentials: dict) -> dict | None:
        server_url = str(credentials.get("server_url") or "").strip()
        username = str(credentials.get("username") or "").strip()
        app_password = str(credentials.get("app_password") or "")

        if not server_url or not username or not app_password:
            raise ConnectorMissingCredentialError("Nextcloud")

        if self.client is not None:
            self.client.close()

        self.client = NextcloudWebDAVClient(
            server_url=server_url,
            username=username,
            app_password=app_password,
            verify_ssl=self.verify_ssl,
        )
        # Stable prefix so document IDs cannot collide across instances.
        self._instance_key = hashlib.sha1(server_url.rstrip("/").encode()).hexdigest()[:8]
        logger.info(
            "Nextcloud connector configured for %s, folders=%s",
            server_url,
            self.folders,
        )
        return None

    # ------------------------------------------------------------- interface

    def load_from_state(self) -> GenerateDocumentsOutput:
        yield from self._yield_documents(modified_since=None)

    def poll_source(
        self, start: SecondsSinceUnixEpoch, end: SecondsSinceUnixEpoch
    ) -> GenerateDocumentsOutput:
        yield from self._yield_documents(modified_since=start)

    def validate_connector_settings(self) -> None:
        """Verify the server URL, credentials and configured folders are usable.

        Runs as Onyx's INDEXING capability check for this source (the UI shows
        the result as "Connector settings validation").
        """
        client = self._require_client()
        folders = self.folders or [""]
        errors: list[str] = []
        for folder in folders:
            try:
                client.list_folder(folder)
            except NextcloudAuthError:
                raise
            except NextcloudError as exc:
                errors.append(str(exc))
        if errors:
            raise NextcloudError("; ".join(errors))

    # ---------------------------------------------------------------- internals

    def _require_client(self) -> NextcloudWebDAVClient:
        if self.client is None:
            raise ConnectorMissingCredentialError("Nextcloud")
        return self.client

    def _document_id(self, file: NextcloudFile) -> str:
        return f"nc-{self._instance_key}-{file.file_id}"

    def _yield_documents(self, modified_since: float | None) -> GenerateDocumentsOutput:
        client = self._require_client()

        # No folders configured means "scan the whole account": walk from the
        # WebDAV user root downwards.
        roots = self.folders or [""]

        files = client.walk(roots)
        logger.info(
            "Nextcloud scan found %d supported file(s) in %s",
            len(files),
            roots if self.folders else "the whole account",
        )

        max_bytes = self.max_file_size_mb * 1024 * 1024
        batch: list[Document | HierarchyNode] = []

        for file in files:
            if (
                modified_since is not None
                and file.modified_at
                and file.modified_at < modified_since
            ):
                continue
            if file.size > max_bytes:
                logger.warning(
                    "Skipping '%s': %d bytes exceeds the %d MiB limit",
                    file.path,
                    file.size,
                    self.max_file_size_mb,
                )
                continue

            document = self._build_document(client, file, max_bytes)
            if document is None:
                continue

            batch.append(document)
            if len(batch) >= self.batch_size:
                yield batch
                batch = []

        if batch:
            yield batch

    def _build_document(
        self, client: NextcloudWebDAVClient, file: NextcloudFile, max_bytes: int
    ) -> Document | None:
        downloaded = self._download_stable(client, file, max_bytes)

        try:
            extraction = extract_text_and_images(BytesIO(downloaded.content), file.name)
        except Exception as exc:  # Onyx extractors raise a variety of errors
            logger.warning("Could not extract text from '%s': %s", file.path, exc)
            return None

        text = (extraction.text_content or "").strip()
        if not text:
            logger.warning(
                "Skipping '%s': no extractable text (scanned PDF or empty file)",
                file.path,
            )
            return None

        webdav_url = client.webdav_url(file.path)
        updated_at = datetime.fromtimestamp(file.modified_at, tz=UTC) if file.modified_at else None
        metadata: dict[str, str | list[str]] = {
            "nc_fileid": file.file_id,
            "nc_path": file.path,
            "nc_folder": file.folder,
            "nc_instance": client.server_url,
            "nc_etag": downloaded.etag or file.etag,
            "nc_link": webdav_url,
            "nc_file_type": file.name.rsplit(".", 1)[-1].lower() if "." in file.name else "",
        }
        if updated_at is not None:
            metadata["nc_modified"] = updated_at.isoformat()

        return Document(
            id=self._document_id(file),
            sections=[TextSection(text=text, link=webdav_url)],
            source=DocumentSource.NEXTCLOUD,
            semantic_identifier=file.name,
            title=file.name,
            doc_updated_at=updated_at,
            metadata=metadata,
        )

    def _download_stable(self, client: NextcloudWebDAVClient, file: NextcloudFile, max_bytes: int):
        """Download a file, retrying if it changes mid-download.

        The ETag served by the GET must equal the ETag seen during the scan;
        otherwise the bytes are a different version than the one we are about to
        index, so the file is fetched again.
        """
        for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
            downloaded = client.download(file, max_bytes)
            if not downloaded.etag or downloaded.etag == file.etag:
                return downloaded
            logger.warning(
                "File '%s' changed during download (attempt %d/%d)",
                file.path,
                attempt,
                MAX_DOWNLOAD_ATTEMPTS,
            )
        raise NextcloudError(
            f"'{file.path}' kept changing while being downloaded; "
            "the indexing attempt will be retried"
        )


__all__ = ["NextcloudConnector", "NextcloudAuthError", "NextcloudError"]
