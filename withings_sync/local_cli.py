"""Native local workflow; no browser login prompts or Garmin uploads."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def backup(runtime: Path):
    """Copy using SQLite's online backup API, verify, then publish atomically."""
    folder = runtime / "snapshots"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    target = folder / datetime.now(ZoneInfo("Europe/Amsterdam")).strftime(
        "withings-%Y%m%d-%H%M%S.db"
    )
    temporary = target.with_suffix(".partial")
    if target.exists() or temporary.exists():
        raise FileExistsError("Snapshot filename already exists")
    source = sqlite3.connect(
        (runtime / "withings.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
    )
    try:
        dest = sqlite3.connect(temporary)
        try:
            temporary.chmod(0o600)
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot integrity failed")
            if dest.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("Snapshot foreign keys failed")
        finally:
            dest.close()
        os.replace(temporary, target)
    finally:
        source.close()
        temporary.unlink(missing_ok=True)
    return target


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runtime", type=Path, default=Path.home() / ".local/share/withings-sync"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("sync").add_argument("--full", action="store_true")
    commands.add_parser("backup")
    commands.add_parser("verify")
    commands.add_parser("serve").add_argument("--port", type=int, default=18796)
    args = parser.parse_args()
    try:
        if args.command == "sync":
            from .local_ingest import sync

            result = sync(args.runtime, full=args.full)
            backup(args.runtime)
            print(json.dumps(result, sort_keys=True))
        elif args.command == "backup":
            backup(args.runtime)
            print("Verified SQLite snapshot created")
        elif args.command == "verify":
            from .local_status import coverage

            data = coverage(args.runtime)
            print(
                json.dumps(
                    {
                        "days": len(data["days"]),
                        "first_date": data["days"][0]["date"],
                        "last_date": data["days"][-1]["date"],
                        "sync_status": data["sync"]["status"],
                    }
                )
            )
        else:
            from .local_status import serve

            serve(args.runtime, args.port)
    except Exception as exc:  # noqa: BLE001 -- CLI boundary must redact provider failures.
        # Exception messages from HTTP providers may contain private payloads.
        print(f"Withings workflow failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
