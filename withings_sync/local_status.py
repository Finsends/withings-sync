"""Read-only, count-only coverage for the existing Application Health UI."""

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
LABELS = {
    1: "Weight",
    4: "Height",
    5: "Fat-free mass",
    6: "Fat ratio",
    8: "Fat mass",
    9: "Diastolic blood pressure",
    10: "Systolic blood pressure",
    11: "Heart pulse",
    12: "Temperature",
    54: "Oxygen saturation",
    71: "Body temperature",
    73: "Skin temperature",
    76: "Muscle mass",
    77: "Hydration",
    88: "Bone mass",
    91: "Pulse wave velocity",
    155: "Vascular age",
    167: "Nerve health score (feet maximum)",
    170: "Visceral fat index",
    226: "Basal metabolic rate",
}


def coverage(runtime: Path, at=None):
    """Obtain one consistent SQLite snapshot; never return measurement values."""
    at = at or datetime.now(timezone.utc)
    today = at.astimezone(TZ).date()
    path = runtime / "withings.sqlite3"
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        states = conn.execute("SELECT * FROM sync_state").fetchall()
        if len(states) != 1:
            raise ValueError("One verified source account is required")
        state = dict(states[0])
        run = conn.execute(
            "SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run = dict(run) if run else {}
        groups = conn.execute(
            "SELECT category,group_id,measured_at FROM measurement_groups WHERE account_id=?",
            (state["account_id"],),
        ).fetchall()
        measures = conn.execute(
            "SELECT g.measured_at,m.category,m.measure_type,COUNT(*) n "
            "FROM measurements m JOIN measurement_groups g USING(account_id,category,group_id) "
            "WHERE m.account_id=? GROUP BY g.measured_at,m.category,m.measure_type",
            (state["account_id"],),
        ).fetchall()
    finally:
        conn.close()
    checked = state["last_success_at"]
    checked_end = datetime.fromisoformat(state["window_end_date"]).date()
    coverage_end = min(today, checked_end)
    by_day = defaultdict(Counter)
    types = set()
    first = coverage_end
    for group in groups:
        day = datetime.fromtimestamp(group["measured_at"], TZ).date()
        if group["category"] == 1 and day > today:
            raise ValueError("Future-dated source measurements require inspection")
        if group["category"] == 2 and day > coverage_end:
            # Goals can target a future date. Display inventory separately; do
            # not certify a future measurement date as historically retrieved.
            by_day[coverage_end]["future_objectives"] += 1
            continue
        first = min(first, day)
        by_day[day][
            "measurement_groups" if group["category"] == 1 else "objectives"
        ] += 1
    for measure in measures:
        if measure["category"] != 1:
            continue
        day = datetime.fromtimestamp(measure["measured_at"], TZ).date()
        code = measure["measure_type"]
        types.add(code)
        by_day[day]["measurements"] += measure["n"]
        by_day[day][f"metric_{code}"] += measure["n"]
        if code not in LABELS:
            by_day[day]["unknown_measurements"] += measure["n"]
    if (coverage_end - first).days >= 20000 or len(types) > 94:
        raise ValueError("Coverage exceeds the dashboard evidence limit")
    labels = {
        "measurement_groups": "Measurement groups",
        "measurements": "Observations",
        "unknown_measurements": "Observations with unmapped metric types",
        "metric_types": "Distinct metric types on this date",
        "objectives": "Objective groups",
        "future_objectives": "Future-dated objective groups (inventory as of last sync)",
    }
    labels.update(
        {
            f"metric_{code}": f"{LABELS.get(code, 'Unmapped metric')} (type {code})"
            for code in sorted(types)
        }
    )
    backfilled = bool(state["backfill_complete"])
    # A prior complete backfill certifies absence only through its successful end date.
    days = []
    for offset in range((coverage_end - first).days + 1):
        day = first + timedelta(days=offset)
        counts = by_day[day]
        counts["metric_types"] = sum(counts[f"metric_{code}"] > 0 for code in types)
        covered = backfilled and checked_end is not None and day <= checked_end
        tables = [
            {
                "table": key,
                "label": label,
                "records": counts[key],
                "state": ("present" if counts[key] else "source_empty")
                if covered
                else "missing",
                "backfilled": covered,
                "checked_at": checked if covered else None,
                "core": False,
            }
            for key, label in labels.items()
        ]
        days.append(
            {
                "date": day.isoformat(),
                "state": "missing"
                if not covered
                else "partial"
                if day == today
                else "complete",
                "backfilled": covered,
                "records": sum(t["records"] for t in tables),
                "today": day == today,
                "tables": tables,
            }
        )
    completed = run.get("finished_at")
    started = run.get("started_at")

    def local_date(value):
        return (
            datetime.fromisoformat(value).astimezone(TZ).date().isoformat()
            if value
            else None
        )

    return {
        "app": "withings-sync",
        "schema_version": 1,
        "description": "Withings measurement history stored locally. Counts only; health values remain private.",
        "observed_at": at.isoformat(),
        "database": "SQLite",
        "timezone": str(TZ),
        "days": days,
        "sync": {
            "status": "ok"
            if run.get("status") == "success"
            else run.get("status", "not_run"),
            "started_at": started,
            "completed_at": completed,
            "start_date": first.isoformat(),
            "end_date": local_date(completed or started),
            "error_type": run.get("error_code"),
            "last_source_sync_at": checked,
            "last_source_sync_end_date": state["window_end_date"] if checked else None,
            "mode": "live" if run else None,
        },
    }


def create_server(runtime: Path, port: int):
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def do_GET(self):
            # Reject arbitrary Host headers, including DNS rebinding from public pages.
            if self.headers.get("Host") not in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }:
                self.send_error(403)
                return
            if urlsplit(self.path).path != "/api/coverage":
                self.send_error(404)
                return
            try:
                body = json.dumps(coverage(runtime), separators=(",", ":")).encode()
                if len(body) > 16 * 1024 * 1024:
                    raise ValueError("Coverage response exceeds limit")
                code = 200
            except Exception:  # noqa: BLE001 -- Never expose database details in HTTP errors.
                code, body = 503, b'{"app":"withings-sync","status":"unavailable"}'
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(runtime: Path, port: int):
    with create_server(runtime, port) as server:
        server.serve_forever()
