"""Generate the native Mac jobs. Installing them is an explicit local operation."""

import argparse
import os
import plistlib
import tempfile
from pathlib import Path


def jobs(project: Path, runtime: Path):
    python = str(project / ".venv/bin/python")
    common = {
        "WorkingDirectory": str(project),
        "EnvironmentVariables": {"TZ": "Europe/Amsterdam", "PYTHONUNBUFFERED": "1"},
        "ProcessType": "Background",
        "Umask": 63,
    }
    sync = {
        **common,
        "Label": "com.finsends.withings-sync",
        "ProgramArguments": [
            python,
            "-m",
            "withings_sync.local_cli",
            "--runtime",
            str(runtime),
            "sync",
        ],
        "StartCalendarInterval": {"Hour": 10, "Minute": 15},
        "StandardOutPath": str(runtime / "logs/sync.log"),
        "StandardErrorPath": str(runtime / "logs/sync-error.log"),
    }
    status = {
        **common,
        "Label": "com.finsends.withings-status",
        "ProgramArguments": [
            python,
            "-m",
            "withings_sync.local_cli",
            "--runtime",
            str(runtime),
            "serve",
            "--port",
            "18796",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(runtime / "logs/status.log"),
        "StandardErrorPath": str(runtime / "logs/status-error.log"),
    }
    return [sync, status]


def write_jobs(project: Path, runtime: Path, destination: Path):
    os.umask(0o077)
    destination.mkdir(parents=True, exist_ok=True)
    (runtime / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
    (runtime / "logs").chmod(0o700)
    written = []
    for job in jobs(project, runtime):
        path = destination / (job["Label"] + ".plist")
        fd, temporary = tempfile.mkstemp(dir=destination, prefix=".withings-job-")
        try:
            with os.fdopen(fd, "wb") as handle:
                plistlib.dump(job, handle)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        written.append(path)
    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    for path in write_jobs(
        args.project.resolve(), args.runtime.resolve(), args.destination.resolve()
    ):
        print(path)
