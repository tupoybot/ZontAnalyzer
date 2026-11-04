"""Exercise the SDK hook using transport fixtures, without a database or network."""

from types import SimpleNamespace

import grpc
import pytest
import ydb
from ydb._grpc.v4.protos import ydb_query_pb2, ydb_table_pb2
from ydb.connection import _RpcState

from tools.ydb_lab_observer import ObserveRpc, _stats_metrics, summarize, windows


class Transport:
    def __init__(self, initial=(), trailing=(), code=grpc.StatusCode.OK):
        self.initial, self.trailing, self.status_code = initial, trailing, code

    def initial_metadata(self):
        return self.initial

    def trailing_metadata(self):
        return self.trailing

    def code(self):
        return self.status_code


class Stream(Transport):
    def __init__(self, parts, **kwargs):
        super().__init__(**kwargs)
        self.parts = iter(parts)
        self.callback = None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            part = next(self.parts)
        except StopIteration:
            if self.callback:
                self.callback()
            raise
        # A completion callback can run before the last buffered stats are consumed.
        if self.callback:
            self.callback()
        return part

    def add_callback(self, callback):
        self.callback = callback
        return True


def install(monkeypatch, responses):
    responses = iter(responses)

    def call(state, request):
        response = next(responses)
        if isinstance(response, BaseException):
            raise response
        state.rendezvous = response if isinstance(response, Transport) else Transport()
        return response

    monkeypatch.setattr(_RpcState, "__call__", call)
    return lambda name, request=None: _RpcState.__call__(
        SimpleNamespace(rpc_name=name, rendezvous=None), request or SimpleNamespace())


def query(cpu, rows):
    response = ydb_query_pb2.ExecuteQueryResponsePart(status=400000)
    stats = response.exec_stats
    stats.compilation.cpu_time_us = 100
    stats.process_cpu_time_us = 100
    phase = stats.query_phases.add(cpu_time_us=cpu)
    access = phase.table_access.add(name="private-table-not-exported")
    access.reads.rows, access.reads.bytes = rows, rows * 64
    return response


def test_stream_final_snapshot_metadata_dedup_and_privacy(monkeypatch):
    stream = Stream([query(100, 2), query(900, 7)],
                    initial=(("x-ydb-consumed-units", "2"),),
                    trailing=(("x-ydb-consumed-units", "9"),))
    invoke = install(monkeypatch, [stream])
    request = SimpleNamespace(stats_mode=0, sql="private-sql", params={"secret": "value"})
    with ObserveRpc() as observed:
        assert list(invoke("ExecuteQuery", request))
    record = observed.records[0]
    assert record["measured_ru"] == 9
    assert record["metrics"]["phase_cpu_us"] == 900
    assert record["metrics"]["read_rows"] == 7
    assert record["cpu_complete"] is True
    assert record["io_complete"] is False
    assert record["estimated_ru"] is None
    assert record["complete"] and not record["failed"]
    assert 0 <= record["started_seconds"] <= record["completed_seconds"]
    assert "private" not in str(record) and "secret" not in str(record)
    assert observed.summary()["measured_ru"] == 9


class Failed(grpc.RpcError, Transport):
    def __init__(self):
        Transport.__init__(self, trailing=(("x-ydb-consumed-units", "3"),),
                           code=grpc.StatusCode.UNAVAILABLE)


def test_failed_attempt_and_retry_each_counted_and_hook_restored(monkeypatch):
    invoke = install(monkeypatch, [Failed(), Stream([query(10, 1)],
                     trailing=(("x-ydb-consumed-units", "2"),))])
    original = _RpcState.__call__
    with ObserveRpc() as observed:
        with pytest.raises(grpc.RpcError):
            invoke("ExecuteQuery")
        list(invoke("ExecuteQuery"))
    assert _RpcState.__call__ is original
    assert observed.summary()["rpc_count"] == 2
    assert observed.summary()["failed_rpc_count"] == 1
    assert observed.summary()["measured_ru"] == 5


def test_read_table_nested_result_rows_and_unavailable_stats(monkeypatch):
    parts = [ydb_table_pb2.ReadTableResponse(status=400000) for _ in range(2)]
    for part in parts:
        part.result.result_set.rows.add()
    invoke = install(monkeypatch, [Stream(parts, trailing=(("x-ydb-consumed-units", "4"),))])
    with ObserveRpc() as observed:
        list(invoke("StreamReadTable"))
    record = observed.records[0]
    assert record["result_rows"] == 2
    assert record["measured_ru"] == 4
    assert record["metrics"]["read_rows"] is None
    assert not record["cpu_complete"] and not record["io_complete"]
    assert record["estimated_ru"] is None


@pytest.mark.parametrize("header", [(), (("x-ydb-consumed-units", "bad"),),
                                     (("x-ydb-consumed-units", "nan"),),
                                     (("x-ydb-consumed-units", "-1"),)])
def test_missing_invalid_header_does_not_change_response(monkeypatch, header):
    response = Stream([query(10, 1)], trailing=header)
    invoke = install(monkeypatch, [response])
    with ObserveRpc() as observed:
        returned = list(invoke("ExecuteQuery"))
    assert len(returned) == 1
    assert observed.summary()["measured_ru"] is None
    assert not observed.summary()["measurement_complete"]
    assert "missing_ru_header" in observed.records[0]["incomplete_reasons"]


def test_control_rpcs_do_not_invent_billable_ru_and_unknown_is_incomplete(monkeypatch):
    invoke = install(monkeypatch, [SimpleNamespace(), SimpleNamespace()])
    with ObserveRpc() as observed:
        invoke("CreateSession")
        invoke("UnexpectedRpc")
    control, unknown = observed.records
    assert not control["billed"] and control["measured_ru"] is None
    assert summarize([control])["measured_ru"] == 0
    assert "unknown_rpc" in unknown["incomplete_reasons"]
    assert observed.summary()["measured_ru"] is None


def test_exact_estimate_requires_all_components_and_counts_each_access():
    access = {"reads": {"rows": 2, "bytes": 16},
              "updates": {"rows": 2, "bytes": 2456}, "deletes": {"rows": 0}}
    stats = {"compilation": {"cpu_time_us": 4062}, "process_cpu_time_us": 870,
             "query_phases": [{"cpu_time_us": 475, "table_access": [access]},
                              {"cpu_time_us": 514, "table_access": [access]}]}
    metrics, cpu, io, estimate = _stats_metrics(stats)
    assert cpu and io and estimate == 16
    assert metrics["update_rows"] == 4
    del stats["process_cpu_time_us"]
    assert _stats_metrics(stats)[3] is None
    assert _stats_metrics({})[0]["read_rows"] is None
    del access["updates"]["bytes"]
    assert not _stats_metrics(stats)[2]


def record(completion, ru, rpc="ExecuteQuery"):
    return dict(rpc=rpc, billed=True, category="data", measured_ru=ru,
                completed_seconds=completion, complete=True, failed=False,
                estimated_ru=None, cpu_complete=False, io_complete=False)


def test_completion_windows_boundaries_and_nonquery_idle_usage():
    records = [record(0.9, 1), record(1, 2, "CommitTransaction"),
               record(60, 3, "BeginTransaction"), record(120, 4)]
    seconds = windows(records, 120, 1)
    assert seconds[0]["measured_ru"] == 1
    assert seconds[1]["measured_ru"] == 2
    assert seconds[2]["rpc_count"] == 0 and seconds[2]["measured_ru"] == 0
    assert seconds[60]["measured_ru"] == 3
    assert seconds[-1]["measured_ru"] == 4
    minutes = windows(records, 120, 60)
    assert [w["measured_ru"] for w in minutes] == [3, 7]
    assert sum(w["rpc_count"] for w in minutes) == 4
    unresolved = windows([record(None, 8)], 1, 1)[0]
    assert unresolved["rpc_count"] == 1
    assert unresolved["measured_ru"] is None
    assert unresolved["unattributed_rpc_count"] == 1
    with pytest.raises(ValueError):
        windows([], 60, 0)


def test_future_path_is_explicitly_incomplete(monkeypatch):
    raw = Transport(trailing=(("x-ydb-consumed-units", "2"),))
    raw.add_done_callback = lambda callback: callback(raw)
    monkeypatch.setattr(_RpcState, "future", lambda *_args, **_kwargs: (raw, "result"))
    with ObserveRpc() as observed:
        result = _RpcState.future(SimpleNamespace(rpc_name="ReadRows"))
    assert result == (raw, "result")
    assert observed.summary()["measured_ru"] == 2
    assert not observed.summary()["measurement_complete"]
    assert "unsupported_path" in observed.records[0]["incomplete_reasons"]


def test_broken_metadata_is_safe_and_trailer_invalidates_initial(monkeypatch):
    stream = Stream([query(10, 1)], initial=(("x-ydb-consumed-units", "7"),),
                    trailing=(("x-ydb-consumed-units", "invalid"),))
    invoke = install(monkeypatch, [stream])
    with ObserveRpc() as observed:
        list(invoke("ExecuteQuery"))
    assert observed.records[0]["measured_ru"] is None

    stream = Stream([query(10, 1)])

    def broken():
        raise ValueError("private-diagnostic")

    stream.initial_metadata = broken
    stream.trailing_metadata = broken
    invoke = install(monkeypatch, [stream])
    with ObserveRpc() as observed:
        list(invoke("ExecuteQuery"))
    assert observed.records[0]["measured_ru"] is None
    assert "private-diagnostic" not in str(observed.records)


def test_stream_failure_keeps_failure_and_propagates_exception(monkeypatch):
    class BrokenStream(Stream):
        def __next__(self):
            raise Failed()

    invoke = install(monkeypatch, [BrokenStream([], code=grpc.StatusCode.UNAVAILABLE)])
    with ObserveRpc() as observed, pytest.raises(Failed):
        list(invoke("StreamReadTable"))
    assert observed.records[0]["failed"]
    assert observed.records[0]["complete"]
    assert observed.summary()["failed_rpc_count"] == 1


def test_unary_transaction_preserves_response_and_nested_status(monkeypatch):
    response = ydb_table_pb2.CommitTransactionResponse()
    response.operation.status = 400120
    invoke = install(monkeypatch, [response])
    with ObserveRpc() as observed:
        assert invoke("CommitTransaction") is response
    assert observed.records[0]["status"] == 400120
    assert observed.records[0]["failed"]
    assert observed.summary()["measured_ru"] is None


def test_open_session_attachment_does_not_hide_completed_data_measurement(monkeypatch):
    attachment = Stream([])
    data = Stream([query(10, 1)], trailing=(("x-ydb-consumed-units", "2"),))
    invoke = install(monkeypatch, [attachment, SimpleNamespace(), data])
    with ObserveRpc() as observed:
        invoke("AttachSession")
        invoke("GetOperation")
        list(invoke("ExecuteQuery"))
    attach, polling, _ = observed.records
    assert not attach["complete"] and attach["completed_seconds"] is None
    assert polling["category"] == "control" and not polling["billed"]
    summary = observed.summary()
    assert summary["all_rpc_complete"] is False
    assert summary["all_billed_rpc_complete"] is True
    assert summary["inflight_control_rpc_count"] == 1
    assert summary["measurement_complete"] is True
    assert summary["measured_ru"] == 2


def test_inflight_data_attempt_remains_incomplete(monkeypatch):
    invoke = install(monkeypatch, [Stream([])])
    with ObserveRpc() as observed:
        invoke("ExecuteQuery")
    assert not observed.summary()["all_billed_rpc_complete"]
    assert not observed.summary()["measurement_complete"]


@pytest.mark.parametrize("sql", [
    "CREATE TABLE `private-table` (id Utf8, PRIMARY KEY(id));",
    "PRAGMA TablePathPrefix = 'private/path'; ALTER TABLE `private-table` ADD COLUMN x Utf8;",
    "-- private comment\nPRAGMA SomeFlag = 'value'; /* select ignored */ DROP TABLE `private-table`;",
    "CREATE TABLE `private;table` (id Utf8, PRIMARY KEY(id)); ALTER TABLE `private;table` ADD COLUMN x Utf8;",
])
def test_schema_execute_query_keeps_stats_mode_and_is_control(monkeypatch, sql):
    invoke = install(monkeypatch, [Stream([ydb_query_pb2.ExecuteQueryResponsePart(status=400000)])])
    request = SimpleNamespace(stats_mode=0, query_content=SimpleNamespace(text=sql))
    with ObserveRpc() as observed:
        list(invoke("ExecuteQuery", request))
    record = observed.records[0]
    assert request.stats_mode == 0
    assert record["category"] == "control" and not record["billed"]
    assert record["measured_ru"] is None
    assert observed.summary()["measured_ru"] == 0
    assert observed.summary()["measurement_complete"]
    assert "private" not in str(observed.records)


@pytest.mark.parametrize("sql", [
    "CREATE TABLE `private-table` (id Utf8, PRIMARY KEY(id)); UPSERT INTO `private-table` (id) VALUES ('x');",
    "CREATE TABLE `private-table` AS SELECT id FROM `private-source`;",
    "DECLARE $id AS Utf8; ALTER TABLE `private-table` ADD COLUMN x Utf8;",
])
def test_mixed_schema_query_is_unknown_and_never_assumed_free(monkeypatch, sql):
    invoke = install(monkeypatch, [Stream([ydb_query_pb2.ExecuteQueryResponsePart(status=400000)])])
    request = SimpleNamespace(stats_mode=0, query_content=SimpleNamespace(text=sql))
    with ObserveRpc() as observed:
        list(invoke("ExecuteQuery", request))
    record = observed.records[0]
    assert request.stats_mode == 0
    assert record["category"] == "unknown" and record["billed"]
    assert observed.summary()["measured_ru"] is None
    assert not observed.summary()["measurement_complete"]
    assert "private" not in str(observed.records)


def test_header_only_mode_preserves_stats_mode_and_does_not_invent_estimate(monkeypatch):
    response = Stream([ydb_query_pb2.ExecuteQueryResponsePart(status=400000)],
                      trailing=(("x-ydb-consumed-units", "7"),))
    invoke = install(monkeypatch, [response])
    request = SimpleNamespace(stats_mode=10, query_content=SimpleNamespace(
        text="UPSERT INTO `private-table` (id) VALUES ('private');"))
    with ObserveRpc(collect_stats=False) as observed:
        list(invoke("ExecuteQuery", request))
    assert request.stats_mode == 10
    assert observed.records[0]["collect_stats"] is False
    assert observed.summary()["stats_requested_rpc_count"] == 0
    assert observed.summary()["measured_ru"] == 7
    assert observed.summary()["measurement_complete"]
    assert not observed.summary()["stats_complete"]
    assert observed.summary()["estimated_ru"] is None
    assert all(value is None for value in observed.summary()["metrics"].values())


def test_basic_stats_remains_default(monkeypatch):
    invoke = install(monkeypatch, [Stream([query(10, 1)],
                      trailing=(("x-ydb-consumed-units", "1"),))])
    request = SimpleNamespace(stats_mode=0)
    with ObserveRpc() as observed:
        list(invoke("ExecuteQuery", request))
    assert request.stats_mode == ydb.QueryStatsMode.BASIC.value
    assert observed.records[0]["collect_stats"] is True
    assert observed.records[0]["stats_mode_requested"] == "BASIC"
    assert observed.records[0]["stats_semantics"] == "cumulative_snapshot"
    assert observed.summary()["stats_requested_rpc_count"] == 1


def test_stats_collection_mode_can_change_between_calls(monkeypatch):
    responses = [Stream([query(10, 1)], trailing=(("x-ydb-consumed-units", "1"),))
                 for _ in range(2)]
    invoke = install(monkeypatch, responses)
    first = SimpleNamespace(stats_mode=ydb.QueryStatsMode.NONE.value)
    second = SimpleNamespace(stats_mode=ydb.QueryStatsMode.FULL.value)
    with ObserveRpc() as observed:
        list(invoke("ExecuteQuery", first))
        observed.collect_stats = False
        list(invoke("ExecuteQuery", second))
    assert first.stats_mode == ydb.QueryStatsMode.BASIC.value
    assert second.stats_mode == ydb.QueryStatsMode.FULL.value
    assert observed.records[0]["stats_mode_requested"] == "BASIC"
    assert observed.records[1]["stats_mode_original"] == "FULL"
    assert observed.records[1]["stats_mode_requested"] == "FULL"
    assert observed.summary()["stats_requested_rpc_count"] == 1


def test_disabled_observer_passes_through_and_restores_hooks(monkeypatch):
    raw = Stream([query(10, 1)])
    invoke = install(monkeypatch, [raw, Stream([query(10, 1)])])
    old_call, old_future = _RpcState.__call__, _RpcState.future
    request = SimpleNamespace(stats_mode=ydb.QueryStatsMode.FULL.value)
    with ObserveRpc(enabled=False) as observed:
        assert invoke("ExecuteQuery", request) is raw
        assert request.stats_mode == ydb.QueryStatsMode.FULL.value
        assert observed.records == []
        observed.enabled = True
        list(invoke("ExecuteQuery"))
        assert len(observed.records) == 1
        observed.enabled = False
    assert _RpcState.__call__ is old_call
    assert _RpcState.future is old_future


def test_disabled_future_passes_through_without_callback_or_events(monkeypatch):
    raw, result = object(), object()
    expected = (raw, result)
    monkeypatch.setattr(_RpcState, "future", lambda *_args, **_kwargs: expected)
    old_future = _RpcState.future
    with ObserveRpc(enabled=False) as observed:
        assert _RpcState.future(SimpleNamespace(rpc_name="ReadRows")) is expected
        assert observed.records == []
    assert _RpcState.future is old_future


def test_unfinished_windows_remain_unknown_without_mutating_records():
    missing = dict(record(None, None), started_seconds=5, complete=False)
    later = dict(record(90, 7), started_seconds=15)
    future_start = dict(record(90, 9), started_seconds=80)
    attachment = dict(record(None, None, "AttachSession"),
                      started_seconds=0, complete=False, billed=False, category="control")
    result = windows([missing, later, future_start, attachment], 60, 30)
    assert result[0]["rpc_count"] == 0
    assert result[0]["measured_ru"] == 0
    assert result[-1]["rpc_count"] == 2
    assert result[-1]["unattributed_rpc_count"] == 2
    assert result[-1]["measured_ru"] is None
    assert not result[-1]["all_billed_rpc_complete"]
    assert not result[-1]["measurement_complete"]
    assert later["complete"] and later["measured_ru"] == 7
    assert later["completed_seconds"] == 90
