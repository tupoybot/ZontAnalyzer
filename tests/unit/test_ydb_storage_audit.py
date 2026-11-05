"""The public audit reports aggregates, with bounded reads and no database writes."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tools.ydb_storage_audit import PAGE_SIZE, inspect_storage
from zont_analyzer.adapters.ydb.schema import TABLES
from zont_analyzer.adapters.ydb.telemetry import encode


class FakeDatabase:
    path = "/private/database/application"

    def __init__(self, metadata=(), snapshots=()):
        self.metadata = list(metadata)
        self.snapshots = list(snapshots)
        self.queries = []
        self.descriptions = []
        self.driver = SimpleNamespace(table_client=self)
        self.stats = SimpleNamespace(
            rows_estimate=17, store_size=2048, modification_time=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def describe_table(self, path, *, settings):
        self.descriptions.append((path, settings))
        return SimpleNamespace(table_stats=self.stats)

    def execute(self, query, parameters, *, timeout_seconds):
        assert timeout_seconds == 20
        assert all(word not in query.upper() for word in ("UPSERT", "UPDATE", "DELETE", "COUNT(", "SUM("))
        self.queries.append((query, parameters))
        if "FROM app_meta" in query:
            rows = sorted(self.metadata, key=lambda row: row.key)
            rows = [row for row in rows if row.key > parameters.get("$after0", "")]
        else:
            assert "FROM config_snapshots" in query
            rows = sorted(self.snapshots, key=lambda row: (row.device_id, row.content_hash))
            after = (parameters.get("$after0", ""), parameters.get("$after1", ""))
            rows = [row for row in rows if (row.device_id, row.content_hash) > after]
        return [SimpleNamespace(rows=rows[:parameters["$limit"].value])]


def snapshot(config, timestamp, identity=1, device="private-device", content_hash=None):
    return SimpleNamespace(
        device_id=device, content_hash=content_hash or str(identity), id=identity, captured_at=timestamp,
        payload=encode({"id": device, "name": "Private owner", "raw": {"id": device, "name": config}}),
    )


def test_metadata_inventory_is_bounded_and_public_safe():
    private = "SECRET-PRIVATE-IDENTIFIER"
    db = FakeDatabase(metadata=[
        SimpleNamespace(key="telemetry-day:" + private, value="данные"),
        SimpleNamespace(key=private + ":token", value="secret-token"),
    ])
    result = inspect_storage(db, max_rows=10)
    assert result["complete"]
    assert len(result["tables"]) == len(TABLES)
    assert len(db.descriptions) == len(TABLES)
    assert result["tables"][0]["physical_rows"] == 17
    assert result["tables"][0]["physical_bytes"] == 2048
    groups = result["app_meta"]["prefixes"]
    assert groups["telemetry-day:"]["value_utf8_bytes"] == len("данные".encode())
    assert groups["other"]["rows"] == 1
    serialized = encode(result)
    assert private not in serialized
    assert "secret-token" not in serialized
    assert db.path not in serialized


def test_total_cap_prevents_reading_snapshots_and_marks_incomplete():
    db = FakeDatabase(metadata=[SimpleNamespace(key=str(i), value="v") for i in range(3)])
    result = inspect_storage(db, max_rows=3)
    assert not result["complete"]
    assert len(db.queries) == 1
    assert result["app_meta"]["error"] == "row_cap_reached"
    assert result["config_snapshots"]["projected_payload_bytes"] is None
    assert result["config_snapshots"]["projected_event_count"] is None


def test_keyset_pages_and_chronological_adjacent_configuration_changes():
    # Hash order deliberately differs from event order. Repeated state A after
    # B remains an event, but adjacent A -> A disappears from the projection.
    day = 86_400_000_000
    rows = [snapshot("A", day, 1, content_hash="z"),
            snapshot("A", 2 * day, 2, content_hash="y"),
            snapshot("B", 3 * day, 3, content_hash="a"),
            snapshot("A", 4 * day, 4, content_hash="b")]
    db = FakeDatabase(metadata=[SimpleNamespace(key=f"key-{i:04}", value="v")
                                for i in range(PAGE_SIZE + 1)], snapshots=rows)
    report = inspect_storage(db)["config_snapshots"]
    assert report["complete"]
    assert report["projected_event_count"] == 3
    assert report["observed_rows"] == 4
    assert report["observed_span_days"] == 3
    assert report["projected_payload_bytes_per_day"] == report["projected_payload_bytes"] / 3
    assert len(db.queries) == 3
    assert "$after0" in db.queries[1][1]
    assert all(parameters["$limit"].value <= PAGE_SIZE for _, parameters in db.queries)
    assert "Private owner" not in encode(report)
    assert "private-device" not in encode(report)


def test_unavailable_statistics_are_not_reported_as_zero():
    db = FakeDatabase()
    db.stats = None
    report = inspect_storage(db)
    assert not report["complete"]
    assert all(item["physical_rows"] is None and item["physical_bytes"] is None
               and item["error"] == "table_statistics_unavailable" for item in report["tables"])


def test_errors_do_not_leak_private_exception_messages():
    class Unavailable(FakeDatabase):
        def describe_table(self, path, *, settings):
            raise RuntimeError("private database path and token")

        def execute(self, query, parameters, *, timeout_seconds):
            raise RuntimeError("private query and parameters")

    result = inspect_storage(Unavailable())
    assert not result["complete"]
    assert "private" not in encode(result)
    assert result["app_meta"]["error"] == "RuntimeError"


def test_invalid_payload_does_not_publish_partial_projection():
    row = snapshot("A", 1)
    row.payload = "malformed private payload"
    result = inspect_storage(FakeDatabase(snapshots=[row]))["config_snapshots"]
    assert not result["complete"]
    assert result["projected_event_count"] is None
    assert "private" not in encode(result)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_budget_is_rejected_before_queries(limit):
    db = FakeDatabase()
    with pytest.raises(ValueError, match="positive integer"):
        inspect_storage(db, max_rows=limit)
    assert not db.queries and not db.descriptions


def test_snapshot_composite_key_paging_and_device_boundaries():
    rows = [snapshot("A", i + 1, i + 1, content_hash=f"hash-{i:04}")
            for i in range(PAGE_SIZE + 1)]
    rows.append(snapshot("A", 1, device="second-private-device", content_hash="first"))
    db = FakeDatabase(snapshots=rows)
    report = inspect_storage(db)["config_snapshots"]
    assert report["complete"]
    assert report["observed_rows"] == PAGE_SIZE + 2
    assert report["projected_event_count"] == 2
    assert len(db.queries) == 3
    assert "$after0" in db.queries[-1][1] and "$after1" in db.queries[-1][1]


def test_snapshot_cap_never_claims_partial_projection_is_complete():
    db = FakeDatabase(snapshots=[snapshot("A", 1), snapshot("B", 2, 2)])
    report = inspect_storage(db, max_rows=2)["config_snapshots"]
    assert not report["complete"]
    assert report["observed_rows"] == 2
    assert report["projected_event_count"] is None
    assert report["projected_payload_bytes"] is None
    assert report["projected_payload_bytes_per_day"] is None


def test_invalid_snapshot_timestamp_disables_growth_estimate():
    db = FakeDatabase(snapshots=[snapshot("A", 0), snapshot("B", 1, 2)])
    report = inspect_storage(db)["config_snapshots"]
    assert report["projected_event_count"] == 2
    assert report["observed_span_days"] is None
    assert report["projected_payload_bytes_per_day"] is None


def test_protobuf_zero_statistics_timestamp_is_unavailable():
    db = FakeDatabase()
    db.stats.modification_time = datetime(1970, 1, 1, tzinfo=UTC)
    result = inspect_storage(db)
    assert not result["complete"]
    assert all(item["statistics_timestamp"] is None
               and item["error"] == "table_statistics_partially_unavailable" for item in result["tables"])
