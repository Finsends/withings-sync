"""Synthetic chart data and owner-only HTTP regressions; no live measurements."""

import http.client
import json
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from withings_sync.chart_data import (
    ChartInputError,
    available_metrics,
    measurement_series,
)
from withings_sync.local_ingest import sync
from withings_sync.local_status import create_server

TZ = ZoneInfo("Europe/Amsterdam")


def populate(runtime):
    class Provider:
        account_id = "synthetic"

        def __init__(self, folder):
            pass

        def request(self, parameters):
            if parameters["category"] == 2:
                return {
                    "status": 0,
                    "body": {
                        "measuregrps": [
                            {
                                "grpid": 999,
                                "date": 1792886400,
                                "measures": [{"type": 1, "value": 1, "unit": 0}],
                            }
                        ]
                    },
                }
            times = [
                datetime(2026, 10, 24, 23, 59, 59, tzinfo=TZ),
                datetime(2026, 10, 25, 0, 0, tzinfo=TZ),
                datetime(2026, 10, 25, 23, 59, 59, tzinfo=TZ),
                datetime(2026, 10, 26, 0, 0, tzinfo=TZ),
            ]
            return {
                "status": 0,
                "body": {
                    "measuregrps": [
                        {
                            "grpid": index,
                            "date": int(at.timestamp()),
                            "measures": [
                                {"type": 1, "value": 71234 + index, "unit": -3},
                                {"type": 6, "value": 12345, "unit": -3},
                                {"type": 227, "value": 1, "unit": 0},
                            ],
                        }
                        for index, at in enumerate(times)
                    ]
                },
            }

    sync(runtime, provider_factory=Provider)


def params(metric="1", start="2026-10-25", end="2026-10-25"):
    return {"metric": [metric], "start": [start], "end": [end]}


def test_series_uses_exact_scale_and_inclusive_local_dst_end_date(tmp_path):
    populate(tmp_path)
    data = measurement_series(tmp_path, params())
    assert [p["value"] for p in data["points"]] == [71.235, 71.236]
    assert (
        data["points"][1]["timestamp"] - data["points"][0]["timestamp"] == 25 * 3600 - 1
    )
    assert data["metric"]["label"] == "Weight" and data["metric"]["unit"] == "kg"
    assert "synthetic" not in json.dumps(data)
    fat = measurement_series(tmp_path, params(metric="6"))
    assert fat["metric"]["unit"] == "%"
    assert [p["value"] for p in fat["points"]] == [12.345, 12.345]


def test_metadata_has_only_nine_selected_metrics_and_data_bounds(tmp_path):
    populate(tmp_path)
    data = available_metrics(tmp_path)
    assert {m["id"] for m in data["metrics"]} == {1, 5, 6, 8, 76, 77, 88, 170, 226}
    assert data["start"] == "2026-10-24" and data["end"] == "2026-10-26"
    assert all(m["description"] and m["unit"] for m in data["metrics"])


@pytest.mark.parametrize(
    "query",
    [
        params(metric="227"),
        params(metric="11"),
        params(metric="4"),
        params(metric="1 OR 1"),
        params(start="2026-10-26", end="2026-10-25"),
        params(start="2026-02-30"),
        params(start="20261025"),
        {**params(), "metric": ["1", "6"]},
        {**params(), "extra": ["1"]},
        {"metric": ["1"]},
    ],
)
def test_invalid_query_rejected_before_opening_database(tmp_path, query):
    with pytest.raises(ChartInputError):
        measurement_series(tmp_path, query)
    assert not (tmp_path / "withings.sqlite3").exists()


def test_empty_range_is_explicit_and_result_limit_never_truncates(
    tmp_path, monkeypatch
):
    populate(tmp_path)
    assert (
        measurement_series(tmp_path, params(start="2026-10-28", end="2026-10-29"))[
            "points"
        ]
        == []
    )
    monkeypatch.setattr("withings_sync.chart_data.MAX_POINTS", 1)
    with pytest.raises(ChartInputError, match="shorter date range"):
        measurement_series(tmp_path, params())


@pytest.fixture
def http_server(tmp_path, monkeypatch):
    populate(tmp_path)
    monkeypatch.setenv("WITHINGS_TAILSCALE_USER", "owner@example.com")
    server = create_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def get(server, path, headers=None):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        client.request("GET", path, headers=headers or {})
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


def test_chart_http_requires_owner_identity_and_preserves_count_only_health(
    http_server, monkeypatch
):
    assert get(http_server, "/withings/")[0] == 403
    assert (
        get(
            http_server, "/withings/api/series?metric=1&start=2026-10-25&end=2026-10-25"
        )[0]
        == 403
    )
    assert (
        get(http_server, "/withings/", {"Tailscale-User-Login": "other@example.com"})[0]
        == 403
    )
    assert get(http_server, "/api/health")[0] == 200
    status, _, raw = get(http_server, "/api/coverage")
    assert status == 200 and b'"value"' not in raw
    monkeypatch.delenv("WITHINGS_TAILSCALE_USER")
    assert (
        get(http_server, "/withings/", {"Tailscale-User-Login": "owner@example.com"})[0]
        == 403
    )


def test_chart_http_mount_assets_queries_and_safe_errors(http_server):
    owner = {
        "Tailscale-User-Login": "owner@example.com",
        "Host": "mothership.tailfe5410.ts.net:8793",
    }
    status, headers, body = get(http_server, "/withings/", owner)
    assert status == 200 and b'id="start-date"' in body
    assert (
        headers["Cache-Control"] == "no-store"
        and "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    )
    assert get(http_server, "/withings/chart.js", owner)[0] == 200
    assert get(http_server, "/withings/vendor/chart.umd.js", owner)[0] == 200
    assert get(http_server, "/withings/api/metrics", owner)[0] == 200
    status, _, body = get(
        http_server,
        "/withings/api/series?metric=6&start=2026-10-25&end=2026-10-25",
        owner,
    )
    assert status == 200 and json.loads(body)["metric"]["unit"] == "%"
    assert (
        get(
            http_server,
            "/withings/api/series?metric=227&start=2026-10-25&end=2026-10-25",
            owner,
        )[0]
        == 400
    )
    assert (
        get(http_server, "/withings/../../config/.withings_user.json", owner)[0] == 404
    )
    assert (
        get(http_server, "/withings/", {**owner, "Host": "attacker.invalid"})[0] == 403
    )
