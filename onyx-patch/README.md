# Onyx patch: native Nextcloud connector

This directory patches Onyx **v4.9.0** to add a real, native `Nextcloud`
connector: it appears as a card under *Admin Panel → Connectors*, is configured
in the UI (folders, SSL, size limit), stores its credentials in Onyx's
encrypted credential store, and is indexed by Onyx's own pipeline with Onyx's
own text extraction.

The running instance at `localhost:3000` already uses these images.

## What is patched

Backend (overlaid onto the upstream `onyxdotapp/onyx-backend:v4.9.0` image):

| File | Change |
| --- | --- |
| `onyx/configs/constants.py` | `DocumentSource.NEXTCLOUD = "nextcloud"` + description entry |
| `onyx/connectors/registry.py` | `CONNECTOR_CLASS_MAP` entry → `NextcloudConnector` |
| `onyx/connectors/nextcloud/` | new package: `config.py`, `client.py` (WebDAV), `connector.py` |

Web (fresh clone of the pinned upstream tag + `nextcloud-web.patch`, built with
the upstream `web/Dockerfile` steps):

| File | Change |
| --- | --- |
| `src/lib/types.ts` | `ValidSources.Nextcloud = "nextcloud"` |
| `src/lib/sources.ts` | `SOURCE_METADATA_MAP` entry (Storage category) + icon |
| `src/components/icons/icons.tsx` | `NextcloudIcon` component |
| `src/lib/connectors/constants.ts` | `SOURCE_DESCRIPTION_KEYS` entry |
| `src/lib/connectors/connectors.tsx` | `connectorConfigs.nextcloud` form fields |
| `src/lib/connectors/credentials.ts` | credential template + display names |
| `src/lib/connectors/types/credentialJson.ts` | `NextcloudCredentialJson` |
| `src/i18n/messages/*.json` | `sources.nextcloud.description` in all 9 locales |

The web patch is **purely additive** (103 insertions, 0 deletions) so it stays
rebaseable on Onyx upgrades.

Connector behaviour (same guarantees as the standalone bridge):

- WebDAV only (`PROPFIND` `Depth: 1` recursion + `GET`); internal storage untouched.
- Stable document IDs `nc-<instance-hash>-<fileid>`; `oc:fileid` survives renames/moves.
- Text extracted by Onyx's own `extract_text_and_images` (PDF/DOCX/TXT/MD).
- Files that change between scan and download are re-fetched (3 attempts), then the
  indexing attempt fails rather than indexing a mismatched version.
- Files with no extractable text (scanned PDFs) are skipped with a warning.
- Oversized files (`max_file_size_mb`, default 25) are skipped.
- Scan/auth/download errors fail the indexing attempt instead of publishing a
  partial result, so nothing is removed from the index on a broken scan.
- Credentials never logged; document contents never logged.

## Build

```sh
docker build -f onyx-patch/backend.Dockerfile \
  -t onyx-nextcloud-backend:v4.9.0 onyx-patch/backend/
docker build -f onyx-patch/web.Dockerfile \
  --build-arg ONYX_VERSION=v4.9.0-nextcloud \
  -t onyx-nextcloud-web:v4.9.0 onyx-patch/web/
```

## Deploy (this machine)

`~/.config/onyx/deployment/.env` carries:

```env
ONYX_BACKEND_IMAGE=onyx-nextcloud-backend:v4.9.0
ONYX_WEB_SERVER_IMAGE=onyx-nextcloud-web:v4.9.0
```

then `docker compose -d up` in that directory recreates `api_server`,
`background` and `web_server`. `nginx`, the model servers and all data
containers stay untouched.

## Rollback

Comment out the two `ONYX_*_IMAGE` lines in the deployment `.env` and run
`docker compose up -d` — the stock `v4.9.0` images are still present locally.

## Upgrade to a newer Onyx

1. Rebase `backend/onyx/` overlay files on the new tag (the connector package
   itself rarely changes; `constants.py`/`registry.py` may shift).
2. Regenerate `web/nextcloud-web.patch`: clone the new tag, apply this patch
   (resolve conflicts), `git diff > nextcloud-web.patch`.
3. Rebuild both images with the new tag and update the deployment `.env`.

## Test

`tests/check_connector.py` runs inside the backend image against an in-process
fake WebDAV server (no network, no credentials):

```sh
docker run --rm --network onyx_default \
  --env-file ~/.config/onyx/deployment/.env \
  -v $PWD/tests/check_connector.py:/tmp/check_connector.py \
  onyx-nextcloud-backend:v4.9.0 python /tmp/check_connector.py
```

Covers: enum/registry wiring, config/`__init__` signature equality (an upstream
test invariant), document IDs and metadata, WebDAV recursion, PDF/DOCX/TXT/MD
extraction via Onyx's extractor, scanned-PDF/oversized/unsupported skipping,
incremental polling, change-during-download retry/failure, auth-failure abort.
