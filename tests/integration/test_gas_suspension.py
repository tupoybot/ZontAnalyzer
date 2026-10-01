"""Production-default suspension keeps inputs and publication without derived gas I/O."""
from datetime import timedelta

import pytest

from tests.unit.test_incremental_publication import _daily_reports, _record_change, _runtime
from zont_analyzer.adapters.ydb.publication import PublicationRepository
from zont_analyzer.application import publication
from zont_analyzer.application.gas import GasService
from zont_analyzer.application.gas_feature import gas_analysis_enabled
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.domain import TelemetryPoint
from zont_analyzer.reports.renderers import render_html


def test_gas_is_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.delenv('ZONT_GAS_ANALYSIS_ENABLED')
    assert not gas_analysis_enabled()
    with pytest.raises(RuntimeError, match='temporarily disabled'):
        GasService(None, None)  # Must fail before touching any database/configuration.


@pytest.mark.ydb
def test_suspended_analysis_reading_input_and_publication(tmp_path, monkeypatch):
    monkeypatch.delenv('ZONT_GAS_ANALYSIS_ENABLED')
    runtime = _runtime(tmp_path)
    monkeypatch.setattr(GasService, '__init__', lambda *a, **k: pytest.fail('gas constructed'))
    reports = _daily_reports(runtime, 3)
    assert all('gas' not in r.context and 'gas_savings' not in r.context for r in reports)
    owner = OwnerContextStore(runtime.db)
    saved = owner.update_gas(reports[0].id, {'value_m3': '100'})
    assert saved['reading']['value_m3'] == '100'
    publication.publish_reports(runtime)
    repository = PublicationRepository(runtime.db.storage)
    canonical = [runtime.db.report(r.id).model_dump_json() for r in reports]

    def forbidden(*args, **kwargs):
        pytest.fail('publication read historical telemetry or calibration')

    monkeypatch.setattr(runtime.db, '_samples', forbidden)
    monkeypatch.setattr(runtime.db, 'telemetry_day_revisions', forbidden)
    monkeypatch.setattr(PublicationRepository, 'reading_span', forbidden)
    at = reports[0].period_start + timedelta(hours=12)
    runtime.db.telemetry.write_window(
        device_id='device', data_type='history', start=at, end=at + timedelta(hours=1),
        points=[TelemetryPoint(device_id='device', entity_id='boiler', source_type='z3k_boiler_adapter',
                               metric_key='s', timestamp_utc=at, value_text="['fl', 'ch']")],
    )
    owner.update_gas(reports[1].id, {'value_m3': '125'})
    _record_change(runtime.db, 'global', 'gas')
    _record_change(runtime.db, 'tariff', '2026-08-01T00:00:00+00:00')
    result = publication.publish_reports(runtime)
    assert result['rendered_reports'] == result['pending_reports'] == 0
    assert int(repository.load_meta()['checkpoint']) == runtime.db.source_revision()
    assert publication.publish_reports(runtime)['rendered_reports'] == 0
    assert [runtime.db.report(r.id).model_dump_json() for r in reports] == canonical
    assert owner.gas(reports[0].id)['reading']['value_m3'] == '100'
    assert owner.gas(reports[1].id)['reading']['value_m3'] == '125'
    html = render_html(reports[0], owner_data={'gas': saved})
    assert 'Расчёт расхода и стоимости газа временно отключён' in html
    assert 'data-gas-save' in html
    assert '<div class="owner-tariffs">' not in html


@pytest.mark.ydb
def test_existing_gas_backlog_drains_without_changing_canonical_ai(tmp_path, monkeypatch):
    runtime = _runtime(tmp_path)
    reports = _daily_reports(runtime, 2)
    reports[0].ai_used = True
    reports[0].summary = 'Сохранённый ответ'
    runtime.db.save_report(reports[0], reports[0].summary)
    publication.publish_reports(runtime)
    canonical = [runtime.db.report(r.id).model_dump_json() for r in reports]
    runtime.db.storage.execute('UPDATE publication_items SET dirty=3, queued_at=1;')
    monkeypatch.delenv('ZONT_GAS_ANALYSIS_ENABLED')
    monkeypatch.setattr(GasService, '__init__', lambda *a, **k: pytest.fail('gas constructed'))
    assert publication.publish_reports(runtime, batch_size=1)['pending_reports'] == 1
    assert publication.publish_reports(runtime, batch_size=1)['pending_reports'] == 0
    assert [runtime.db.report(r.id).model_dump_json() for r in reports] == canonical
    # New long reports still complete without restoring gas calculation.
    long_report = runtime.analysis(no_ai=True).analyze_week(2026, 32, use_ai=False)
    assert 'gas' not in long_report.context
    assert 'gas_savings' not in long_report.context
