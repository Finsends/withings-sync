# Local Withings history

This fork adds a local measurement archive to the established upstream client.
Authentication remains upstream OAuth. No custom login page, Garmin upload,
paid subscription, cloud database, or public measurement endpoint is involved.

## Storage and ingestion

SQLite fits a single person's scale history. It follows the creation-time
conventions in `Finsends/personal-trackers/skeleton/backend/tracker_base/db.py`:
private database files, WAL, foreign keys, bounded busy waits, dictionary-like
rows, and explicit commit/rollback. There is no runtime dependency on another
project. Durability uses `synchronous=FULL` for ingestion receipts.

The database is `~/.local/share/withings-sync/withings.sqlite3`, outside Git.
It owns `measurement_groups`, `measurements`, `source_responses`, `sync_runs`,
and `sync_state`. Raw responses are stored with SHA-256 hashes, request bounds,
and their retrieval run. Individual measures retain the original integer value,
unit exponent, type ID, array position, and JSON. Unknown types remain available.
`value * 10**unit` reconstructs the source value; do not round for storage.
Metric labels follow the official [Withings Body Scan data-point catalog](https://developer.withings.com/developer-guide/v3/integration-guide/surveys/data-api/all-available-health-data-body-scan/).
Unverified IDs keep their numeric label. Future-dated goals appear as a separate
current inventory count rather than extending historical measurement coverage.

An initial run requests all `getmeas` history with `lastupdate=0`, without a
metric filter, for real measurements (category 1) and objectives (category 2).
Every page is exhausted. Subsequent runs use the previous successful fetch start
with a seven-day overlap. This catches edits and backdated uploads because
`lastupdate` filters modification time rather than measurement date. Pagination
failure, malformed data, account changes, or token failures cannot advance the
checkpoint. Failed partial runs retain already received validated raw pages. Retrying
restarts the uncommitted fetch window safely. Corrections replace child rows;
older modifications cannot overwrite newer ones. Source deletions are not
inferred from absence in an incremental response.

This connection grants `user.metrics`. The archive covers all measurement types
returned by that API, including scale and blood-pressure records when present.
Sleep, workouts, and activity APIs require separate scopes and are outside this
connection. Complete API history does not prove data predating the source account
or records the provider no longer returns.

## Run

```sh
./local-sync             # initial backfill or incremental catch-up, then snapshot
./local-sync --full      # replay all available measurement history
.venv/bin/python -m withings_sync.local_cli verify
```

Only one collector may run at a time. The process lock covers token rotation and
publication. Scheduled jobs never prompt for a login. Existing tokens live under
the private runtime's `config/` directory. Rotation uses an atomic file replace.
The old upstream CLI does not share this lock: do not run it against these tokens
while the local workflow runs.

Every successful command creates a timestamped SQLite online backup in
`snapshots/`, checks full integrity and foreign keys, and atomically publishes it.
Snapshots remain on the same Mac; they are recovery copies, not an independent
disaster backup. No automatic retention deletes history or snapshots.

## Availability dashboard and health

The existing Application Health page `/apps/withings-sync` shows measurement
groups, observations, unmapped types, dates, and counts for each metric type.
It reuses the Garmin coverage template. Health checks cover live-sync freshness,
coverage, service identity, SQLite integrity, and the scheduled recovery copy.
Empty measurement days are valid, not failures. The calendar ends at the last
successful sync date. Before the next daily job, unqueried dates remain outside
the calendar and Health shows that current coverage is not yet confirmed.
No measurement values leave the app-owned database.

The app-owned API binds only `127.0.0.1:18796` (reserved in `PORTS.md`) and serves
`GET /api/coverage`. It reads one consistent database snapshot on each request,
returns count-only evidence, rejects unexpected Host headers, and has no CORS,
write, authentication, or token endpoint. Access through the existing private
Health dashboard; never expose this status port publicly.

## Native Mac jobs

`deployment/local_jobs.py` generates `com.finsends.withings-sync` and
`com.finsends.withings-status`. The sync runs daily at 10:15 in the Mac's local
calendar; installation requires its timezone to be Europe/Amsterdam. `TZ` also
sets application date interpretation. launchd catches missed calendar events
after sleep, but cannot run while the Mac is shut down. Status starts at login.
Both jobs use private logs and require no Codex session or AI usage.

```sh
.venv/bin/python deployment/local_jobs.py \
  --project "$PWD" --runtime "$HOME/.local/share/withings-sync" \
  --destination "$HOME/Library/LaunchAgents"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.finsends.withings-sync.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.finsends.withings-status.plist"
```

To restore, stop the sync job, retain the current database and its WAL files,
verify the selected snapshot's integrity and raw-response hashes, then restore
via SQLite's backup API. Never overwrite a live database while a connection is
open. Restoration replaces live data and requires owner approval.

The fork and installed code survive upstream repository deletion. OAuth still
depends on the upstream registered application; its owner can revoke it. A
separate free Withings app registration removes that remaining dependency and
is supported by `config/withings_app.json` without replacing this client.
