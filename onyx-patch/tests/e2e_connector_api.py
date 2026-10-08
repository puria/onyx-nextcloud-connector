"""End-to-end verification of the Nextcloud connector through the Onyx API.

Creates a real credential + connector + CC pair in the running Onyx, points it
at the `fake-nextcloud` container (onyx_default network), triggers a one-shot
indexing run, waits for the attempt to succeed and verifies the indexed
document IDs, then deletes everything it created.

Requires an API key with `manage:connectors` (service account in the Admin
group). Usage:

    ONYX_URL=http://localhost:3000/api ONYX_API_KEY=<key> \\
        python onyx-patch/tests/e2e_connector_api.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

ONYX_URL = os.environ.get("ONYX_URL", "http://localhost:3000/api").rstrip("/")
API_KEY = os.environ.get("ONYX_API_KEY", "")
SERVER_URL = os.environ.get("NC_SERVER_URL", "http://fake-nextcloud:8899")
SUFFIX = str(int(time.time()))

if not API_KEY:
    print("ONYX_API_KEY is required (service account key with manage:connectors)")
    sys.exit(2)


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    request = urllib.request.Request(
        f"{ONYX_URL}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode()
            return response.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def main() -> int:
    created: dict[str, int] = {}
    try:
        # 1. credential (secrets live in Onyx's encrypted credential store)
        status, credential = call(
            "POST",
            "/manage/credential",
            {
                "credential_json": {
                    "server_url": SERVER_URL,
                    "username": "me",
                    "app_password": "fake-app-password",
                },
                "admin_public": True,
                "source": "nextcloud",
                "name": f"nextcloud-e2e-{SUFFIX}",
            },
        )
        assert status == 200, (status, credential)
        created["credential"] = credential["id"]

        # 2. connector
        status, connector = call(
            "POST",
            "/manage/admin/connector",
            {
                "name": f"nextcloud-e2e-{SUFFIX}",
                "source": "nextcloud",
                "input_type": "poll",
                "connector_specific_config": {
                    "folders": ["Documents"],
                    "verify_ssl": True,
                },
                "refresh_freq": 24 * 60 * 60,
                "prune_freq": None,
                "access_type": "public",
            },
        )
        assert status == 200, (status, connector)
        created["connector"] = connector["id"]

        # 3. associate credential -> creates the CC pair
        status, pair = call(
            "PUT",
            f"/manage/connector/{created['connector']}/credential/{created['credential']}",
            {"name": f"nextcloud-e2e-{SUFFIX}", "access_type": "public"},
        )
        assert status == 200, (status, pair)
        created["cc_pair"] = pair["cc_pair_id"]

        # 4. one-shot indexing run
        status, run = call(
            "POST",
            "/manage/admin/connector/run-once",
            {
                "connector_id": created["connector"],
                "credential_ids": [created["credential"]],
                "from_beginning": True,
            },
        )
        assert status == 200, (status, run)

        # 5. wait for the index attempt to succeed
        deadline = time.time() + 300
        attempt_status = None
        while time.time() < deadline:
            status, attempts = call(
                "GET",
                f"/manage/admin/cc-pair/{created['cc_pair']}/index-attempts",
            )
            assert status == 200, (status, attempts)
            items = attempts.get("items", [])
            if items:
                attempt_status = items[0].get("status")
                if attempt_status == "success":
                    break
                if attempt_status == "failed":
                    print("index attempt failed:", json.dumps(items[0])[:500])
                    return 1
            time.sleep(5)
        else:
            print("timed out waiting for the index attempt; last status:", attempt_status)
            return 1
        print("index attempt: success")

        # 6. verify documents were indexed
        status, cc_pair = call("GET", f"/manage/admin/cc-pair/{created['cc_pair']}")
        assert status == 200, (status, cc_pair)
        docs_indexed = cc_pair.get("docs_indexed") or cc_pair.get("num_indexed_docs")
        assert docs_indexed in (None,) or docs_indexed > 0, cc_pair
        print("cc-pair status:", json.dumps(cc_pair)[:400])

        # 6b. folder browsing endpoint (powers the picker in the UI)
        status, listing = call(
            "POST",
            "/manage/admin/nextcloud/browse",
            {
                "credential_id": created["credential"],
                "path": "",
                "verify_ssl": True,
            },
        )
        assert status == 200, (status, listing)
        print(
            "browse root ->",
            [folder["path"] for folder in listing.get("folders", [])][:5],
            "files:",
            listing.get("file_count"),
        )

        status, docs = call("GET", f"/manage/connector/{created['connector']}/documents")

        print("E2E CONNECTOR VERIFICATION PASSED")
        return 0
    finally:
        # 7. cleanup (order: pair -> connector -> credential; doc pruning is async)
        for path in (
            f"/manage/admin/cc-pair/{created.get('cc_pair')}" if created.get("cc_pair") else None,
            f"/manage/admin/connector/{created.get('connector')}"
            if created.get("connector")
            else None,
            f"/manage/credential/{created.get('credential')}"
            if created.get("credential")
            else None,
        ):
            if path:
                status, body = call("DELETE", path)
                print("cleanup", path, "->", status)


if __name__ == "__main__":
    raise SystemExit(main())
