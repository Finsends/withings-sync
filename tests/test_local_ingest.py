"""Synthetic source tests: no credentials, network, or private measurements."""

import json
from copy import deepcopy

import pytest
from withings_sync.local_ingest import (
    OVERLAP_SECONDS,
    AtomicConfig,
    SyncError,
    SyncLocked,
    runtime_lock,
    sync,
)
from withings_sync.local_store import connect


def group(group_id=10, modified=100, **extra):
    return {
        "grpid": group_id,
        "date": 1,
        "modified": modified,
        "measures": [
            {"type": 1, "value": 72001, "unit": -3},
            {"type": 9999, "value": 12, "unit": -1},
            {"type": 9999, "value": 13, "unit": -1},
        ],
        **extra,
    }


def page(groups=(), **extra):
    return {"status": 0, "body": {"measuregrps": list(groups), **extra}}


class Provider:
    account_id = "synthetic-account"

    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    def request(self, params):
        self.requests.append(params)
        result = next(self.pages)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


def run(tmp_path, pages, full=False):
    provider = Provider(pages)
    return sync(tmp_path, full=full, provider_factory=lambda _: provider), provider


def rows(tmp_path, table):
    with connect(tmp_path / "withings.sqlite3", readonly=True) as database:
        return [dict(row) for row in database.execute("SELECT * FROM " + table)]


def test_full_pagination_unknown_and_repeated_measures(tmp_path):
    summary, provider = run(
        tmp_path,
        [
            page([group()], more=1, offset=10),
            page([group(11)]),
            page([group(10, category=2)]),
        ],
    )
    assert summary["mode"] == "full"
    assert summary["response_count"] == 3
    assert provider.requests == [
        {"category": 1, "lastupdate": 0},
        {"category": 1, "lastupdate": 0, "offset": 10},
        {"category": 2, "lastupdate": 0},
    ]
    assert len(rows(tmp_path, "measurement_groups")) == 3
    assert len(rows(tmp_path, "measurements")) == 9
    assert [m["raw_value"] for m in rows(tmp_path, "measurements")[:3]] == [
        72001,
        12,
        13,
    ]
    assert rows(tmp_path, "sync_state")[0]["backfill_complete"] == 1
    for path in tmp_path.iterdir():
        if path.is_file():
            assert path.stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_replay_is_idempotent_and_lastupdate_uses_fetch_start(tmp_path):
    run(tmp_path, [page([group()]), page()])
    state = rows(tmp_path, "sync_state")[0]
    summary, provider = run(tmp_path, [page([group()]), page()])
    assert summary["mode"] == "incremental"
    assert provider.requests[0]["lastupdate"] == max(
        0, state["last_source_sync_at"] - OVERLAP_SECONDS
    )
    assert "startdate" not in provider.requests[0]
    assert len(rows(tmp_path, "measurements")) == 3
    assert len(rows(tmp_path, "source_responses")) == 4
    _, provider = run(tmp_path, [page(), page()], full=True)
    assert provider.requests[0]["lastupdate"] == 0


def test_old_dated_correction_replaces_children_but_older_revision_does_not(tmp_path):
    run(tmp_path, [page([group()]), page()])
    corrected = group(modified=200, measures=[{"type": 1, "value": 73000, "unit": -3}])
    run(tmp_path, [page([corrected]), page()])
    assert len(rows(tmp_path, "measurements")) == 1
    run(tmp_path, [page([group(modified=101)]), page()])
    assert rows(tmp_path, "measurements")[0]["raw_value"] == 73000
    assert rows(tmp_path, "measurement_groups")[0]["modified_at"] == 200


def test_empty_success_keeps_existing_data_and_marks_complete(tmp_path):
    result, _ = run(tmp_path, [page(), page()])
    assert result["group_count"] == 0
    assert rows(tmp_path, "sync_state")[0]["backfill_complete"] == 1
    run(tmp_path, [page([group()]), page()])
    run(tmp_path, [page(), page()])
    assert len(rows(tmp_path, "measurement_groups")) == 1


@pytest.mark.parametrize(
    "bad",
    [
        page([group()], more=1, offset="oops"),
        page([group()], more=1),
        {"status": 0, "body": {}},
        page([group(measures=[{"type": 1, "value": "72", "unit": -3}])]),
        page([group(category=2)]),
        page([group(measures=[])]),
        {"status": 503},
        RuntimeError("secret provider response"),
    ],
)
def test_failed_fetch_preserves_data_checkpoint_and_safe_failure(tmp_path, bad):
    run(tmp_path, [page([group()]), page()])
    before_groups = rows(tmp_path, "measurement_groups")
    before_state = rows(tmp_path, "sync_state")
    with pytest.raises(SyncError, match="previous data retained") as error:
        run(tmp_path, [page([group(11)], more=1, offset=1), bad, page()])
    assert "secret" not in str(error.value)
    assert rows(tmp_path, "measurement_groups") == before_groups
    assert rows(tmp_path, "sync_state") == before_state
    assert rows(tmp_path, "sync_runs")[-1]["status"] == "failed"
    assert any(
        r["page_index"] == 0 and r["run_id"] == 2
        for r in rows(tmp_path, "source_responses")
    )
    assert rows(tmp_path, "source_responses")[-1]["run_id"] == 2


def test_repeated_offset_and_invalid_more_are_rejected(tmp_path):
    with pytest.raises(SyncError):
        run(
            tmp_path,
            [page([group()], more=1, offset=1), page([group()], more=1, offset=1)],
        )
    assert rows(tmp_path, "sync_state") == []
    assert rows(tmp_path, "measurement_groups") == []
    with pytest.raises(SyncError):
        run(tmp_path, [page(more="yes")])


def test_category_two_failure_rolls_back_category_one(tmp_path):
    with pytest.raises(SyncError):
        run(tmp_path, [page([group()]), RuntimeError("no goals")])
    assert rows(tmp_path, "measurement_groups") == []
    assert rows(tmp_path, "sync_state") == []
    assert len(rows(tmp_path, "source_responses")) == 1


def test_lock_precedes_provider_and_database_access(tmp_path):
    with runtime_lock(tmp_path), pytest.raises(SyncLocked):
        sync(
            tmp_path,
            provider_factory=lambda _: pytest.fail("provider called while locked"),
        )
    assert not (tmp_path / "withings.sqlite3").exists()


def test_missing_auth_never_prompts_and_records_failure(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not prompt"))
    with pytest.raises(SyncError):
        sync(tmp_path)
    assert rows(tmp_path, "sync_runs")[-1]["status"] == "failed"


def test_token_write_is_atomic_private_and_readable(tmp_path):
    target = tmp_path / "config" / ".withings_user.json"
    config = AtomicConfig(target)
    config.config = {"access_token": "synthetic", "refresh_token": "synthetic-refresh"}
    config.write()
    assert json.loads(target.read_text()) == config.config
    assert target.stat().st_mode & 0o777 == 0o600
    assert list(target.parent.iterdir()) == [target]


def test_source_responses_preserve_unknown_fields_and_hash(tmp_path):
    import hashlib

    response = page([group(future_vendor_field={"opaque": 1})], vendor="new")
    run(tmp_path, [response, page()])
    raw = rows(tmp_path, "source_responses")[0]
    assert json.loads(raw["response_json"]) == response
    assert (
        hashlib.sha256(raw["response_json"].encode()).hexdigest()
        == raw["response_sha256"]
    )


def test_database_publication_failure_rolls_back_every_group(tmp_path, monkeypatch):
    import withings_sync.local_ingest as ingestion

    run(tmp_path, [page([group()]), page()])
    before = rows(tmp_path, "measurement_groups")
    before_state = rows(tmp_path, "sync_state")
    real_upsert = ingestion._upsert_group

    def fail_after_first(connection, account, category, item):
        if item["grpid"] == 12:
            raise RuntimeError("synthetic database failure")
        return real_upsert(connection, account, category, item)

    monkeypatch.setattr(ingestion, "_upsert_group", fail_after_first)
    with pytest.raises(SyncError):
        run(tmp_path, [page([group(11), group(12)]), page()])
    assert rows(tmp_path, "measurement_groups") == before
    assert rows(tmp_path, "sync_state") == before_state
    assert len(rows(tmp_path, "source_responses")) == 4
    assert rows(tmp_path, "sync_runs")[-1]["status"] == "failed"


@pytest.mark.parametrize(
    "response",
    [
        page(more=1.0),
        page([group(date=True)]),
        page([group(grpid=2**63)]),
        page([group(modified=-1)]),
    ],
)
def test_malformed_fields_never_publish(tmp_path, response):
    with pytest.raises(SyncError):
        run(tmp_path, [response, page()])
    assert rows(tmp_path, "measurement_groups") == []
    assert rows(tmp_path, "sync_state") == []


def test_other_account_is_rejected_before_source_fetch(tmp_path):
    run(tmp_path, [page([group()]), page()])
    before = rows(tmp_path, "sync_state")
    other = Provider([])
    other.account_id = "another-account"
    with pytest.raises(SyncError):
        sync(tmp_path, provider_factory=lambda _: other)
    assert not other.requests
    assert rows(tmp_path, "sync_state") == before
    assert len(rows(tmp_path, "measurement_groups")) == 1


def test_database_uses_wal_and_amsterdam_calendar(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    run(tmp_path, [page(), page()])
    with connect(tmp_path / "withings.sqlite3", readonly=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert (
        rows(tmp_path, "sync_state")[0]["window_end_date"]
        == datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    )


@pytest.mark.parametrize("exhausted", [False, True])
def test_upstream_retry_logs_redact_provider_details(
    monkeypatch, caplog, capsys, exhausted
):
    import logging
    from types import SimpleNamespace

    from withings_sync.local_ingest import LocalProvider
    from withings_sync.withings2 import WithingsException

    private_text = "synthetic-private-measurement-and-token"
    limited = {"status": 601, "error": private_text, "body": {"wait_seconds": 0}}
    responses = iter([limited] * 3 if exhausted else [limited, page()])
    monkeypatch.setattr(
        "withings_sync.withings2.requests.post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None, json=lambda: next(responses)
        ),
    )
    monkeypatch.setattr("withings_sync.withings2.time.sleep", lambda _: None)
    provider = object.__new__(LocalProvider)
    provider.user_config = {"access_token": "synthetic-access-token"}
    logger = logging.getLogger("withings")
    before_filters = list(logger.filters)
    with caplog.at_level(logging.WARNING, logger="withings"):
        if exhausted:
            with pytest.raises(WithingsException):
                provider.request({"category": 1, "lastupdate": 0})
        else:
            assert provider.request({"category": 1, "lastupdate": 0}) == page()
    assert logger.filters == before_filters
    assert "provider details suppressed" in caplog.text
    assert private_text not in caplog.text
    assert "synthetic-access-token" not in caplog.text
    captured = capsys.readouterr()
    assert private_text not in captured.out + captured.err
