"""Onyx ingestion API client.

Only documented endpoints are used:
- POST   /onyx-api/ingestion            (upsert; IngestionDocument payload)
- DELETE /onyx-api/ingestion/{doc_id}   (delete)
Auth: `Authorization: Bearer <API key>`.
"""

from __future__ import annotations

import time
from typing import Any

import httpx


class OnyxError(Exception):
    """Ingestion API failure; the affected document stays retryable."""


class OnyxAuthError(OnyxError):
    """Invalid API key (HTTP 401/403); fatal for the run."""


class OnyxClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        verify_tls: bool = True,
        timeout: float = 60.0,
        max_retries: int = 3,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}"},
            verify=verify_tls,
            timeout=timeout,
        )
        self._max_retries = max_retries

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        last: httpx.Response | Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)  # type: ignore[arg-type]
                if response.status_code in (401, 403):
                    raise OnyxAuthError(f"Onyx rejected the API key (HTTP {response.status_code})")
                if response.status_code < 500 and response.status_code != 429:
                    return response
                last = response
            except httpx.HTTPError as exc:
                last = exc
            if attempt < self._max_retries:
                time.sleep(2**attempt)
        if isinstance(last, httpx.Response):
            return last
        raise OnyxError(f"{method} request failed: {last}")

    def ingest(self, payload: dict[str, Any]) -> None:
        """Upsert one document. Raises OnyxError on failure; no state update happens."""
        response = self._request("POST", f"{self._base}/onyx-api/ingestion", json=payload)
        if response.status_code >= 300:
            raise OnyxError(
                f"Onyx ingestion failed with HTTP {response.status_code}: "
                f"{_truncate(response.text)}"
            )

    def delete(self, document_id: str) -> None:
        """Delete one bridge-owned document by ID."""
        response = self._request(
            "DELETE", f"{self._base}/onyx-api/ingestion/{urllib_quote(document_id)}"
        )
        if response.status_code >= 300:
            raise OnyxError(
                f"Onyx delete of {document_id} failed with HTTP {response.status_code}: "
                f"{_truncate(response.text)}"
            )


def urllib_quote(value: str) -> str:
    import urllib.parse

    return urllib.parse.quote(value, safe="")


def _truncate(text: str, limit: int = 300) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"
