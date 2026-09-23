"""Run locally in Docker against an anonymized window from an online SQLite backup.

This is M3 storage acceptance, not the M4 migration tool. Source identities,
free text and events are not copied. Output contains only counts and timings.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import UTC, datetime

from zont_analyzer.adapters.ydb.database import YdbConfig, YdbDatabase
from zont_analyzer.adapters.ydb.telemetry import TelemetryRepository
from zont_analyzer.domain import TelemetryPoint


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup")
    parser.add_argument("--days", type=int, default=7, choices=range(1, 8))
    args = parser.parse_args()
    source = sqlite3.connect(f"file:{args.backup}?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("invalid online backup")
    end = int(source.execute("SELECT MAX(timestamp_utc) FROM telemetry_samples").fetchone()[0]) + 1
    start = end - args.days * 86400
    db = YdbDatabase(YdbConfig.from_environment())
    try:
        db.initialize()
        repo = TelemetryRepository(db)
        repo.save_devices([{"id": "acceptance", "name": "anonymized"}])
        count = 0
        numeric_count = 0
        source_sum = 0.0
        times = []
        for left in range(start, end, 1800):
            right = min(left + 1800, end)
            rows = source.execute(
                "SELECT series_id,timestamp_utc,value_num,value_text,quality FROM telemetry_samples "
                "WHERE timestamp_utc>=? AND timestamp_utc<? ORDER BY series_id,timestamp_utc", (left, right),
            ).fetchall()
            # Retain distinctions NULL / empty / nonempty, without private text.
            points = [TelemetryPoint(
                device_id="acceptance", source_type="fixture", entity_id=f"series-{r['series_id']}",
                metric_key="value", timestamp_utc=datetime.fromtimestamp(r["timestamp_utc"], UTC),
                value_num=r["value_num"], value_text=None if r["value_text"] is None else
                ("" if r["value_text"] == "" else "anonymized"), quality=r["quality"],
            ) for r in rows]
            started = time.monotonic()
            repo.write_window(device_id="acceptance", data_type="history", start=datetime.fromtimestamp(left, UTC),
                              end=datetime.fromtimestamp(right, UTC), points=points,
                              state="complete" if points else "empty")
            times.append(time.monotonic() - started)
            count += len(points)
            source_sum += sum(p.value_num for p in points if p.value_num is not None)
            numeric_count += sum(p.value_num is not None for p in points)
        measurements = {}
        for label, begin in [("day", end - 86400), ("period", start)]:
            started = time.monotonic()
            snapshot = repo.read_period(
                "acceptance", datetime.fromtimestamp(begin, UTC), datetime.fromtimestamp(end, UTC),
            )
            measurements[label] = {"rows": len(snapshot["samples"]), "seconds": time.monotonic() - started}
        assert len(snapshot["samples"]) == count
        actual_numeric = [r["value_num"] for r in snapshot["samples"] if r["value_num"] is not None]
        assert len(actual_numeric) == numeric_count
        assert abs(sum(actual_numeric) - source_sum) < max(1, abs(source_sum)) * 1e-10
        assert repo.missing_intervals("acceptance", "history", datetime.fromtimestamp(start, UTC),
                                     datetime.fromtimestamp(end, UTC), now=datetime.now(UTC)) == []
        print(json.dumps({"samples": count, "series": len(repo.list_series()), "windows": len(times),
                          "write_seconds_total": sum(times), "write_seconds_max": max(times),
                          "reads": measurements, "numeric_count": numeric_count,
                          "payload_bytes": len(json.dumps(snapshot["samples"]).encode()), "integrity": "ok"}))
    finally:
        source.close()
        db.close()


if __name__ == "__main__":
    main()
