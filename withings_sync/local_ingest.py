"""All-history and incremental getmeas ingestion, independent of Garmin uploads."""

import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .local_store import connect, initialize, private_directory
from .withings2 import (
    APP_CONFIG,
    GETMEAS_URL,
    WithingsConfig,
    WithingsOAuth2,
    _api_request,
)

OVERLAP_SECONDS = 7 * 86400
MAX_PAGES = 10000


class SyncError(RuntimeError):
    """An incomplete source response prevented publication."""


class SyncLocked(SyncError):
    """Another collector owns this runtime, including its rotating OAuth token."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _now():
    return datetime.now(UTC).isoformat()


def _integer(value, name, minimum=None):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SyncError(f"Invalid integer field: {name}")
    if minimum is not None and value < minimum:
        raise SyncError(f"Invalid integer field: {name}")
    # SQLite INTEGERs are signed 64-bit; reject rather than silently coerce.
    if not -(2**63) <= value < 2**63:
        raise SyncError(f"Integer outside storage range: {name}")
    return value


class AtomicConfig(WithingsConfig):
    """Persist rotating tokens without exposing a partial or permissive file."""

    def write(self):
        target = Path(self.config_file)
        private_directory(target.parent)
        if target.is_symlink():
            raise SyncError("Token file must not be a symlink")
        fd, temporary = tempfile.mkstemp(prefix=".tokens-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf8") as handle:
                json.dump(self.config, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class _PrivateSourceLogFilter(logging.Filter):
    """Keep retry warnings useful without copying vendor response/error text."""

    def filter(self, record):
        record.msg = "Withings source request retrying; provider details suppressed"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


@contextmanager
def _private_source_logs():
    # This affects only this call's upstream logger. Always restore the logger,
    # including when retries exhaust, so the legacy CLI retains its own behavior.
    logger = logging.getLogger("withings")
    redaction = _PrivateSourceLogFilter()
    logger.addFilter(redaction)
    try:
        yield
    finally:
        logger.removeFilter(redaction)


class LocalProvider(WithingsOAuth2):
    """Reuse upstream OAuth; scheduled jobs never initiate interactive login."""

    def __init__(self, config_folder):
        folder = private_directory(Path(config_folder))
        token_path = folder / ".withings_user.json"
        if token_path.is_symlink() or not token_path.is_file():
            raise SyncError("Existing Withings authorization required")
        token_path.chmod(0o600)
        self.user_cfg = AtomicConfig(token_path)
        self.user_config = self.user_cfg.config
        if not all(
            self.user_config.get(key)
            for key in ("access_token", "refresh_token", "userid")
        ):
            raise SyncError("Existing Withings authorization required")
        app_path = folder / "withings_app.json"
        if not app_path.is_file():
            app_path = APP_CONFIG
        self.app_config = WithingsConfig(app_path).config
        account_before = str(self.user_config["userid"])
        self.refresh_accesstoken()
        # Save a rotated token before further work, including identity validation.
        self.user_cfg.write()
        if str(self.user_config["userid"]) != account_before:
            raise SyncError("Withings account changed during token refresh")

    @property
    def account_id(self):
        return str(self.user_config["userid"])

    def request(self, params):
        with _private_source_logs():
            return _api_request(
                GETMEAS_URL,
                {
                    **params,
                    "access_token": self.user_config["access_token"],
                },
            )


@contextmanager
def runtime_lock(runtime):
    private_directory(runtime)
    descriptor = os.open(
        runtime / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncLocked("A Withings sync is already running") from None
        yield
    finally:
        os.close(descriptor)


def _validate_group(group, category):
    if not isinstance(group, dict):
        raise SyncError("Measurement group must be an object")
    _integer(group.get("grpid"), "grpid", 0)
    _integer(group.get("date"), "date", 0)
    if "category" in group and _integer(group["category"], "category") != category:
        raise SyncError("Unexpected measurement group category")
    if group.get("modified") is not None:
        _integer(group["modified"], "modified", 0)
    measures = group.get("measures")
    if not isinstance(measures, list) or not measures:
        raise SyncError("Measurement group requires a nonempty measures array")
    for measure in measures:
        if not isinstance(measure, dict):
            raise SyncError("Measurement must be an object")
        for field in ("type", "value", "unit"):
            _integer(measure.get(field), field)


def _pages(provider, category, lastupdate):
    offset = None
    seen = set()
    for page_index in range(MAX_PAGES):
        params = {"category": category, "lastupdate": lastupdate}
        if offset is not None:
            params["offset"] = offset
        response = provider.request(params)
        if not isinstance(response, dict) or response.get("status") != 0:
            raise SyncError("Withings returned an unsuccessful response")
        body = response.get("body")
        if not isinstance(body, dict) or not isinstance(body.get("measuregrps"), list):
            raise SyncError("Withings response lacks measurement groups")
        for group in body["measuregrps"]:
            _validate_group(group, category)
        more = body.get("more", 0)
        if not isinstance(more, (int, bool)) or more not in (0, 1):
            raise SyncError("Invalid pagination flag")
        yield page_index, offset, response
        if not more:
            return
        next_offset = _integer(body.get("offset"), "offset", 0)
        if next_offset in seen or (offset is not None and next_offset <= offset):
            raise SyncError("Withings repeated or reversed pagination offset")
        seen.add(next_offset)
        offset = next_offset
    raise SyncError("Withings exceeded pagination safety limit")


def _upsert_group(connection, account, category, group):
    previous = connection.execute(
        "SELECT modified_at FROM measurement_groups WHERE account_id=? AND category=? AND group_id=?",
        (account, category, group["grpid"]),
    ).fetchone()
    modified = group.get("modified")
    if (
        previous is not None
        and previous[0] is not None
        and (modified is None or modified < previous[0])
    ):
        # An overlapping or eventually consistent page must not roll back a correction.
        return False
    connection.execute(
        "INSERT INTO measurement_groups VALUES (?,?,?,?,?,?) ON CONFLICT(account_id,category,group_id) "
        "DO UPDATE SET measured_at=excluded.measured_at,modified_at=excluded.modified_at,raw_json=excluded.raw_json",
        (account, category, group["grpid"], group["date"], modified, _json(group)),
    )
    connection.execute(
        "DELETE FROM measurements WHERE account_id=? AND category=? AND group_id=?",
        (account, category, group["grpid"]),
    )
    connection.executemany(
        "INSERT INTO measurements VALUES (?,?,?,?,?,?,?,?)",
        [
            (
                account,
                category,
                group["grpid"],
                index,
                measure["type"],
                measure["value"],
                measure["unit"],
                _json(measure),
            )
            for index, measure in enumerate(group["measures"])
        ],
    )
    return True


def sync(runtime: Path, full: bool = False, provider_factory=None):
    """Publish both categories atomically; leave the checkpoint unchanged on failure.

    The injectable factory receives runtime/config and returns account_id + request(params).
    Receipts expose only counts and a safe exception class, never provider error text.
    """
    runtime = Path(runtime)
    with runtime_lock(runtime):
        connection = connect(runtime / "withings.sqlite3")
        try:
            initialize(connection)
            started_epoch = int(time.time())
            started = _now()
            cursor = connection.execute(
                "INSERT INTO sync_runs(started_at,status,mode) VALUES (?,'running',?)",
                (started, "full" if full else "incremental"),
            )
            run_id = cursor.lastrowid
            connection.commit()
            try:
                provider = (provider_factory or LocalProvider)(runtime / "config")
                source_account = provider.account_id
                account = str(source_account)
                if source_account is None or not account or len(account) > 100:
                    raise SyncError("Missing or invalid account identifier")
                if connection.execute(
                    "SELECT 1 FROM sync_state WHERE account_id != ? LIMIT 1", (account,)
                ).fetchone():
                    raise SyncError(
                        "Withings account differs from the existing database"
                    )
                state = connection.execute(
                    "SELECT * FROM sync_state WHERE account_id=?", (account,)
                ).fetchone()
                backfill = full or state is None or not state["backfill_complete"]
                lastupdate = (
                    0
                    if backfill
                    else max(0, state["last_source_sync_at"] - OVERLAP_SECONDS)
                )
                groups = observations = responses = 0
                for category in (1, 2):
                    for page_index, offset, response in _pages(
                        provider, category, lastupdate
                    ):
                        raw = _json(response)
                        connection.execute(
                            "INSERT INTO source_responses(run_id,category,page_index,requested_lastupdate,requested_offset,response_sha256,response_json) VALUES (?,?,?,?,?,?,?)",
                            (
                                run_id,
                                category,
                                page_index,
                                lastupdate,
                                offset,
                                hashlib.sha256(raw.encode()).hexdigest(),
                                raw,
                            ),
                        )
                        # Raw evidence survives a later failed page/category. Data and
                        # checkpoints remain unpublished until every request succeeds.
                        connection.commit()
                        responses += 1
                connection.execute("BEGIN IMMEDIATE")
                for receipt in connection.execute(
                    "SELECT category,response_json FROM source_responses WHERE run_id=? ORDER BY id",
                    (run_id,),
                ):
                    for group in json.loads(receipt["response_json"])["body"][
                        "measuregrps"
                    ]:
                        if _upsert_group(
                            connection, account, receipt["category"], group
                        ):
                            groups += 1
                            observations += len(group["measures"])
                finished = _now()
                connection.execute(
                    "INSERT INTO sync_state VALUES (?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
                    "backfill_complete=excluded.backfill_complete,last_source_sync_at=excluded.last_source_sync_at,"
                    "last_success_at=excluded.last_success_at,window_end_date=excluded.window_end_date",
                    (
                        account,
                        1,
                        started_epoch,
                        finished,
                        datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat(),
                    ),
                )
                connection.execute(
                    "UPDATE sync_runs SET finished_at=?,status='success',mode=?,group_count=?,measurement_count=?,response_count=? WHERE id=?",
                    (
                        finished,
                        "full" if backfill else "incremental",
                        groups,
                        observations,
                        responses,
                        run_id,
                    ),
                )
                connection.commit()
                return {
                    "status": "success",
                    "run_id": run_id,
                    "mode": "full" if backfill else "incremental",
                    "group_count": groups,
                    "measurement_count": observations,
                    "response_count": responses,
                }
            except Exception as error:  # noqa: BLE001 - persist every failure without leaking provider text
                connection.rollback()
                connection.execute(
                    "UPDATE sync_runs SET finished_at=?,status='failed',error_code=? WHERE id=?",
                    (_now(), type(error).__name__, run_id),
                )
                connection.commit()
                raise SyncError(
                    f"Withings sync failed ({type(error).__name__}); previous data retained"
                ) from None
        finally:
            connection.close()
