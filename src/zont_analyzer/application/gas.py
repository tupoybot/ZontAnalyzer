"""Versioned local gas estimates; readings remain original auditable owner data.

Daily sufficient statistics are cached by telemetry revision, avoiding repeated raw
history sweeps. Model snapshots use app_meta's existing durable key/value storage.
Publication updates only canonical context under an optimistic guard, never feedback.
"""
from __future__ import annotations

import ast
import json
from bisect import bisect_right
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from zont_analyzer.adapters.sqlite.database import AppMetaRow, Database, ReportRow
from zont_analyzer.analytics.dhw import parse_opentherm_flags
from zont_analyzer.analytics.gas import ALGORITHM_VERSION, Exposure, GasInterval, StateSample, integrate_exposure
from zont_analyzer.application.owner_context import GasReadingRow, OwnerContextStore
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import Report

VERSION = "gas-context-v2"
EDGES = (0.0, 25.0, 50.0, 75.0, 100.0)
FRESHNESS = timedelta(minutes=15)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(value: Any) -> str:
    return sha256(_json(value).encode()).hexdigest()[:24]


def _utc(day: date, timezone: str, hour: int = 0) -> datetime:
    return datetime.combine(day, time(hour), ZoneInfo(timezone)).astimezone(UTC)


class GasService:
    def __init__(self, db: Database, config: AppConfig):
        self.db, self.config = db, config
        self.timezone = config.home.timezone
        self.series = db.list_series()
        states = [s for s in self.series if s['source_type'] == 'z3k_boiler_adapter' and s['metric_key'] == 's']
        self.state = states[0] if len(states) == 1 else None
        devices = db.list_devices()
        self.device_id = str(self.state['device_id']) if self.state else (
            str(devices[0]['id']) if len(devices) == 1 else None
        )
        profile = (OwnerContextStore(db).profile(self.device_id)
                   if self.device_id in {str(d['id']) for d in devices} else {})
        self.fields = {key: value for key, value in profile.get('fields', {}).items()
                       if key in {'gas_min_m3h', 'gas_max_m3h', 'has_gas_stove', 'boiler_model',
                                  'nominal_power_kw', 'installation_notes'}}
        with db.session() as session:
            self.readings = [dict(id=r.id, day=r.reading_day, value_m3=r.value_m3, segment=r.meter_segment,
                                  updated_at=str(r.updated_at))
                             for r in session.scalars(select(GasReadingRow).where(
                                 GasReadingRow.device_id == 'installation').order_by(GasReadingRow.reading_day))]
        self._windows: dict[tuple[datetime, datetime], dict[str, Any]] = {}
        self._models: dict[str, Any] = {}

    def _unique(self, role: str) -> dict[str, Any] | None:
        rows = [s for s in self.series if s['role'] == role and
                (self.device_id is None or str(s['device_id']) == self.device_id)]
        return rows[0] if len(rows) == 1 else None

    def window(self, start: datetime, end: datetime) -> dict[str, Any]:
        """Return additive statistics, splitting on local day boundaries for reuse."""
        key = (start, end)
        if key in self._windows:
            return self._windows[key]
        pieces = []
        cursor = start
        while cursor < end:
            day = cursor.astimezone(ZoneInfo(self.timezone)).date()
            after = min(end, _utc(day + timedelta(days=1), self.timezone))
            pieces.append(self._slice(cursor, after))
            cursor = after
        result: dict[str, Any] = {k: sum(p[k] for p in pieces) for k in (
            'minutes', 'flame_minutes', 'observed_minutes', 'unknown_modulation_minutes',
            'heating_minutes', 'dhw_minutes', 'weather_hours', 'degree_hours',
            'temperature_hours', 'target_hours', 'target_degree_hours', 'room_hours', 'room_degree_hours',
        )}
        result['bin_minutes'] = [sum(p['bin_minutes'][i] for p in pieces) for i in range(4)]
        self._windows[key] = result
        return result

    def _slice(self, start: datetime, end: datetime) -> dict[str, Any]:
        revision = self.db.period_data_revision(start - FRESHNESS, end + FRESHNESS)
        signature = _hash([VERSION, ALGORITHM_VERSION, self.state, self.series,
                           self.config.analysis.modulation_capability_profile, start, end, revision])
        cache_key = f'gas-exposure:{signature}'
        cached = self.db.get_app_meta(cache_key)
        if cached:
            return dict(json.loads(cached))
        states = (self.db.fetch_text_samples(int(self.state['id']), start - FRESHNESS, end + FRESHNESS)
                  if self.state else [])
        mods = [s for s in self.series if self.state and s['entity_id'] == self.state['entity_id']
                and s['metric_key'] in {'rml', 'modulation'}]
        modulation = self.db.fetch_samples(int(mods[0]['id']), start - FRESHNESS, end) if len(mods) == 1 else []
        mtimes = [t for t, _ in modulation]
        stimes = [t for t, _ in states]
        valid_states = []
        for _, encoded in states:
            try:
                value = ast.literal_eval(encoded)
                valid_states.append(isinstance(value, (list, tuple, set)) and
                                    all(isinstance(flag, str) for flag in value))
            except (ValueError, SyntaxError):
                valid_states.append(False)
        # Include TTL boundaries so modulation updates cannot renew a stale flame.
        cuts = sorted({start, end, *(t for t, _ in states), *(t for t, _ in modulation),
                       *(t + FRESHNESS for t, _ in states), *(t + FRESHNESS for t, _ in modulation)})
        points = []
        for moment in cuts:
            if moment < start or moment > end:
                continue
            si, mi = bisect_right(stimes, moment) - 1, bisect_right(mtimes, moment) - 1
            known = si >= 0 and valid_states[si] and moment - stimes[si] < FRESHNESS
            flags = parse_opentherm_flags(states[si][1]) if known else frozenset()
            mod = modulation[mi][1] if mi >= 0 and moment - mtimes[mi] < FRESHNESS else None
            if self.config.analysis.modulation_capability_profile == 'unknown':
                mod = None
            points.append(StateSample(moment, 'fl' in flags if known else None, mod,
                                      'dhw' in flags if known else None, 'ch' in flags if known else None))
        exposure = integrate_exposure(start, end, points)
        result = asdict(exposure)
        result['flame_minutes'] = sum(exposure.bin_minutes) + exposure.unknown_modulation_minutes
        result.update(weather_hours=0.0, degree_hours=0.0, temperature_hours=0.0,
                      target_hours=0.0, target_degree_hours=0.0, room_hours=0.0, room_degree_hours=0.0)
        for role, hours_key, integral_key in [('outdoor_temperature', 'weather_hours', 'temperature_hours'),
                                               ('target_temperature', 'target_hours', 'target_degree_hours'),
                                               ('control_indoor_temperature', 'room_hours', 'room_degree_hours')]:
            source = self._unique(role)
            samples = self.db.fetch_samples(int(source['id']), start - FRESHNESS, end + FRESHNESS) if source else []
            for index, (moment, value) in enumerate(samples):
                right = min(end, moment + FRESHNESS, samples[index+1][0] if index+1 < len(samples) else end)
                left = max(start, moment)
                if right <= left:
                    continue
                hours = (right - left).total_seconds()/3600
                result[hours_key] += hours
                result[integral_key] += hours * value
                if role == 'outdoor_temperature':
                    result['degree_hours'] += hours * max(0.0, 18.0-value)
        self.db.set_app_meta(cache_key, _json(result))
        return result

    def intervals(self, *, before: datetime | None = None) -> list[GasInterval]:
        intervals = []
        for first, last in zip(self.readings, self.readings[1:], strict=False):
            if first['segment'] != last['segment']:
                continue
            start = _utc(date.fromisoformat(first['day']), self.timezone, 12)
            end = _utc(date.fromisoformat(last['day']), self.timezone, 12)
            if before and end + timedelta(hours=12) > before:
                continue
            volume = float(last['value_m3']) - float(first['value_m3'])
            if volume < 0 or end <= start:
                continue
            w = self.window(start, end)
            # Worst observed boundary-day burner exposure, rather than pretending
            # the noon convention is a known reading instant.
            boundary = self.window(start-timedelta(hours=12), start+timedelta(hours=12))
            boundary2 = self.window(end-timedelta(hours=12), end+timedelta(hours=12))
            mean = volume/w['flame_minutes'] if w['flame_minutes'] > 0 else 0
            uncertainty = mean * (boundary['flame_minutes'] + boundary2['flame_minutes'])
            intervals.append(GasInterval(start, end, volume, tuple(w['bin_minutes']), w['flame_minutes'],
                                         w['observed_minutes'], w['observed_minutes']/w['minutes'],
                                         w['unknown_modulation_minutes'], uncertainty))
        return intervals

    def model(self, *, before: datetime | None = None) -> Any:
        from zont_analyzer.analytics.gas import fit_intervals

        intervals = self.intervals(before=before)
        model_readings = [r for r in self.readings if before is None or
                          _utc(date.fromisoformat(r['day'])+timedelta(days=1), self.timezone) <= before]
        fields = self.fields
        if before and self.device_id and fields:
            historical = OwnerContextStore(self.db).profile(self.device_id, before)['fields']
            fields = {k: v for k, v in historical.items() if k in self.fields}
        key = _hash([VERSION, ALGORITHM_VERSION, model_readings, fields, [asdict(i) for i in intervals], before])
        if key not in self._models:
            values = {k: v.get('value') for k, v in fields.items()}
            model = fit_intervals(intervals, passport_min_m3_per_hour=values.get('gas_min_m3h'),
                                  passport_max_m3_per_hour=values.get('gas_max_m3h'),
                                  has_gas_stove=values.get('has_gas_stove'))
            self._models[key] = model
            snapshot = {'version': key, 'algorithm': VERSION, 'model': asdict(model),
                        'readings': model_readings, 'profile': fields,
                        'intervals': [asdict(i) for i in intervals], 'before': before}
            self.db.set_app_meta(f'gas-model:{key}', _json(snapshot))
        return key, self._models[key], intervals

    def context(self, start: datetime, end: datetime, *, complete: bool = True) -> dict[str, Any]:
        from zont_analyzer.analytics.gas import estimate_exposure

        version, model, intervals = self.model()
        w = self.window(start, end)
        exposure = Exposure(
            minutes=w['minutes'], bin_minutes=tuple(w['bin_minutes']),
            unknown_modulation_minutes=w['unknown_modulation_minutes'],
            observed_minutes=w['observed_minutes'], heating_minutes=w['heating_minutes'],
            dhw_minutes=w['dhw_minutes'], flame_minutes=w['flame_minutes'],
        )
        estimate = estimate_exposure(exposure, model, start=start, end=end)
        bounds = estimate.uncertainty_m3
        days = (end-start).total_seconds()/86400
        result = {
            'id': f'gas:{int(start.timestamp())}:{int(end.timestamp())}:{version}',
            'epistemic_level': 'derived',
            'status': estimate.status, 'volume_m3': estimate.volume_m3,
            'lower_m3': bounds[0] if bounds else None, 'upper_m3': bounds[1] if bounds else None,
            'reliability_index_pct': estimate.reliability_index, 'coverage_pct': estimate.coverage*100,
            'model_version': version, 'algorithm_version': VERSION, 'reasons': list(estimate.reasons),
            'observed_days': days,
            'average_daily_m3': estimate.volume_m3/days if estimate.volume_m3 is not None else None,
            'average_weekly_m3': estimate.volume_m3/days*7 if estimate.volume_m3 is not None else None,
            'scope': 'boiler' if self.fields.get('has_gas_stove', {}).get('value') is False else 'shared_meter',
            'flame_hours': w['flame_minutes']/60 if w['observed_minutes'] else None,
            'observed_hours': w['observed_minutes']/60,
            'flame_pct': w['flame_minutes']/w['observed_minutes']*100 if w['observed_minutes'] else None,
            'heating_flame_hours': w['heating_minutes']/60 if w['observed_minutes'] else None,
            'dhw_flame_hours': w['dhw_minutes']/60 if w['observed_minutes'] else None,
            'purpose_unknown_flame_hours': max(0, w['flame_minutes']-w['heating_minutes']-w['dhw_minutes'])/60,
            'complete': complete, 'model': asdict(model),
            'source': 'Активность fl и исходные показания общего счётчика; время снятия известно с точностью до дня.',
            'uncertainty_method': 'Диапазон чувствительности модели; не вероятностный доверительный интервал.',
            'measured_intervals': [{**asdict(i), 'start': i.start.isoformat(), 'end': i.end.isoformat(),
                                    'scope': 'whole_meter', 'time_precision': 'day',
                                    'predicted_m3': sum(a*b for a,b in zip(
                                        i.features, model.rates_m3_per_minute, strict=True)) +
                                    i.unknown_modulation_minutes*model.mean_rate_m3_per_minute
                                    if model.mean_rate_m3_per_minute is not None else None}
                                   for i in intervals if i.start < end and i.end > start][-12:],
            'ai_stale': False,
        }
        # Match dates only as a day-precision accounting interval, never claim
        # those readings were taken at the report's midnight boundaries.
        local_start = start.astimezone(ZoneInfo(self.timezone))
        local_end = end.astimezone(ZoneInfo(self.timezone))
        candidates = [i for i in intervals if
                      local_start.date() <= i.start.astimezone(ZoneInfo(self.timezone)).date()
                      and i.end.astimezone(ZoneInfo(self.timezone)).date() <= local_end.date()]
        if (local_start.time() == time.min and local_end.time() == time.min and candidates
                and candidates[0].start.astimezone(ZoneInfo(self.timezone)).date() == local_start.date()
                and candidates[-1].end.astimezone(ZoneInfo(self.timezone)).date() == local_end.date()
                and all(a.end == b.start for a, b in zip(candidates, candidates[1:], strict=False))):
            measured = sum(i.volume_m3 for i in candidates)
            boundary_error = sum(i.boundary_uncertainty_m3 for i in candidates)
            known_boundary = all(i.flame_minutes > 0 and i.coverage >= .8 for i in candidates)
            result.update(status='measured', volume_m3=measured, scope='whole_meter',
                          lower_m3=max(0, measured-boundary_error) if known_boundary else None,
                          upper_m3=measured+boundary_error if known_boundary else None,
                          average_daily_m3=measured/days, average_weekly_m3=measured/days*7,
                          measurement_time_precision='day',
                          source=('Разность показаний общего счётчика за указанные даты; '
                                  'время снятия внутри дня неизвестно.'))
        for item in result['measured_intervals']:
            item['id'] = 'gas-meter:' + _hash([item['start'], item['end'], item['volume_m3']])
            item['residual_m3'] = (item['volume_m3']-item['predicted_m3']
                                   if item['predicted_m3'] is not None else None)
        result['observed_volume_m3'] = getattr(estimate, 'observed_volume_m3', None)
        result['unknown_minutes'] = w['minutes']-w['observed_minutes']
        # Canonical context must survive a JSON round trip without tuple/list or
        # datetime/string differences triggering writes on every publication.
        return dict(json.loads(_json(result)))

    def refresh(self, report: Report) -> Report:
        gas = self.context(report.period_start, report.period_end,
                           complete=report.context.get('period', {}).get('complete', True))
        old = report.context.get('gas')
        stale = report.ai_used and (not old or old.get('model_version') != gas['model_version'] or
                                   old.get('volume_m3') != gas['volume_m3'] or old.get('ai_stale', False))
        gas['ai_stale'] = stale
        if old and old.get('updated'):
            gas['updated'] = True
            gas['previous_model_version'] = old.get('previous_model_version')
        if old and old != gas:
            gas['updated'] = True
            gas['previous_model_version'] = old.get('previous_model_version') or old.get('model_version')
        result = report.model_copy(deep=True)
        # Stage 8 changes gas context only; existing stage 7 heat calculations remain valid.
        if result.context.get('calculation_version') == 'stage7-v1':
            result.context['calculation_version'] = 'stage8-v1'
        result.context['gas'] = gas
        result.context['gas_savings'] = self.savings(report.period_end)
        if stale:
            result.context['gas_interpretation_stale'] = True
        return result

    def persist_refresh(self, original: Report, refreshed: Report) -> bool:
        with self.db.session() as session:
            # Preserve revisions without touching recommendation lifecycle, outbox,
            # generation time, or an AI regeneration concurrently replacing the row.
            old = session.get(ReportRow, original.id)
            if old is None:
                return True
            if Report.model_validate_json(old.canonical_json) != original:
                return False
            if original.context == refreshed.context:
                return True
            revision = _hash(original.context.get('gas'))
            session.merge(AppMetaRow(key=f'gas-report-revision:{original.id}:{revision}',
                                     value=_json(original.context.get('gas'))))
            changed = session.execute(update(ReportRow).where(
                ReportRow.id == original.id, ReportRow.canonical_json == old.canonical_json,
            ).values(canonical_json=refreshed.model_dump_json()))
            return bool(getattr(changed, "rowcount", 0))

    def savings(self, end: datetime) -> dict[str, Any]:
        """Evaluate whole independent meter intervals across recorded interventions."""
        from zont_analyzer.analytics.gas_savings import GasInterval as WeatherInterval
        from zont_analyzer.analytics.gas_savings import WeatherPoint, fit_weather_baseline
        from zont_analyzer.application.gas_comparison import gas_comparison_context

        result: dict[str, Any] = {'status': 'unknown', 'comparisons': [],
                                  'reason': 'Для оценки экономии нужны показания до и после ручного изменения.'}
        intervals = self.intervals(before=end)
        interventions = self.db.intervention_history(limit=100, before=end)
        for entry in interventions[:4]:
            experiment = entry.get('experiment') or {}
            when_text = experiment.get('performed_at')
            if not when_text:
                continue
            when = datetime.fromisoformat(when_text)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            prior = [i for i in intervals if i.end + timedelta(hours=12) <= when]
            after = [i for i in intervals if i.start - timedelta(hours=12) >= when + timedelta(days=1)]
            if not prior:
                continue
            model_version, frozen_model, _ = self.model(before=when)

            def weather_interval(i: GasInterval) -> WeatherInterval:
                w = self.window(i.start, i.end)
                hours = w['weather_hours']
                # A sufficient statistic exactly preserving observed degree-hours;
                # the synthetic temperature is explicitly derived, never weather telemetry.
                weather = (WeatherPoint(i.start, 18-w['degree_hours']/hours, hours),) if hours else ()
                other = self.fields.get('has_gas_stove', {}).get('value') is not False
                return WeatherInterval(
                    i.start, i.end, i.volume_m3, weather,
                    dhw_hours=(w['dhw_minutes']/60 if w['observed_minutes']/w['minutes'] >= .9
                               and w['heating_minutes']+w['dhw_minutes'] >= .9*w['flame_minutes'] else None),
                    weather_coverage_pct=min(100.0, max(0.0, hours/(w['minutes']/60)*100)),
                    volume_uncertainty_m3=i.boundary_uncertainty_m3 + (i.volume_m3*.25 if other else 0),
                    target_c=w['target_degree_hours']/w['target_hours'] if w['target_hours'] else None,
                )

            try:
                training = [weather_interval(i) for i in prior]
                baseline = fit_weather_baseline(training, intervention_boundary=when)
                measured_after = bool(after)
                if not after:
                    from zont_analyzer.analytics.gas import estimate_exposure
                    after_start = when + timedelta(days=1)
                    after_end = min(end, after_start + (prior[-1].end-prior[-1].start))
                    if after_end <= after_start:
                        continue
                    w = self.window(after_start, after_end)
                    e = Exposure(w['minutes'], tuple(w['bin_minutes']), w['unknown_modulation_minutes'],
                                 w['observed_minutes'], w['heating_minutes'], w['dhw_minutes'], w['flame_minutes'])
                    prediction = estimate_exposure(e, frozen_model, start=after_start, end=after_end)
                    if prediction.volume_m3 is None or prediction.uncertainty_m3 is None:
                        continue
                    spread = max(prediction.volume_m3-prediction.uncertainty_m3[0],
                                 prediction.uncertainty_m3[1]-prediction.volume_m3)
                    after = [GasInterval(after_start, after_end, prediction.volume_m3, tuple(w['bin_minutes']),
                                         w['flame_minutes'], w['observed_minutes'],
                                         w['observed_minutes']/w['minutes'], w['unknown_modulation_minutes'], spread)]
                from dataclasses import replace
                after_weather = replace(weather_interval(after[0]), independent_measurement=measured_after)
                item = gas_comparison_context(baseline, training[-1], after_weather)
            except ValueError as error:
                result['reason'] = 'Недостаточно независимых сопоставимых интервалов для погодной модели.'
                result['diagnostic'] = str(error)
                continue
            item.update(id='gas-savings:' + _hash([entry['intervention_id'], model_version, after[0].end]),
                        intervention_id=entry['intervention_id'], experiment=experiment,
                        before_start=prior[-1].start.isoformat(), before_end=prior[-1].end.isoformat(),
                        after_start=after[0].start.isoformat(), after_end=after[0].end.isoformat(),
                        frozen_gas_model_version=model_version,
                        frozen_weather_model=json.loads(_json(asdict(baseline))),
                        original_prediction=json.loads(self.db.get_app_meta(
                            f"intervention-prediction:{entry['intervention_id']}") or 'null'),
                        owner_note=entry.get('owner_note'),
                        comfort={'before': self.comfort(prior[-1].start, prior[-1].end),
                                 'after': self.comfort(after[0].start, after[0].end),
                                 'subjective': 'unknown; requires owner feedback'},
                        occupancy={'status': 'unknown', 'epistemic_level': 'hypothesis',
                                   'correction_applied': False, 'owner_note': entry.get('owner_note'),
                                   'alternatives': ['ГВС', 'расписание', 'проветривание', 'погода']})
            # Any other action inside the accounting interval prevents attribution.
            other_actions = [a for a in interventions if a['intervention_id'] != entry['intervention_id']
                             and prior[-1].start <= datetime.fromisoformat(a['temporal_boundary']) <= after[0].end]
            if other_actions:
                item['confounders'].append('Между окнами есть другое ручное вмешательство.')
                item['effect_status'] = 'confounded'
            result['comparisons'].append(item)
        if result['comparisons']:
            result['status'] = 'available'
            result.pop('reason', None)
        return result

    def comfort(self, start: datetime, end: datetime) -> dict[str, Any]:
        w = self.window(start, end)
        return {'mean_room_c': w['room_degree_hours']/w['room_hours'] if w['room_hours'] else None,
                'mean_target_c': w['target_degree_hours']/w['target_hours'] if w['target_hours'] else None,
                'room_coverage_pct': 100*w['room_hours']/(w['minutes']/60),
                'target_coverage_pct': 100*w['target_hours']/(w['minutes']/60),
                'source': 'Independent time-weighted means; comfort-band durations require aligned evidence',
                'ventilation': 'hypothesis only; distinguish schedule, sensor and heating alternatives'}
