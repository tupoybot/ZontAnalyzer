"""Real SerializableRW cleanup, fenced against canonical report changes."""
import json

import pytest

from tests.unit.test_cleanup_chart_cache import _fixture
from tools.cleanup_chart_cache import apply_chart_cleanup, plan_chart_cleanup
from zont_analyzer.adapters.ydb.application import Database
from zont_analyzer.domain import Report


@pytest.mark.ydb
@pytest.mark.parametrize("conflict", [False, True])
def test_real_chart_cleanup_is_atomic_and_restartable(ydb_database, conflict):
    reports, meta, target, current, obsolete, encoded = _fixture()
    db = Database(ydb_database)
    report = Report.model_validate(json.loads(reports[0]["payload"])["report"])
    db.save_report(report, "retained rendered text")
    row = ydb_database.execute("SELECT id,payload FROM reports;")[0].rows[0]
    reports = [{"id": row.id, "payload": row.payload}]
    for item in meta:
        db.set_app_meta(item["key"], item["value"])
    plan = plan_chart_cleanup(reports, meta)
    if conflict:
        db.set_app_meta(obsolete, "changed after export")
    before = {row.key: row.value for row in ydb_database.execute("SELECT key,value FROM app_meta;")[0].rows}
    result = apply_chart_cleanup(ydb_database, plan)
    after = {row.key: row.value for row in ydb_database.execute("SELECT key,value FROM app_meta;")[0].rows}
    assert ydb_database.execute("SELECT payload FROM reports;")[0].rows[0].payload == reports[0]["payload"]
    if conflict:
        assert result["conflicted_reports"] == 1
        assert after == before
    else:
        assert result["deleted_rows"] == 2
        assert after[target] == encoded
        assert current not in after and obsolete not in after
        assert after["gas-model:fixture"] == "keep"
        assert after["chart-data:v2:unknown"] == "keep"
        assert apply_chart_cleanup(ydb_database, plan)["already_complete_reports"] == 1
