"""Optional per-test timings and container resource snapshot for CI measurements."""

import json
import os
from pathlib import Path

import pytest

_reports = {}
_workers = {}


def _snapshot():
    from tests.ydb_support import pool_statistics
    return {"resources": _resources(), "schema_pool": pool_statistics()}


def _number(path):
    try:
        value = Path(path).read_text(encoding="ascii").strip()
        return None if value == "max" else int(value)
    except (OSError, ValueError):
        return None


def _resources():
    quota = None
    period = None
    try:
        parts = Path("/sys/fs/cgroup/cpu.max").read_text(encoding="ascii").split()
        if len(parts) == 2:
            quota = None if parts[0] == "max" else int(parts[0])
            period = int(parts[1])
    except (OSError, ValueError):
        quota = _number("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period = _number("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        if quota is not None and quota < 0:
            quota = None
    rss_kib = None
    try:
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                rss_kib = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass
    return {
        "cpu_quota_us": quota,
        "cpu_period_us": period,
        "memory_limit_bytes": _number("/sys/fs/cgroup/memory.max")
        or _number("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        "memory_current_bytes": _number("/sys/fs/cgroup/memory.current")
        or _number("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        "memory_peak_bytes": _number("/sys/fs/cgroup/memory.peak")
        or _number("/sys/fs/cgroup/memory/memory.max_usage_in_bytes"),
        "pytest_rss_kib": rss_kib,
    }


def pytest_runtest_logreport(report):
    if report.when in ("setup", "call", "teardown"):
        _reports.setdefault(report.nodeid, {})[report.when] = {
            "duration_seconds": report.duration,
            "outcome": report.outcome,
        }
        _reports[report.nodeid]["worker"] = getattr(report, "worker_id", "main")


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    _workers[node.gateway.id] = node.workeroutput.get("ci_metrics", {"error": str(error)})


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        session.config.workeroutput["ci_metrics"] = _snapshot()
        return
    destination = os.environ.get("ZONT_PYTEST_METRICS")
    if destination:
        payload = {
            "exit_status": int(exitstatus),
            **_snapshot(),
            "workers": _workers,
            "tests": [{"nodeid": nodeid, **phases} for nodeid, phases in sorted(_reports.items())],
        }
        Path(destination).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
