"""Command-line interface for the Nextcloud -> Onyx sync bridge."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from .config import Config, ConfigError
from .nextcloud import NextcloudAuthError, NextcloudClient
from .onyx import OnyxAuthError, OnyxClient
from .state import SyncState
from .sync import RunLock, run_sync

BANNER = r"""
  _   _  ___  ___   ___    ___  _  _  _____  _____ ___  _   _
 | \ | |/ __|| \ \ / / |  / _ \| \| ||  _  ||  _  |/ __|| | | |
 |  \| |\__ \|  \ V /| |_| (_) | .` || |_| || |_| |\__ \| |_| |
 |_|\__||___/  \_/   \___/\___/|_|\_||_____||_____||___/ \___/
 Nextcloud  ->  Onyx sync bridge
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="onyx-nextcloud-connector", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sync = sub.add_parser("sync", help="scan Nextcloud and push documents to Onyx")
    mode = sync.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single sync (default)")
    mode.add_argument("--dry-run", action="store_true", help="report actions without applying")
    mode.add_argument("--daemon", action="store_true", help="run continuously")
    sync.add_argument(
        "--interval",
        type=int,
        default=None,
        help="daemon interval in seconds (default: SYNC_INTERVAL_SECONDS or 600)",
    )
    return parser


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level)
    if args.dry_run:
        print(BANNER)
    logging.getLogger(__name__).info(
        "Bridge start: %d folder(s), interval %ds, max %d MiB/file",
        len(config.nc_folders),
        args.interval or config.sync_interval_seconds,
        config.max_file_bytes // (1024 * 1024),
    )

    state = SyncState(config.state_path)
    nc = NextcloudClient(
        config.nc_url,
        config.nc_username,
        config.nc_app_password,
        verify_tls=config.nc_verify_tls,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )
    onyx = OnyxClient(
        config.onyx_url,
        config.onyx_api_key,
        verify_tls=config.onyx_verify_tls,
        timeout=config.request_timeout,
        max_retries=config.max_retries,
    )

    try:
        if args.daemon:
            return _daemon(config, args, state, nc, onyx)
        return _run_once(state, nc, onyx, config, dry_run=args.dry_run)
    except OnyxAuthError as exc:
        logging.getLogger(__name__).error("Onyx authentication failed: %s", exc)
        return 2
    except NextcloudAuthError as exc:
        logging.getLogger(__name__).error("Nextcloud authentication failed: %s", exc)
        return 2
    finally:
        state.close()
        nc.close()
        onyx.close()


def _run_once(
    state: SyncState,
    nc: NextcloudClient,
    onyx: OnyxClient,
    config: Config,
    *,
    dry_run: bool,
) -> int:
    log = logging.getLogger(__name__)
    try:
        with RunLock(config.state_path):
            report = run_sync(config, state, nc, onyx, dry_run=dry_run)
    except RuntimeError as exc:
        log.warning("Skipping run: %s", exc)
        return 0
    if dry_run:
        log.info(
            "Dry run: would ingest %d, would delete %d, unchanged %d, skipped %d",
            report.would_ingest,
            report.would_delete,
            report.unchanged,
            report.skipped,
        )
        return 0 if not report.scan_errors else 1
    log.info(
        "Run: scanned %d, ingested %d, unchanged %d, deleted %d, skipped %d, failed %d",
        report.scanned,
        report.ingested,
        report.unchanged,
        report.deleted,
        report.skipped,
        report.failed,
    )
    return 0 if report.ok else 1


def _daemon(
    config: Config,
    args: argparse.Namespace,
    state: SyncState,
    nc: NextcloudClient,
    onyx: OnyxClient,
) -> int:
    log = logging.getLogger(__name__)
    interval = args.interval or config.sync_interval_seconds
    stop = {"flag": False}

    def _handle(_signum: int, _frame: object) -> None:
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    log.info("Daemon started: interval %ds", interval)
    while not stop["flag"]:
        _run_once(state, nc, onyx, config, dry_run=False)
        deadline = time.monotonic() + interval
        while not stop["flag"] and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))
    log.info("Daemon stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
