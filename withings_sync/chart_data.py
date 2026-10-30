"""Bounded, read-only series for the owner's Withings chart."""

import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Amsterdam")
MAX_POINTS = 50_000
METRICS = {
    1: ("Weight", "kg", "Body weight recorded at each measurement."),
    8: ("Fat mass", "kg", "Estimated body fat mass."),
    6: ("Body fat", "%", "Estimated percentage of body weight that is fat."),
    77: (
        "Body water",
        "kg",
        "Estimated total body water. Water is already contained within other body tissues.",
    ),
    76: (
        "Muscle mass",
        "kg",
        "Estimated muscle mass. Short-term changes can reflect changes in body water.",
    ),
    5: (
        "Fat-free mass",
        "kg",
        "Estimated body mass excluding fat, including water, muscle, bone and organs.",
    ),
    170: (
        "Visceral fat index",
        "index",
        "Withings' estimate of fat around internal organs, expressed as an index.",
    ),
    226: (
        "Basal metabolic rate",
        "kcal/day",
        "Estimated energy needed for basic bodily functions at rest. This is not total daily energy expenditure.",
    ),
    88: ("Bone mass", "kg", "Estimated bone mass."),
}


class ChartInputError(ValueError):
    """Safe validation message suitable for the chart UI."""


def metric_info(metric):
    label, unit, description = METRICS[metric]
    return {"id": metric, "label": label, "unit": unit, "description": description}


@contextmanager
def _database(runtime: Path):
    path = (runtime / "withings.sqlite3").resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        accounts = connection.execute("SELECT account_id FROM sync_state").fetchall()
        if len(accounts) != 1:
            raise sqlite3.DatabaseError("One source account is required")
        yield connection, accounts[0][0]
    finally:
        connection.close()


def available_metrics(runtime: Path):
    placeholders = ",".join("?" for _ in METRICS)
    with _database(runtime) as (db, account):
        first, last = db.execute(
            "SELECT MIN(g.measured_at),MAX(g.measured_at) FROM measurement_groups g "
            "WHERE g.account_id=? AND g.category=1 AND EXISTS "
            "(SELECT 1 FROM measurements m WHERE m.account_id=g.account_id "
            "AND m.category=g.category AND m.group_id=g.group_id "
            f"AND m.measure_type IN ({placeholders}))",
            (account, *METRICS),
        ).fetchone()

    def local_date(stamp):
        return (
            datetime.fromtimestamp(stamp, TZ).date().isoformat()
            if stamp is not None
            else None
        )

    return {
        "metrics": [metric_info(m) for m in METRICS],
        "start": local_date(first),
        "end": local_date(last),
    }


def measurement_series(runtime: Path, parameters):
    if set(parameters) != {"metric", "start", "end"} or any(
        len(v) != 1 for v in parameters.values()
    ):
        raise ChartInputError("Choose one metric, start date and end date.")
    metric_text, start_text, end_text = (
        parameters[k][0] for k in ("metric", "start", "end")
    )
    if not re.fullmatch(r"[0-9]{1,3}", metric_text) or int(metric_text) not in METRICS:
        raise ChartInputError("Choose one of the nine available metrics.")
    metric = int(metric_text)
    try:
        if any(
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
            for text in (start_text, end_text)
        ):
            raise ValueError
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        if start > end or (end - start).days >= 20_000:
            raise ValueError
        start_epoch = int(datetime.combine(start, time.min, TZ).timestamp())
        # Amsterdam midnight bounds include all of the end date, including DST days.
        end_epoch = int(
            datetime.combine(end + timedelta(days=1), time.min, TZ).timestamp()
        )
    except (ValueError, OverflowError):
        raise ChartInputError(
            "Choose valid dates with the start on or before the end, within 20,000 days."
        ) from None
    with _database(runtime) as (db, account):
        rows = db.execute(
            "SELECT g.measured_at,m.raw_value,m.unit FROM measurements m "
            "JOIN measurement_groups g USING(account_id,category,group_id) "
            "WHERE m.account_id=? AND m.category=1 AND m.measure_type=? "
            "AND g.measured_at>=? AND g.measured_at<? "
            "ORDER BY g.measured_at,g.group_id,m.measurement_index LIMIT ?",
            (account, metric, start_epoch, end_epoch, MAX_POINTS + 1),
        ).fetchall()
    if len(rows) > MAX_POINTS:
        raise ChartInputError(
            "This range has too many readings. Choose a shorter date range."
        )
    points = []
    for stamp, raw, exponent in rows:
        if not isinstance(exponent, int) or not -12 <= exponent <= 6:
            raise sqlite3.DatabaseError("Unsupported source exponent")
        value = float(Decimal(raw) * Decimal(10) ** exponent)
        if not math.isfinite(value):
            raise sqlite3.DatabaseError("Nonfinite source value")
        points.append({"timestamp": stamp, "value": value})
    return {
        "metric": metric_info(metric),
        "start": start_text,
        "end": end_text,
        "points": points,
    }
