from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from zont_analyzer.application.gas import GasService
from zont_analyzer.application.owner_context import OwnerContextStore
from zont_analyzer.application.publication import publish_reports
from zont_analyzer.domain import TelemetryPoint
from zont_analyzer.runtime import build_runtime


def history(tmp_path: Path):
    r = build_runtime(None, tmp_path)
    r.config.home.timezone = 'UTC'
    r.config.analysis.modulation_capability_profile = 'flame_zero_is_minimum'
    r.db.save_devices([{'id': '1', 'name': 'boiler'}])
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = []
    for minute in range(0, 12*1440+15, 10):
        timestamp = start+timedelta(minutes=minute)
        for key, value in [('s', "['fl', 'ch']"), ('rml', 0.0), ('outdoor', 0.0)]:
            points.append(TelemetryPoint(device_id='1', entity_id='outdoor' if key == 'outdoor' else 'boiler',
                                         source_type='z3k_boiler_adapter', metric_key=key, timestamp_utc=timestamp,
                                         **({"value_text": value} if isinstance(value, str) else {"value_num": value})))
    r.db.upsert_samples(points, roles={'outdoor': 'outdoor_temperature'})
    store = OwnerContextStore(r.db)
    store.update_profile('1', {'fields': {'has_gas_stove': {'value': False}}})
    a = r.analysis(no_ai=True)
    reports = [a.analyze_daily(date(2026, 1, day), use_ai=False) for day in [1, 5, 9, 10]]
    return r, store, reports


def test_historical_gas_recalibrates_outside_interval_preserves_ai_feedback_and_versions(tmp_path: Path):
    r, store, reports = history(tmp_path)
    store.update_gas(reports[0].id, {'value_m3': 100})
    store.update_gas(reports[2].id, {'value_m3': 292})  # eight days * 24 m3
    first = GasService(r.db, r.config).refresh(reports[-1])
    assert first.context['gas']['volume_m3'] is not None
    assert first.context['gas']['flame_hours'] == 24
    assert first.context['gas']['flame_pct'] == 100
    from zont_analyzer.domain import Recommendation
    first.recommendations = [Recommendation(
        id='keep-owner-decision', title='Наблюдать', category='observe_only', priority='low', confidence=.5,
        hypothesis='Недостаточно данных', suggested_manual_action='Наблюдать', expected_effect='Больше данных',
        observation_period_days=7,
    )]
    first.ai_used = True
    first.summary = 'Исходный прогноз сохранён'
    r.db.save_report(first, first.summary)
    r.db.set_recommendation_feedback('keep-owner-decision', 'rejected', 'Оставить как есть')
    feedback = r.db.recommendation('keep-owner-decision')
    r.config.pilot.reports_dir = str(tmp_path/'publish')
    publish_reports(r)
    stored = r.db.report(first.id)
    assert stored and stored.summary == first.summary
    version = stored.context['gas']['model_version']
    store.update_gas(reports[2].id, {'value_m3': 388})
    publish_reports(r)
    updated = r.db.report(first.id)
    assert updated and updated.context['gas']['model_version'] != version
    assert updated.context['gas']['ai_stale'] is True
    assert updated.context['gas']['volume_m3'] > first.context['gas']['volume_m3']
    assert updated.summary == first.summary
    assert r.db.recommendation('keep-owner-decision') == feedback
    assert r.db.get_app_meta('gas-model:'+version)
    canonical = updated.model_dump_json()
    publish_reports(r)
    assert r.db.report(first.id).model_dump_json() == canonical
    # Inserting an old reading splits calibration but cannot invent new meter rows.
    store.update_gas(reports[1].id, {'value_m3': 244})
    publish_reports(r)
    assert len(GasService(r.db, r.config).readings) == 3


def test_reset_boundary_prevents_subtraction_and_profile_alone_is_not_gas_measurement(tmp_path: Path):
    r, store, reports = history(tmp_path)
    store.update_gas(reports[0].id, {'value_m3': 100})
    store.update_gas(reports[1].id, {'value_m3': 10, 'reset': True})
    service = GasService(r.db, r.config)
    assert service.intervals() == []
    assert service.context(reports[-1].period_start, reports[-1].period_end)['volume_m3'] is None
    store.update_profile('1', {'fields': {'gas_min_m3h': {'value': 1}, 'gas_max_m3h': {'value': 2}}})
    gas = GasService(r.db, r.config).context(reports[-1].period_start, reports[-1].period_end)
    assert gas['status'] == 'extrapolated'
    assert gas['lower_m3'] <= 24 and gas['upper_m3'] >= 48


def test_missing_fl_is_not_reconstructed_from_modulation(tmp_path: Path):
    r = build_runtime(None, tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    r.db.save_devices([{'id': '1'}])
    points = [TelemetryPoint(device_id='1', entity_id='boiler', source_type='z3k_boiler_adapter',
                              metric_key='rml', timestamp_utc=start+timedelta(minutes=i), value_num=100.0)
              for i in range(60)]
    r.db.upsert_samples(points)
    gas = GasService(r.db, r.config).context(start, start+timedelta(hours=1))
    assert gas['coverage_pct'] == 0 and gas['volume_m3'] is None
    assert gas['flame_pct'] is None


def test_gas_packet_keeps_amounts_and_provenance_despite_large_calibration_history():
    from zont_analyzer.adapters.openai.provider import analysis_packet
    gas = dict(status='estimated', volume_m3=15, lower_m3=8, upper_m3=22,
               reliability_index_pct=30, model_version='v1', scope='shared_meter',
               model={'raw': 'large'*20000, 'model_kind': 'mean'},
               measured_intervals=[dict(id=f'meter:{i}', volume_m3=i, scope='whole_meter',
                                        raw='large'*10000) for i in range(100)])
    packet = analysis_packet(quality={}, metrics=[], events=[], period={'kind':'weekly'}, context={'gas':gas})
    actual = packet['control_context']['gas']
    assert actual['volume_m3'] == 15 and actual['scope'] == 'shared_meter'
    assert actual['lower_m3'] == 8 and actual['upper_m3'] == 22
    assert len(actual['measured_intervals']) == 3
    assert actual['interval_selection']['available'] == 100


def test_full_accounting_interval_is_measured_with_day_precision_not_daily_interpolation(tmp_path: Path):
    r, store, reports = history(tmp_path)
    store.update_gas(reports[0].id, {'value_m3': 100})
    store.update_gas(reports[2].id, {'value_m3': 292})
    service = GasService(r.db, r.config)
    gas = service.context(reports[0].period_start, reports[2].period_start)
    assert gas['status'] == 'measured' and gas['volume_m3'] == 192
    assert gas['scope'] == 'whole_meter' and gas['measurement_time_precision'] == 'day'
    split = gas['purpose_split']
    assert split['scope'] == 'modelled_boiler'
    assert split['total_modelled_m3'] is not None
    assert split['components']['heating']['volume_m3'] is not None
    daily = service.context(reports[1].period_start, reports[1].period_end)
    assert daily['status'] != 'measured'
    assert len(service.readings) == 2


def test_gas_publication_does_not_overwrite_concurrent_ai_revision(tmp_path: Path):
    r, _store, reports = history(tmp_path)
    original = reports[-1]
    service = GasService(r.db, r.config)
    refreshed = service.refresh(original)
    newer = original.model_copy(update={'summary': 'Новая AI-ревизия'})
    r.db.save_report(newer, newer.summary)
    assert service.persist_refresh(original, refreshed) is False
    assert r.db.report(original.id).summary == newer.summary


def test_frozen_calibration_version_does_not_depend_on_subsequent_meter_readings(tmp_path: Path):
    r, store, reports = history(tmp_path)
    store.update_gas(reports[0].id, {'value_m3':100})
    store.update_gas(reports[1].id, {'value_m3':196})
    boundary = datetime(2026, 1, 7, tzinfo=UTC)
    first = GasService(r.db, r.config).model(before=boundary)
    store.update_gas(reports[2].id, {'value_m3':300})
    second = GasService(r.db, r.config).model(before=boundary)
    assert first[0] == second[0]
    assert first[1] == second[1]


def test_new_regeneration_candidate_is_published_before_database_commit(tmp_path: Path):
    from zont_analyzer.application.publication import _publish_locked, archive_paths
    r, _store, reports = history(tmp_path)
    old = reports[-1]
    candidate = old.model_copy(update={'summary': 'Новый ответ о газе', 'generated_at': datetime.now(UTC)})
    output = tmp_path/'publish'
    output.mkdir()
    _publish_locked(r, output, datetime.now(UTC), overrides=[candidate])
    html, canonical = archive_paths(output, candidate)
    assert 'Новый ответ о газе' in html.read_text()
    assert 'Новый ответ о газе' in canonical.read_text()
    assert r.db.report(old.id).summary == old.summary


def test_persisted_gas_context_is_equal_after_json_roundtrip(tmp_path: Path):
    r, store, reports = history(tmp_path)
    store.update_gas(reports[0].id, {'value_m3': 100})
    store.update_gas(reports[2].id, {'value_m3': 292})
    service = GasService(r.db, r.config)
    report = service.refresh(reports[-1])
    r.db.save_report(report, report.summary)
    stored = r.db.report(report.id)
    assert service.refresh(stored).context == stored.context


def test_reused_ai_cannot_appear_current_after_deterministic_reanalysis(tmp_path: Path):
    r, _store, reports = history(tmp_path)
    service = GasService(r.db, r.config)
    report = service.refresh(reports[-1])
    report.ai_used = True
    for key in ['pilot_ai_reuse', 'ai_interpretation_reuse']:
        candidate = report.model_copy(deep=True)
        candidate.context[key] = {'source_generated_at': report.generated_at.isoformat()}
        assert service.refresh(candidate).context['gas']['ai_stale'] is True


def test_gas_correction_reuses_published_charts_without_raw_telemetry_rebuild(tmp_path: Path, monkeypatch):
    from zont_analyzer.reports import chart_data
    r, store, reports = history(tmp_path)
    for report in reports:
        path, digest = chart_data._cache_path(r.db, report)
        chart_data._atomic_write_json(path, {
            'schema_version': chart_data.CHART_DATA_SCHEMA_VERSION, 'report_digest': digest, 'data': {'series': {}},
        })
    store.update_gas(reports[0].id, {'value_m3': 100})
    store.update_gas(reports[2].id, {'value_m3': 292})

    def forbidden(*args, **kwargs):
        raise AssertionError('Gas-only publication must preserve observed chart packets')

    monkeypatch.setattr(chart_data, 'build_chart_data', forbidden)
    r.config.pilot.reports_dir = str(tmp_path/'publish')
    assert publish_reports(r)['reports'] == len(reports)


def test_gas_context_holds_old_setpoint_until_explicit_unknown(tmp_path: Path):
    import pytest

    r = build_runtime(None, tmp_path)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = [TelemetryPoint(device_id='1', entity_id='circuit', source_type='z3k_heating_circuit',
                             metric_key='target_temp', timestamp_utc=stamp, value_num=value)
              for stamp, value in [(start-timedelta(days=3), 22.0),
                                   (start+timedelta(hours=1), None),
                                   (start+timedelta(hours=2), 24.0)]]
    r.db.upsert_samples(points, roles={'circuit': 'target_temperature'})
    window = GasService(r.db, r.config).window(start, start+timedelta(hours=3))
    assert window['target_hours'] == pytest.approx(2)
    assert window['target_degree_hours'] == pytest.approx(46)


def test_publication_does_not_make_unchanged_ai_reuse_stale(tmp_path: Path):
    from zont_analyzer.application.reasoning_context import reuse_ai_interpretation

    runtime, _store, reports = history(tmp_path)
    original = reports[-1].model_copy(deep=True)
    original.ai_used = True
    original.context['gas']['ai_stale'] = False
    retained = reuse_ai_interpretation(original, original.model_copy(deep=True))
    assert retained.context['pilot_ai_reuse']['facts_changed'] is False
    refreshed = GasService(runtime.db, runtime.config).refresh(retained)
    assert refreshed.context['gas']['ai_stale'] is False
    assert not refreshed.context.get('gas_interpretation_stale')
