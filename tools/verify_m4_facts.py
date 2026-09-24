#!/usr/bin/env python3
"""Compare deterministic old-stable and YDB report facts on the same data.

Local acceptance procedure (all paths and data remain private):

1. Pin the old-stable source/image to the deployed immutable revision. Create an
   application SQLite online backup on the production host, download it, and
   make a separately writable local copy. Do no analysis on the production host.
2. In an isolated local Docker container from that pinned old-stable image,
   mount the online backup read-only and the writable copy/data and export
   directories separately. Disable external clients and run selected
   ``analyze daily --date YYYY-MM-DD --no-ai`` (plus the same weekly/monthly
   periods when present). Run ``report publish`` until ``pending_reports=0``;
   copy its exported report JSON to REFERENCE_DIR.
3. Import the same backup into a fresh local YDB namespace. Use the same config,
   timezone, owner state, periods, and ``--no-ai`` selection in the M4 image;
   run ``report publish`` until ``pending_reports=0``, and copy its canonical
   exported report JSON to CANDIDATE_DIR. Keep both exports private.
4. Run this script inside the local test image with both directories mounted
   read-only. An exit code of zero means every matched report agrees on all
   deterministic content after the explicit metadata exclusions below. Store
   ``--details`` output only in a private artifact directory (mode 0600).

The comparison ignores generated report/object IDs, generation time, storage
revision fields and backend input fingerprints. It preserves metric/event
meaning, evidence links, quality, gas context, text, recommendations, period,
and all other fields. No external API is called by this tool.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

_METADATA_KEYS = {"id", "report_id", "generated_at", "revision", "storage_revision", "source_revision"}
_TIME_KEYS = {"period_start", "period_end", "started_at", "ended_at", "timestamp_utc", "effective_from"}
_REQUIRED = {"kind", "period_start", "period_end", "context", "quality", "metrics", "events",
             "recommendations", "summary"}


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: report JSON must be an object")
    if "report" in data and isinstance(data["report"], dict):
        data = data["report"]
    missing = _REQUIRED - data.keys()
    if missing:
        raise ValueError(f"{path}: missing report fields: {', '.join(sorted(missing))}")
    if data.get("ai_used") is not False:
        raise ValueError(f"{path}: acceptance requires an explicit no-AI report")
    return data


def _utc(value: str) -> str:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"timezone-aware timestamp required: {value}")
    return moment.astimezone(UTC).isoformat()


def _legacy_intervention_utc(value: str) -> str:
    """Old SQLite intervention timestamps were naive UTC strings."""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _key(report: dict[str, Any]) -> str:
    return "|".join((str(report["kind"]), _utc(str(report["period_start"])),
                     _utc(str(report["period_end"]))))


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _normalize(value: Any, *, key: str = "", metrics: dict[str, str] | None = None,
               events: dict[str, str] | None = None, errors: list[str] | None = None,
               path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for name, child in value.items():
            if name in _METADATA_KEYS or name == "input_revision":
                continue
            if name.endswith("metric_ids") or name.endswith("event_ids"):
                lookup = metrics if name.endswith("metric_ids") else events
                if not isinstance(child, list):
                    result[name] = _normalize(child, key=name, metrics=metrics, events=events,
                                              errors=errors, path=(*path, name))
                    continue
                resolved = []
                for identifier in child:
                    meaning = lookup.get(str(identifier)) if lookup else None
                    if meaning is None:
                        if errors is not None:
                            errors.append(f"unresolved evidence reference in {name}")
                        meaning = "<unresolved evidence>"
                    resolved.append(meaning)
                result[name] = sorted(resolved)
                continue
            result[name] = _normalize(child, key=name, metrics=metrics, events=events,
                                      errors=errors, path=(*path, name))
        return result
    if isinstance(value, list):
        return [_normalize(item, key=key, metrics=metrics, events=events, errors=errors,
                           path=(*path, str(index))) for index, item in enumerate(value)]
    if (isinstance(value, str) and len(path) == 4
            and path[:2] == ("context", "intervention_history") and path[2].isdigit()
            and path[3] in {"recorded_at", "temporal_boundary"}):
        return _legacy_intervention_utc(value)
    if isinstance(value, str) and key in _TIME_KEYS:
        return _utc(value)
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return Decimal(value)
    return value


def _canonical(report: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    metric_map = {str(item["id"]): _stable_json(_normalize(item)) for item in report["metrics"] if "id" in item}
    event_map = {str(item["id"]): _stable_json(_normalize(item)) for item in report["events"] if "id" in item}
    if len(metric_map) != len([item for item in report["metrics"] if "id" in item]):
        errors.append("duplicate metric ID")
    if len(event_map) != len([item for item in report["events"] if "id" in item]):
        errors.append("duplicate event ID")
    normalized = _normalize(report, metrics=metric_map, events=event_map, errors=errors)
    normalized["metrics"] = sorted(normalized["metrics"], key=_stable_json)
    normalized["events"] = sorted(normalized["events"], key=_stable_json)
    return normalized, errors


def _load_directory(directory: Path) -> dict[str, tuple[Path, dict[str, Any]]]:
    if not directory.is_dir():
        raise ValueError(f"report directory does not exist: {directory}")
    result: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in sorted(directory.rglob("*.json")):
        if path.name == "reports.json":
            continue  # publication manifest, not a canonical report
        report = _read_json(path)
        key = _key(report)
        if key in result:
            raise ValueError(f"duplicate report period {key}: {result[key][0]} and {path}")
        result[key] = path, report
    if not result:
        raise ValueError(f"no report JSON files in {directory}")
    return result


def _differences(reference: Any, candidate: Any, path: str, output: list[dict[str, Any]],
                 *, absolute_tolerance: Decimal, relative_tolerance: Decimal) -> None:
    if isinstance(reference, dict) and isinstance(candidate, dict):
        for key in sorted(reference.keys() | candidate.keys()):
            child = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in reference or key not in candidate:
                output.append({"path": child, "reference": reference.get(key, "<missing>"),
                               "candidate": candidate.get(key, "<missing>")})
            else:
                _differences(reference[key], candidate[key], child, output,
                             absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance)
        return
    if isinstance(reference, list) and isinstance(candidate, list):
        if len(reference) != len(candidate):
            output.append({"path": path + "/length", "reference": len(reference), "candidate": len(candidate)})
        for index, (old, new) in enumerate(zip(reference, candidate, strict=False)):
            _differences(old, new, f"{path}/{index}", output,
                         absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance)
        return
    if isinstance(reference, Decimal) and isinstance(candidate, Decimal):
        margin = max(absolute_tolerance, relative_tolerance * max(abs(reference), abs(candidate)))
        if abs(reference - candidate) <= margin:
            return
    elif type(reference) is type(candidate) and reference == candidate:
        return
    output.append({"path": path or "/", "reference": reference, "candidate": candidate})


def compare(reference_dir: Path, candidate_dir: Path, *, absolute_tolerance: Decimal = Decimal("1e-9"),
            relative_tolerance: Decimal = Decimal("1e-9")) -> dict[str, Any]:
    old, new = _load_directory(reference_dir), _load_directory(candidate_dir)
    differences: list[dict[str, Any]] = []
    for key in sorted(old.keys() | new.keys()):
        if key not in old or key not in new:
            differences.append({"report": key, "path": "/report", "reference": key in old,
                                "candidate": key in new})
            continue
        old_normalized, old_errors = _canonical(old[key][1])
        new_normalized, new_errors = _canonical(new[key][1])
        for error in old_errors:
            differences.append({"report": key, "path": "/reference/evidence", "error": error})
        for error in new_errors:
            differences.append({"report": key, "path": "/candidate/evidence", "error": error})
        found: list[dict[str, Any]] = []
        _differences(old_normalized, new_normalized, "", found,
                     absolute_tolerance=absolute_tolerance, relative_tolerance=relative_tolerance)
        differences.extend({"report": key, **item} for item in found)
    return {"reference_reports": len(old), "candidate_reports": len(new),
            "matched_reports": len(old.keys() & new.keys()), "differences": differences}


def _self_test() -> None:
    base: dict[str, Any] = {"id": "old", "kind": "daily", "period_start": "2026-01-01T00:00:00Z",
            "period_end": "2026-01-02T00:00:00Z", "generated_at": "2026-01-02T01:00:00Z",
            "timezone": "UTC", "context": {"gas": {"volume_m3": 4.0}, "input_revision": {"telemetry": "old"}},
            "quality": {"score": 1.0}, "metrics": [{"id": "m-old", "name": "room", "value": 20.0, "unit": "C"},
                                          {"id": "m-other", "name": "target", "value": 21.0, "unit": "C"}],
            "events": [{"id": "e-old", "kind": "heating", "started_at": "2026-01-01T12:00:00Z",
                        "severity": "info"}],
            "recommendations": [{"id": "r-old", "title": "Observe", "priority": "low",
                                 "evidence_metric_ids": ["m-old"], "evidence_event_ids": ["e-old"]}],
            "summary": "Stable fact", "ai_used": False}
    changed = json.loads(_stable_json(base))
    changed.update(id="new", generated_at="2026-01-02T02:00:00Z")
    changed["context"]["input_revision"] = {"telemetry": "new"}
    changed["metrics"][0]["id"] = "m-new"
    changed["events"][0]["id"] = "e-new"
    changed["recommendations"][0].update(id="r-new", evidence_metric_ids=["m-new"],
                                             evidence_event_ids=["e-new"])
    base["context"]["intervention_history"] = [{
        "recorded_at": "2026-01-01 12:30:00", "temporal_boundary": "2026-01-01 12:45:00",
    }]
    changed["context"]["intervention_history"] = [{
        "recorded_at": "2026-01-01T15:30:00+03:00",
        "temporal_boundary": "2026-01-01T12:45:00+00:00",
    }]
    with tempfile.TemporaryDirectory() as temporary:
        old_dir, new_dir = Path(temporary) / "old", Path(temporary) / "new"
        old_dir.mkdir()
        new_dir.mkdir()
        (old_dir / "report.json").write_text(_stable_json(base), encoding="utf-8")
        target = new_dir / "report.json"
        target.write_text(_stable_json(changed), encoding="utf-8")
        assert compare(old_dir, new_dir)["differences"] == []
        different_time = json.loads(_stable_json(changed))
        different_time["context"]["intervention_history"][0]["recorded_at"] = "2026-01-01T12:31:00+00:00"
        target.write_text(_stable_json(different_time), encoding="utf-8")
        assert any(item["path"] == "/context/intervention_history/0/recorded_at"
                   for item in compare(old_dir, new_dir)["differences"])
        mutations: tuple[Callable[[dict[str, Any]], Any], ...] = (
            lambda item: item["context"]["gas"].update(volume_m3=5.0),
            lambda item: item["metrics"][0].update(value=19.0),
            lambda item: item["events"][0].update(severity="warning"),
            lambda item: item["recommendations"][0].update(priority="high"),
            lambda item: item["recommendations"][0].update(evidence_metric_ids=["m-other"]),
        )
        for mutate in mutations:
            case = json.loads(_stable_json(changed))
            mutate(case)
            target.write_text(_stable_json(case), encoding="utf-8")
            assert compare(old_dir, new_dir)["differences"]
    print("self-test passed: volatile IDs/times ignored; facts, gas, metrics, events and evidence retained")


def _write_private(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reference-dir", type=Path, help="Private old-stable exported report JSON directory")
    parser.add_argument("--candidate-dir", type=Path, help="Private M4/YDB exported report JSON directory")
    parser.add_argument("--details", type=Path, help="Optional private 0600 JSON file containing differing values")
    parser.add_argument("--abs-tol", type=Decimal, default=Decimal("1e-9"))
    parser.add_argument("--rel-tol", type=Decimal, default=Decimal("1e-9"))
    parser.add_argument("--self-test", action="store_true",
                        help="Run a synthetic comparator fixture without a database")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        _self_test()
        return 0
    if arguments.reference_dir is None or arguments.candidate_dir is None:
        parser.error("--reference-dir and --candidate-dir are required")
    if arguments.abs_tol < 0 or arguments.rel_tol < 0:
        parser.error("tolerances must be non-negative")
    try:
        result = compare(arguments.reference_dir, arguments.candidate_dir,
                         absolute_tolerance=arguments.abs_tol, relative_tolerance=arguments.rel_tol)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"comparison failed: {exc}", file=sys.stderr)
        return 2
    if arguments.details is not None:
        _write_private(arguments.details, result)
    print(f"matched {result['matched_reports']} report(s); "
          f"reference={result['reference_reports']}, candidate={result['candidate_reports']}; "
          f"differences={len(result['differences'])}")
    for item in result["differences"][:20]:
        print(f"  {item['report']} {item['path']}")
    if len(result["differences"]) > 20:
        print(f"  ... {len(result['differences']) - 20} more path(s)")
    return 1 if result["differences"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
