import importlib.util
import plistlib
from pathlib import Path


def test_jobs_use_private_runtime_and_one_native_schedule(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "local_jobs", Path(__file__).parents[1] / "deployment/local_jobs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project, runtime = tmp_path / "project", tmp_path / "runtime"
    paths = module.write_jobs(project, runtime, tmp_path / "agents")
    jobs = {
        plistlib.loads(path.read_bytes())["Label"]: plistlib.loads(path.read_bytes())
        for path in paths
    }
    sync = jobs["com.finsends.withings-sync"]
    assert sync["StartCalendarInterval"] == {"Hour": 10, "Minute": 15}
    assert sync["Umask"] == 63
    assert sync["ProgramArguments"][-1] == "sync"
    assert "--full" not in sync["ProgramArguments"]
    status = jobs["com.finsends.withings-status"]
    assert status["KeepAlive"] and status["RunAtLoad"]
    assert status["ProgramArguments"][-1] == "18796"
    assert (runtime / "logs").stat().st_mode & 0o777 == 0o700
    configured = module.jobs(project, runtime, "owner@example.com")
    assert "WITHINGS_TAILSCALE_USER" not in configured[0]["EnvironmentVariables"]
    assert (
        configured[1]["EnvironmentVariables"]["WITHINGS_TAILSCALE_USER"]
        == "owner@example.com"
    )
