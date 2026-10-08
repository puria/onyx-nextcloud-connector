# Nextcloud → Onyx connector

This repository ships **two independent ways** to index Nextcloud into [Onyx](https://onyx.app):

1. **Native Onyx connector** (`onyx-patch/`) — patches Onyx v4.9.0 so a `Nextcloud`
   connector appears under _Admin Panel → Connectors_ with its own configuration form,
   credentials UI and indexing pipeline. The running instance on this machine already
   uses it. See `onyx-patch/README.md` for build, deploy, rollback and upgrade notes.
2. **Standalone bridge** (below) — an external sync process using Onyx's ingestion API.
   Does not modify Onyx; useful when you cannot rebuild the Onyx images.

Both use WebDAV only (`PROPFIND`/`GET`; Nextcloud internal storage is never touched),
keep stable document IDs across renames and moves, skip scanned PDFs without extractable
text, and never log credentials or document contents.

## Onyx API surface used (verified)

Verified against the official API reference generated from Onyx's OpenAPI schema
(`https://docs.onyx.app/developers/api_reference/ingestion/*`):

| Purpose            | Endpoint                                             | Notes                                                                       |
| ------------------ | ---------------------------------------------------- | --------------------------------------------------------------------------- |
| Upsert document    | `POST {ONYX_URL}/onyx-api/ingestion`                 | `Authorization: Bearer <key>`; body `{document: DocumentBase, cc_pair_id?}` |
| Delete document    | `DELETE {ONYX_URL}/onyx-api/ingestion/{document_id}` | **Deletion is supported**                                                   |
| List ingested docs | `GET {ONYX_URL}/onyx-api/ingestion`                  | not used; bridge tracks ownership in SQLite                                 |

Fields used on `DocumentBase`: `id`, `semantic_identifier`, `title`, `sections[].text`,
`sections[].link`, `source`, `doc_updated_at`, `from_ingestion_api`, `metadata`
(string / list-of-strings), and `external_access` (`external_user_emails`,
`external_user_group_ids`, `is_public`).

Because Onyx exposes a delete endpoint, full reconciliation is implemented — there is no
"unable to delete" limitation to work around.

### Visibility is explicit, not inherited

The bridge pushes documents with `external_access.is_public` taken from `ONYX_DOC_PUBLIC`.
**Nextcloud sharing/permissions are not synchronized.** The intended deployment is a
private, single-person Onyx instance, where `ONYX_DOC_PUBLIC=0` restricts the documents
to permitted users only.

### Required Onyx permission

The API key must hold `manage:connectors` (or `admin`). Create it under
_Admin Panel → API Keys_.

## Configuration

Copy `.env.example` to `.env` and fill it in. Credentials are never logged and `.env`
is git-ignored.

| Variable                | Meaning                                                 |
| ----------------------- | ------------------------------------------------------- |
| `NC_URL`                | Nextcloud base URL, e.g. `https://cloud.example.com`    |
| `NC_USERNAME`           | Nextcloud username                                      |
| `NC_APP_PASSWORD`       | App password (Settings → Security → Devices & sessions) |
| `NC_FOLDERS`            | Comma-separated allowlist, relative to the WebDAV root  |
| `NC_VERIFY_TLS`         | Verify Nextcloud TLS (default `1`)                      |
| `ONYX_URL`              | Onyx API base, e.g. `https://onyx.example.com/api`      |
| `ONYX_API_KEY`          | Onyx API key                                            |
| `ONYX_DOC_PUBLIC`       | `1` to mark documents public (default `0`)              |
| `ONYX_VERIFY_TLS`       | Verify Onyx TLS (default `1`)                           |
| `SYNC_INTERVAL_SECONDS` | Daemon interval, default `600`                          |
| `MAX_FILE_MB`           | Largest file to download/ingest, default `25`           |
| `STATE_PATH`            | SQLite path, default `/data/state.db`                   |
| `LOG_LEVEL`             | Log level, default `INFO`                               |

## Usage

### Native (uv)

```sh
uv sync
export $(grep -v '^#' .env | xargs)   # or use direnv / your shell
uv run onyx-nextcloud-connector sync --dry-run   # first dry run
uv run onyx-nextcloud-connector sync --once      # first real sync
uv run onyx-nextcloud-connector sync --daemon    # continuous
```

### Docker Compose

```sh
cp .env.example .env   # edit it
docker compose run --rm bridge sync --dry-run
docker compose run --rm bridge sync --once
docker compose up -d   # daemon, SQLite persisted on the bridge-data volume
```

### Connecting to a local Onyx (already running on `localhost:3000`)

Run the bridge **on the host** (uv): set `ONYX_URL=http://localhost:3000/api`.

Run the bridge **in a container**, which cannot see the host's `localhost` — attach it to
Onyx's Compose network and use the nginx service name:

```yaml
services:
  bridge:
    build: .
    env_file: .env
    environment:
      STATE_PATH: /data/state.db
    volumes:
      - bridge-data:/data
    restart: unless-stopped
    command: ["sync", "--daemon"]
    networks: [onyx]

networks:
  onyx:
    external: true
    name: onyx_default # <compose project>_default
```

Then set `ONYX_URL=http://nginx/api` (nginx proxies `/api` to the Onyx API server).
Alternatively keep `ONYX_URL=http://host.docker.internal:3000/api` and add
`extra_hosts: ["host.docker.internal:host-gateway"]`.

The API key must be able to manage connectors: create a **service account** in
_Admin Panel → Service Accounts_ and assign it the **Admin** group (or, on Enterprise,
a group with _Manage Connectors & Document Sets_). Verify with:

```sh
curl -H "Authorization: Bearer $ONYX_API_KEY" http://localhost:3000/api/me/permissions
```

### Modes

- `sync --once` — one full pass (default).
- `sync --dry-run` — scans and reports planned adds/updates/deletes; downloads nothing,
  writes no state, contacts Onyx not at all.
- `sync --daemon [--interval N]` — repeated passes; `--interval` overrides
  `SYNC_INTERVAL_SECONDS`.

Overlapping runs are prevented by a file lock next to the SQLite database; a second
concurrent run exits without doing anything.

## How syncing works

1. **Scan** — each configured folder is walked with repeated `PROPFIND` requests using
   `Depth: 1`. Files are collected with `oc:fileid`, ETag, size and last-modified.
2. **Diff** — ETag + size + path are compared against SQLite. Unchanged files are skipped
   without downloading.
3. **Download** — files stream over WebDAV with the size cap enforced. If the ETag served
   during download differs from the scanned ETag (file changed mid-download), it retries
   up to 3 times.
4. **Extract** — `.txt`, `.md` (UTF-8, latin-1 fallback), `.docx`, `.pdf`. Unsupported
   formats are skipped; PDFs without extractable text (scans) are reported and skipped.
5. **Upsert** — document ID is `nc-<instance-hash>-<fileid>`, stable across renames and
   moves (Nextcloud keeps `oc:fileid`). The row is only marked `synced` after Onyx returns
   success; failures stay `pending` and are retried next run.
6. **Reconcile** — only after a _complete_ scan **and** a run with no failures, documents
   tracked in SQLite that no longer appear in scope are deleted from Onyx and forgotten.
   Any scan error, auth error or ingestion failure skips reconciliation entirely.

## Troubleshooting

| Symptom                                                        | Likely cause                                                                                     |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `configuration error: Missing required environment variable …` | `.env` not loaded                                                                                |
| `Nextcloud rejected credentials (HTTP 401)`                    | wrong app password or username                                                                   |
| `PROPFIND of folder '…' failed with HTTP 404`                  | folder not in `NC_FOLDERS` or not shared with the user                                           |
| Documents never appear                                         | Onyx key lacks `manage:connectors`, or `ONYX_URL` lacks the `/api` suffix                        |
| `skipped … scanned PDF without extractable text`               | document is an image-only scan (expected)                                                        |
| Nothing deleted from Onyx                                      | a scan error or ingestion failure occurred; check the log for `Skipping deletion reconciliation` |
| `another sync run is already active`                           | a previous run still holds the lock file                                                         |

Deletions are intentionally conservative: fix the reported scan/ingestion error and the
next successful run will reconcile.

## Development

```sh
mise run test          # pytest
mise run lint          # design lint + ruff
mise run format        # ruff + prettier + taplo
```

## Verified vs unverified

**Tested (mocked Nextcloud WebDAV + mocked Onyx API, `mise run test`):** initial ingestion,
unchanged-file skipping, updates, renames without duplicates, deletion after a complete
scan, retryable ingestion failures, no deletions on failed/incomplete scans, auth errors
aborting safely, mid-download change retry, scanned-PDF reporting, oversize/unsupported
skipping, dry-run purity, PROPFIND parsing, config validation.

**End-to-end smoke (mocked HTTP servers, real `httpx` transport and CLI):** a fake
Nextcloud WebDAV server and fake Onyx ingestion API were driven with the real
`onyx-nextcloud-connector` binary to confirm: dry run writes nothing, the first sync
ingests both supported files, a second sync leaves unchanged files untouched, a rename
re-ingests under the same stable document ID (no duplicate), deleting a file causes exactly
one `DELETE /onyx-api/ingestion/{id}`, and a second concurrent run is rejected by the lock.

**Not yet verified against live servers:** a real end-to-end run requires Nextcloud and
Onyx credentials, which were not available. Set them in `.env` and start with
`sync --dry-run`; the request shapes above come from Onyx's published OpenAPI schema.
