"""Private-SDK instrumentation for the disposable local YDB laboratory only.

No request content or server diagnostics are retained. Header RU are observations
of this server, not a claim about managed-service billing. Missing statistics are
unknown, including protobuf scalar fields omitted by the server.
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import Counter
from contextlib import suppress

import ydb
from ydb.connection import _RpcState

_DATA = frozenset({
    "ExecuteQuery", "ExecuteDataQuery", "StreamExecuteScanQuery", "StreamReadTable",
    "ReadRows", "BulkUpsert", "BeginTransaction", "CommitTransaction", "RollbackTransaction",
})
_CONTROL = frozenset({
    "ListEndpoints", "WhoAmI", "CreateSession", "AttachSession", "DeleteSession", "KeepAlive",
    "CreateTable", "DropTable", "AlterTable", "DescribeTable", "DescribeTableOptions",
    "CopyTable", "CopyTables", "RenameTables", "ExecuteSchemeQuery", "MakeDirectory",
    "RemoveDirectory", "ListDirectory", "DescribePath", "PrepareDataQuery", "ExplainDataQuery",
    "GetOperation",
})
_METRICS = ("compile_cpu_us", "process_cpu_us", "phase_cpu_us", "total_cpu_us",
            "read_rows", "read_bytes", "update_rows", "update_bytes", "delete_rows")


def _query_category(request):
    """Recognize schema-only YQL transiently; never retain request text.

    Mask comments and quoted values/identifiers before inspecting statements. The
    deliberately narrow grammar treats mixed schema/data work as unknown rather
    than assigning a free cost to a potentially billable statement.
    """
    try:
        text = request.query_content.text
    except AttributeError:
        return "data"
    if not isinstance(text, str):
        return "unknown"
    masked = re.sub(
        r"--[^\n]*|/\*.*?\*/|'(?:''|\\.|[^'\\])*'|\"(?:\"\"|\\.|[^\"\\])*\"|`(?:``|[^`])*`",
        lambda match: " " if match.group().startswith(("--", "/*")) else " quoted ",
        text, flags=re.DOTALL,
    )
    statements = [part.strip() for part in masked.split(";") if part.strip()]
    statements = [part for part in statements if not re.match(r"^PRAGMA\b", part, re.I)]
    schema = [bool(re.match(r"^(?:CREATE|ALTER|DROP)\s+(?:TABLE|VIEW|EXTERNAL\b)", part, re.I))
              for part in statements]
    if not any(schema):
        return "data"
    if (not all(schema) or any(re.search(r"\b(?:SELECT|INSERT|UPSERT|UPDATE|DELETE|REPLACE)\b",
                                       part, re.I) for part in statements)):
        return "unknown"
    return "control"


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _stats_mode(request):
    """Retain only the enum name, never any request content."""
    try:
        value = request.stats_mode
        return next((mode.name for mode in ydb.QueryStatsMode if mode.value == value), "unknown")
    except (AttributeError, TypeError, ValueError):
        return None


def _fields(message):
    """Inspect only populated protobuf fields; never serialize plan, AST, or names."""
    if isinstance(message, dict):
        return message
    try:
        return {field.name: value for field, value in message.ListFields()}
    except (AttributeError, TypeError, ValueError):
        return {}


def _stats_metrics(stats):
    data = _fields(stats)
    phases = [_fields(p) for p in data.get("query_phases", ())]
    accesses = [_fields(a) for p in phases for a in p.get("table_access", ())]
    metrics = dict.fromkeys(_METRICS)
    metrics["compile_cpu_us"] = _number(_fields(data.get("compilation")).get("cpu_time_us"))
    metrics["process_cpu_us"] = _number(data.get("process_cpu_time_us"))
    metrics["total_cpu_us"] = _number(data.get("total_cpu_time_us"))
    phase_cpu = [_number(p.get("cpu_time_us")) for p in phases]
    if phases and all(n is not None for n in phase_cpu):
        metrics["phase_cpu_us"] = sum(phase_cpu)
    cpu_complete = all(metrics[k] is not None for k in
                       ("compile_cpu_us", "process_cpu_us", "phase_cpu_us"))
    io_ru = 0
    io_complete = bool(accesses)
    for kind, prefix, keys in (("reads", "read", ("rows", "bytes")),
                               ("updates", "update", ("rows", "bytes")),
                               ("deletes", "delete", ("rows",))):
        rows = []
        sizes = []
        for access in accesses:
            operation = _fields(access.get(kind))
            rows.append(_number(operation.get("rows")))
            if "bytes" in keys:
                sizes.append(_number(operation.get("bytes")))
        known_rows = bool(accesses) and all(n is not None for n in rows)
        known_bytes = "bytes" not in keys or (bool(accesses) and all(n is not None for n in sizes))
        if known_rows:
            metrics[prefix + "_rows"] = sum(rows)
        if "bytes" in keys and known_bytes:
            metrics[prefix + "_bytes"] = sum(sizes)
        io_complete = io_complete and known_rows and known_bytes
        if known_rows and known_bytes:
            if kind == "deletes":
                io_ru += 2 * sum(rows)
            else:
                block, factor = (4096, 1) if kind == "reads" else (1024, 2)
                io_ru += factor * sum(max(row, math.ceil(size / block))
                                      for row, size in zip(rows, sizes, strict=True))
    estimate = None
    if cpu_complete and io_complete:
        cpu_ru = math.floor(sum(metrics[k] for k in
                                ("compile_cpu_us", "process_cpu_us", "phase_cpu_us")) / 1500)
        estimate = max(cpu_ru, io_ru)
    return metrics, cpu_complete, io_complete, estimate


def _metadata_ru(raw):
    value = None
    for name in ("initial_metadata", "trailing_metadata"):
        try:
            entries = getattr(raw, name)() or ()
            for key, candidate in entries:
                if key == "x-ydb-consumed-units":
                    # Trailers replace the initial value, even when invalid.
                    value = _number(candidate)
        except Exception:
            continue
    return value


def _safe_code(raw):
    try:
        code = raw.code()
        return "StatusCode." + code.name if hasattr(code, "name") else None
    except Exception:
        return None


def _response(record, part):
    fields = _fields(part)
    status = fields.get("status", _fields(fields.get("operation")).get("status"))
    if status is not None:
        record["status"] = int(status)
    # Table-service streams nest results; Query-service streams use top-level fields.
    containers = [fields, _fields(fields.get("result"))]
    for content in containers:
        result = _fields(content.get("result_set"))
        if "rows" in result:
            record["result_rows"] += len(result["rows"])
        for key in ("exec_stats", "query_stats"):
            if key in content:
                metrics, cpu, io, estimate = _stats_metrics(content[key])
                # Execution snapshots are cumulative. Replace, never add snapshots.
                record.update(metrics=metrics, cpu_complete=cpu, io_complete=io,
                              estimated_ru=estimate, stats_present=True,
                              stats_semantics="cumulative_snapshot")


def _refresh(record):
    reasons = []
    if not record["complete"]:
        reasons.append("rpc_not_complete")
    if record["category"] == "unknown":
        reasons.append("unknown_rpc")
    if record["complete"] and record["grpc_status"] is None:
        reasons.append("missing_grpc_status")
    if record["billed"]:
        if record["measured_ru"] is None:
            reasons.append("missing_ru_header")
        if not record["cpu_complete"]:
            reasons.append("incomplete_cpu_stats")
        if not record["io_complete"]:
            reasons.append("incomplete_io_stats")
    if record.get("unsupported_path"):
        reasons.append("unsupported_path")
    record["incomplete_reasons"] = reasons
    record["failed"] = (record["status"] not in (None, 400000)
                        or record["grpc_status"] not in (None, "StatusCode.OK")
                        or record.get("exception", False))


class ObserveRpc:
    """Observe synchronous SDK RPC attempts; restore hooks on context exit.

    Keep the context open until all clients and streams have shut down. Async/future
    SDK paths are recorded as unsupported rather than silently claiming coverage.
    This global hook is intentionally unsuitable for concurrent observer contexts.
    Set collect_stats=False to preserve request statistics modes while observing
    header RU, for example during bulk fixture setup. Missing metrics stay unknown.
    The default requests BASIC aggregate statistics, without FULL plans or ASTs.
    Set enabled=False only for the explicit observer-overhead control interval:
    calls pass through unchanged and are excluded from events and measured totals.
    Both enabled and collect_stats may change between calls within the context.
    """

    def __init__(self, collect_stats=True, enabled=True):
        self.records = []
        self.collect_stats = bool(collect_stats)
        self.enabled = bool(enabled)
        self._lock = threading.RLock()

    def __enter__(self):
        self._start = time.monotonic()
        self._old = _RpcState.__call__
        self._old_future = _RpcState.future
        owner = self

        def call(state, request, *args, **kwargs):
            if not owner.enabled:
                return owner._old(state, request, *args, **kwargs)
            category = _query_category(request) if state.rpc_name == "ExecuteQuery" else None
            record = owner._new(state.rpc_name, category)
            record["stats_mode_original"] = _stats_mode(request)
            if category == "unknown":
                record["unsupported_path"] = True
            if (owner.collect_stats and state.rpc_name == "ExecuteQuery"
                    and category == "data"):
                try:
                    request.stats_mode = ydb.QueryStatsMode.BASIC.value
                    record["stats_requested"] = True
                except (AttributeError, ValueError, TypeError):
                    record["unsupported_path"] = True
            record["stats_mode_requested"] = _stats_mode(request)
            try:
                response = owner._old(state, request, *args, **kwargs)
            except BaseException as exc:
                record["exception"] = True
                owner._finish(record, exc)
                raise
            if hasattr(response, "__next__"):
                stream = _Stream(response, record, owner)
                try:
                    if not response.add_callback(lambda: owner._finish(record, response)):
                        owner._finish(record, response)
                except Exception:
                    pass
                return stream
            owner._part(record, response)
            owner._finish(record, state.rendezvous)
            return response

        def future(state, *args, **kwargs):
            if not owner.enabled:
                return owner._old_future(state, *args, **kwargs)
            record = owner._new(state.rpc_name)
            record["unsupported_path"] = True
            try:
                raw, result = owner._old_future(state, *args, **kwargs)
            except BaseException as exc:
                record["exception"] = True
                owner._finish(record, exc)
                raise
            with suppress(Exception):
                raw.add_done_callback(lambda completed: owner._finish(record, completed))
            return raw, result

        _RpcState.__call__, _RpcState.future = call, future
        return self

    def _new(self, rpc, category=None):
        if category is None:
            category = "data" if rpc in _DATA else "control" if rpc in _CONTROL else "unknown"
        record = dict(rpc=rpc, category=category,
                      billed=category != "control", started_seconds=time.monotonic() - self._start,
                      completed_seconds=None, status=None, grpc_status=None, measured_ru=None,
                      metrics=dict.fromkeys(_METRICS), cpu_complete=False, io_complete=False,
                      estimated_ru=None, stats_present=False, result_rows=0,
                      collect_stats=self.collect_stats,
                      stats_requested=False, stats_mode_original=None,
                      stats_mode_requested=None, stats_semantics=None,
                      complete=False, failed=False, incomplete_reasons=[])
        with self._lock:
            self.records.append(record)
        return record

    def _part(self, record, part):
        with self._lock:
            try:
                _response(record, part)
            except Exception:
                record["unsupported_path"] = True
            _refresh(record)

    def _finish(self, record, raw):
        with self._lock:
            record["grpc_status"] = _safe_code(raw)
            if record["billed"]:
                record["measured_ru"] = _metadata_ru(raw)
            if record["completed_seconds"] is None:
                record["completed_seconds"] = time.monotonic() - self._start
            record["complete"] = True
            _refresh(record)

    def __exit__(self, *args):
        _RpcState.__call__, _RpcState.future = self._old, self._old_future
        with self._lock:
            for record in self.records:
                _refresh(record)

    def summary(self):
        with self._lock:
            return summarize(self.records)


class _Stream:
    def __init__(self, raw, record, owner):
        self.raw, self.record, self.owner = raw, record, owner

    def __iter__(self):
        return self

    def __next__(self):
        try:
            part = next(self.raw)
        except StopIteration:
            self.owner._finish(self.record, self.raw)
            raise
        except BaseException:
            self.record["exception"] = True
            self.owner._finish(self.record, self.raw)
            raise
        self.owner._part(self.record, part)
        return part

    def __getattr__(self, key):
        return getattr(self.raw, key)


def summarize(records):
    """Aggregate actual attempts, keeping unknown costs unknown (also in empty windows)."""
    records = list(records)
    billed = [r for r in records if r["billed"]]
    known = [r for r in billed if r["measured_ru"] is not None]
    result = {
        "rpc_count": len(records), "rpc_counts": dict(Counter(r["rpc"] for r in records)),
        "billed_rpc_count": len(billed), "control_rpc_count": len(records) - len(billed),
        "measured_ru": sum(r["measured_ru"] for r in known)
        if len(known) == len(billed) else None,
        "ru_present_rpc_count": len(known), "ru_missing_rpc_count": len(billed) - len(known),
        "failed_rpc_count": sum(r["failed"] for r in records),
        "all_rpc_complete": all(r["complete"] for r in records),
        "stats_requested_rpc_count": sum(r.get("stats_requested", False) for r in records),
        "all_billed_rpc_complete": all(r["complete"] for r in billed),
        "inflight_control_rpc_count": sum(not r["complete"] and not r["billed"]
                                          for r in records),
        # Session attachment remains open for the session's lifetime. Keep its
        # actual completion state, without withholding completed data costs.
        "measurement_complete": all(
            (r["category"] == "control" and not r["billed"] and not r["complete"])
            or (r["complete"] and r["category"] != "unknown"
                and r.get("grpc_status") is not None and not r.get("unsupported_path"))
            for r in records)
                                    and len(known) == len(billed),
        "stats_complete": all(r["cpu_complete"] and r["io_complete"] for r in billed),
        "cpu_complete": all(r["cpu_complete"] for r in billed),
        "io_complete": all(r["io_complete"] for r in billed),
        "estimated_ru": sum(r["estimated_ru"] for r in billed)
        if all(r["estimated_ru"] is not None for r in billed) else None,
        "result_rows": sum(r.get("result_rows", 0) for r in records),
    }
    result["metrics"] = {
        key: sum(r["metrics"][key] for r in billed)
        if all(r.get("metrics", {}).get(key) is not None for r in billed) else None
        for key in _METRICS
    }
    return result


def windows(records, elapsed_seconds, width_seconds):
    """Completion buckets [start,end), including idle windows and final endpoint.

    An RPC exactly at the elapsed endpoint belongs to the final bucket. All earlier
    boundaries belong to the bucket starting at that boundary. No time prorating.
    Billed attempts unfinished at scope end are placed in the final bucket with
    unattributed_rpc_count, unknown cost, and incomplete state. This placement is
    an accounting marker, not a claimed completion time. Open controls are omitted.
    """
    if (not math.isfinite(elapsed_seconds) or elapsed_seconds < 0
            or not math.isfinite(width_seconds) or width_seconds <= 0):
        raise ValueError("window duration must be finite and width positive")
    count = max(1, math.ceil(elapsed_seconds / width_seconds))
    buckets = [[] for _ in range(count)]
    unattributed = 0
    for record in records:
        completed = record.get("completed_seconds")
        if record.get("started_seconds", 0) > elapsed_seconds:
            continue
        if completed is None or completed > elapsed_seconds:
            if record["billed"]:
                unresolved = dict(record, complete=False, completed_seconds=None,
                                  measured_ru=None, estimated_ru=None,
                                  metrics=dict.fromkeys(_METRICS), cpu_complete=False,
                                  io_complete=False, stats_present=False, stats_semantics=None)
                unresolved["incomplete_reasons"] = [
                    *record.get("incomplete_reasons", ()), "unfinished_at_scope_end",
                ]
                buckets[-1].append(unresolved)
                unattributed += 1
            continue
        if completed < 0:
            continue
        index = min(int(completed // width_seconds), count - 1)
        buckets[index].append(record)
    return [dict(start_seconds=i * width_seconds,
                 end_seconds=min((i + 1) * width_seconds, elapsed_seconds),
                 unattributed_rpc_count=unattributed if i == count - 1 else 0,
                 **summarize(bucket)) for i, bucket in enumerate(buckets)]
