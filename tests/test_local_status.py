"""Count-only evidence uses committed source receipts and verified snapshots."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from withings_sync.local_cli import backup
from withings_sync.local_ingest import SyncError, sync
from withings_sync.local_status import coverage


class Provider:
    account_id = "test-account"

    def __init__(self, folder):
        pass

    def request(self, params):
        groups = (
            []
            if params["category"] == 2
            else [
                {
                    "grpid": 1,
                    "category": 1,
                    "date": 1782896400,
                    "modified": 1782896400,
                    "measures": [
                        {"type": 1, "value": 7123456, "unit": -5},
                        {"type": 9999, "value": 1234567, "unit": -4},
                    ],
                }
            ]
        )
        return {"status": 0, "body": {"measuregrps": groups}}


def test_count_only_coverage_keeps_unmapped_types_and_valid_empty_days(tmp_path):
    sync(tmp_path, provider_factory=Provider)
    data = coverage(tmp_path)
    serialized = json.dumps(data)
    assert "7123456" not in serialized and "1234567" not in serialized
    assert "test-account" not in serialized
    assert data["sync"]["status"] == "ok"
    observed = next(d for d in data["days"] if any(t["records"] for t in d["tables"]))
    tables = {t["table"]: t for t in observed["tables"]}
    assert tables["measurement_groups"]["records"] == 1
    assert tables["measurements"]["records"] == 2
    assert tables["metric_9999"]["records"] == 1
    assert tables["unknown_measurements"]["records"] == 1
    assert tables["metric_types"]["records"] == 2
    assert all(not t["core"] for t in observed["tables"])
    empty = next(d for d in data["days"] if not d["records"] and not d["today"])
    assert empty["state"] == "complete" and empty["backfilled"]


def test_failure_does_not_claim_fresh_success(tmp_path):
    sync(tmp_path, provider_factory=Provider)
    previous = coverage(tmp_path)["sync"]["last_source_sync_at"]

    class Failed(Provider):
        def request(self, params):
            raise RuntimeError("private error payload")

    with pytest.raises(SyncError):
        sync(tmp_path, provider_factory=Failed)
    data = coverage(tmp_path)
    assert data["sync"]["status"] == "failed"
    assert data["sync"]["last_source_sync_at"] == previous
    assert "private error payload" not in json.dumps(data)


@pytest.mark.parametrize("days_later", [1, 2])
def test_unchecked_dates_are_outside_calendar_not_false_missing(tmp_path, days_later):
    sync(tmp_path, provider_factory=Provider)
    before = coverage(tmp_path)
    later = datetime.fromisoformat(before["observed_at"]).replace(tzinfo=timezone.utc)
    from datetime import timedelta

    after = coverage(tmp_path, later + timedelta(days=days_later))
    assert after["days"][-1]["date"] == before["days"][-1]["date"]
    assert after["days"][-1]["state"] == "complete"
    assert all(day["backfilled"] for day in after["days"])
    assert not any(day["today"] for day in after["days"])


def test_empty_archive_remains_valid_on_the_next_morning(tmp_path):
    class Empty(Provider):
        def request(self, params):
            return {"status": 0, "body": {"measuregrps": []}}

    sync(tmp_path, provider_factory=Empty)
    from datetime import timedelta

    after = coverage(tmp_path, datetime.now(timezone.utc) + timedelta(days=1))
    assert len(after["days"]) == 1 and not after["days"][0]["records"]
    assert after["days"][0]["state"] == "complete"


def test_backup_restores_all_rows_and_has_private_permissions(tmp_path):
    sync(tmp_path, provider_factory=Provider)
    target = backup(tmp_path)
    assert target.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(target) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT COUNT(*) FROM measurements").fetchone()[0] == 2
        assert (
            restored.execute(
                "SELECT raw_value FROM measurements WHERE measure_type=1"
            ).fetchone()[0]
            == 7123456
        )
    assert not list(target.parent.glob("*.partial"))


def test_future_goals_remain_visible_without_future_calendar_coverage(tmp_path):
    class WithGoal(Provider):
        def request(self, params):
            if params["category"] == 1:
                return super().request(params)
            return {
                "status": 0,
                "body": {
                    "measuregrps": [
                        {
                            "grpid": 99,
                            "category": 2,
                            "date": 2208988800,
                            "measures": [{"type": 1, "value": 70000, "unit": -3}],
                        }
                    ]
                },
            }

    sync(tmp_path, provider_factory=WithGoal)
    data = coverage(tmp_path)
    tables = {t["table"]: t for t in data["days"][-1]["tables"]}
    assert tables["future_objectives"]["records"] == 1
    assert data["days"][-1]["today"]
