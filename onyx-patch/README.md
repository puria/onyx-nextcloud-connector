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
| `onyx/server/documents/nextcloud_browse.py` | new `POST /manage/admin/nextcloud/browse` route (folder listing for the UI picker) |
| `onyx/main.py` | imports and registers the browse router (2 lines) |

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
| `src/i18n/messages/*.json` | `sources.nextcloud.description` + `connectorsList.folderPicker.*` in all 9 locales |
| `src/views/.../form/inputs/FolderPickerInput.tsx` | folder browser input (calls the browse route) |
| `src/lib/connectors/types/form.ts`, `utils.ts` | `folder_picker` field type + validation |
| `web/public/Nextcloud.svg` | official Nextcloud logo asset |

The web patch only adds new code plus small edits at existing extension points
(no upstream logic is rewritten), so it stays rebaseable on Onyx upgrades.

## Connector behaviour (same guarantees as the standalone bridge)

- WebDAV only (`PROPFIND` `Depth: 1` recursion + `GET`); internal storage untouched.
- The scan **streams**: batches are handed to Onyx as folders are listed, so
  indexing progress is visible immediately even on large accounts.
- Stable document IDs `nc-<instance-hash>-<fileid>`; `oc:fileid` survives renames/moves.
- Text extracted by Onyx's own `extract_text_and_images` (PDF/DOCX/TXT/MD).
- Files that change between scan and download are re-fetched (3 attempts), then the
  indexing attempt fails rather than indexing a mismatched version.
- Files with no extractable text (scanned PDFs) are skipped with a warning.
- Oversized files (`max_file_size_mb`, default 25) are skipped.
- Scan/auth/download errors fail the indexing attempt instead of publishing a
  partial result, so nothing is removed from the index on a broken scan.
- `folders: []` means the whole account; otherwise only the listed folders (and
  their subfolders) are indexed.
- `validate_connector_settings` runs as Onyx's "Connector settings validation"
  capability check: a live WebDAV call that reports a bad URL, bad app password
  or missing folder.
- The connector form can **browse folders** (`POST /manage/admin/nextcloud/browse`),
  which lists folders using the stored credential — no paths typed by hand.
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

then `docker compose up -d` in that directory recreates `api_server`,
`background` and `web_server`. `nginx`, the model servers and all data
containers stay untouched.

**Always restart nginx afterwards:**

```sh
docker restart onyx-nginx-1
```

`nginx` resolves `proxy_pass http://api_server` once at startup, and recreating
the backend containers gives them new IPs. Without the restart every `/api/*`
request returns `502 Bad Gateway` (HTML), which the UI surfaces as
"backend is currently unavailable" / `JSON.parse: unexpected character`.

## Connect a local notmuch mail MCP server

The host's `mcp-server-notmuch` speaks stdio, while Onyx accepts network MCP
transports. `notmuch-mcp.Dockerfile` packages the upstream read-only server
with Supergateway as Streamable HTTP. The Compose service has **no published
port** and runs on an internal Docker network shared only with `api_server`.
The mail database and config are mounted read-only; no `--allow-drafts`,
`--allow-tags` or `--allow-export` flags are passed.

For a local deployment, add a `notmuch-mcp` service to the deployment's
`docker-compose.override.yml` and attach `api_server` to the same external
internal network:

```yaml
services:
  api_server:
    networks: [default, notmuch-mcp]
  notmuch-mcp:
    build:
      context: /path/to/onyx-nextcloud-connector/onyx-patch
      dockerfile: notmuch-mcp.Dockerfile
    environment:
      HOME: ${HOME}
      NOTMUCH_CONFIG: ${HOME}/.notmuch-config
    volumes:
      - ${HOME}/.config/mcp-server-notmuch/config.toml:${HOME}/.config/mcp-server-notmuch/config.toml:ro
      - ${HOME}/.config/notmuch/config:${HOME}/.notmuch-config:ro
      - ${HOME}/.local/share/mail:${HOME}/.local/share/mail:ro
    networks: [notmuch-mcp]
networks:
  notmuch-mcp:
    external: true
    name: onyx-notmuch-mcp
```

Create the isolated network once (choose a private subnet that does not
overlap the host LAN or existing Docker networks), then start the service:

```sh
docker network create --internal --subnet 10.254.250.0/24 onyx-notmuch-mcp
docker compose up -d api_server notmuch-mcp
```

Onyx v4.9 defaults to blocking private outbound MCP URLs. To let its API server
reach this RFC1918-only network, set `MCP_SERVER_ALLOW_PRIVATE_NETWORK=true`
in the deployment `.env` (or Admin Panel → Organization → Security & Hardening
→ SSRF Protection → Allow Private Network) and recreate `api_server`. This
allows admin-configured MCP/OAuth connections to reach RFC1918 addresses, but
still blocks loopback and cloud metadata endpoints. Keep this bridge on the
isolated network; do not publish port 8765 to the host/LAN.

Then in **Admin Panel → MCP Actions → Add MCP Server**:

- URL: `http://notmuch-mcp:8765/mcp`
- Authentication: **No Auth** (the endpoint has no host/LAN port and is only
  reachable from `api_server` on the dedicated internal network)
- Visibility: **private**, assigned only to your user/group; do not make mail
  tools public to other Onyx users
- Connect, then enable only the read tools you need. Draft, tag and export tools
  are not registered by this server.

The wrapper was verified from the Onyx API container: Streamable HTTP
initialization succeeds and it lists the notmuch read-tier tools without
fetching or printing any email content.

## Rollback

Comment out the two `ONYX_*_IMAGE` lines in the deployment `.env` and run
`docker compose up -d` — the stock `v4.9.0` images are still present locally.

## Upgrade to a newer Onyx

1. Rebase `backend/onyx/` overlay files on the new tag (the connector package
   itself rarely changes; `constants.py`/`registry.py` may shift).
2. Regenerate `web/nextcloud-web.patch`: clone the new tag, apply this patch
   (resolve conflicts), `git diff > nextcloud-web.patch`.
3. Rebuild both images with the new tag and update the deployment `.env`.

## Embedding model performance

Onyx v4.9 embeds with a local CPU model by default (nomic-embed-text-v1,
8 threads, ~20 s per 8 chunks). For anything beyond a handful of small files,
set a faster embedder — either in the UI
(`Admin Panel → Index Settings → Document Processing → Embedding Model`,
e.g. OpenAI `text-embedding-3-small`) or via the deployment `.env`:

```env
DOCUMENT_ENCODER_MODEL=BAAI/bge-small-en-v1.5
DOC_EMBEDDING_DIM=384
EMBEDDING_BATCH_SIZE=32
INDEXING_EMBEDDING_MODEL_NUM_THREADS=16
```

then `docker compose up -d` in the deployment directory. Changing the
embedding model requires a full re-index (no data is lost when nothing was
indexed yet).

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
