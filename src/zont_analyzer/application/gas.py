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
from zont_analyzer.analytics.gas import (
    ALGORITHM_VERSION,
    Exposure,
    GasInterval,
    GasModel,
    StateSample,
    estimate_gas_purpose_split,
    integrate_exposure,
)
from zont_analyzer.analytics.series_semantics import is_setpoint_series
from zont_analyzer.application.owner_context import GasReadingRow, OwnerContextStore
from zont_analyzer.config import AppConfig
from zont_analyzer.domain import Report

VERSION = "gas-context-v3"
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
        from zont_analyzer.application.timezone import apply_device_timezone

        apply_device_timezone(db, config)
        self.timezone = config.home.effective_timezone
        self.timezone_provenance = config.home.timezone_provenance
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
        self._cost_slices: dict[
            tuple[datetime, datetime, str],
            tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]],
        ] = {}
        from zont_analyzer.application.gas_tariffs import GasTariffStore

        self.tariffs = GasTariffStore(db, self.timezone).history(scope='installation')

    def _unique(self, role: str) -> dict[str, Any] | None:
        rows = [s for s in self.series if s['role'] == role and
                (self.device_id is None or str(s['device_id']) == self.device_id)]
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    def _exposure(window: dict[str, Any]) -> Exposure:
        return Exposure(
            minutes=window['minutes'], bin_minutes=tuple(window['bin_minutes']),
            unknown_modulation_minutes=window['unknown_modulation_minutes'],
            observed_minutes=window['observed_minutes'], heating_minutes=window['heating_minutes'],
            dhw_minutes=window['dhw_minutes'], flame_minutes=window['flame_minutes'],
            ambiguous_purpose_minutes=window['ambiguous_purpose_minutes'],
            heating_bin_minutes=tuple(window['heating_bin_minutes']),
            dhw_bin_minutes=tuple(window['dhw_bin_minutes']),
            ambiguous_purpose_bin_minutes=tuple(window['ambiguous_purpose_bin_minutes']),
            heating_unknown_modulation_minutes=window['heating_unknown_modulation_minutes'],
            dhw_unknown_modulation_minutes=window['dhw_unknown_modulation_minutes'],
            ambiguous_purpose_unknown_modulation_minutes=window['ambiguous_purpose_unknown_modulation_minutes'],
        )

    def _month_ranges(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        zone = ZoneInfo(self.timezone)
        cursor = start
        result: list[tuple[datetime, datetime]] = []
        while cursor < end:
            local = cursor.astimezone(zone)
            if local.month == 12:
                boundary = datetime(local.year + 1, 1, 1, tzinfo=zone)
            else:
                boundary = datetime(local.year, local.month + 1, 1, tzinfo=zone)
            after = min(end, boundary.astimezone(UTC))
            result.append((cursor, after))
            cursor = after
        return result

    def _monthly_cost_slices(
        self, start: datetime, end: datetime, model: GasModel,
    ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        from zont_analyzer.analytics.gas import estimate_exposure

        model_key = _hash(asdict(model))
        key = (start, end, model_key)
        if key in self._cost_slices:
            return self._cost_slices[key]
        total: list[dict[str, Any]] = []
        components: dict[str, list[dict[str, Any]]] = {
            'heating': [], 'dhw': [], 'purpose_unknown': [],
            'total_modelled': [], 'unallocated': [],
        }
        for left, right in self._month_ranges(start, end):
            exposure = self._exposure(self.window(left, right))
            estimate = estimate_exposure(exposure, model, start=left, end=right)
            allocation = estimate_gas_purpose_split(exposure, model, estimate)
            total.append({
                'start': left, 'end': right, 'volume_m3': estimate.volume_m3,
                'allocation': 'existing_gas_model_calendar_month',
            })
            split = allocation.get('components', {})
            for name in components:
                if name == 'total_modelled':
                    component_volume = allocation.get('total_modelled_m3')
                elif name == 'unallocated':
                    component_volume = allocation.get('unallocated_m3')
                else:
                    component = split.get(name, {}) if isinstance(split, dict) else {}
                    component_volume = component.get('volume_m3') if isinstance(component, dict) else None
                components[name].append({
                    'start': left, 'end': right,
                    'volume_m3': component_volume,
                    'allocation': 'existing_gas_purpose_model_calendar_month',
                })
        self._cost_slices[key] = (total, components)
        return total, components

    def _cost_for_volume(
        self,
        start: datetime,
        end: datetime,
        volume_m3: Any,
        model: GasModel | None,
        *,
        purpose: str | None = None,
        can_allocate: bool = True,
    ) -> dict[str, Any]:
        from zont_analyzer.application.gas_cost import calculate_gas_cost

        initial = calculate_gas_cost(start, end, volume_m3, self.tariffs, timezone=self.timezone)
        if (
            'volume_distribution_unavailable' not in initial.get('limitations', ())
            or model is None
            or not can_allocate
        ):
            return initial
        total, components = self._monthly_cost_slices(start, end, model)
        raw_slices = components.get(purpose, []) if purpose else total
        expected = float(volume_m3) if isinstance(volume_m3, (int, float)) else None
        numeric_volumes: list[float] = []
        for item in raw_slices:
            value = item.get('volume_m3')
            if not isinstance(value, (int, float)) or value < 0:
                return initial
            numeric_volumes.append(float(value))
        if expected is None:
            return initial
        model_total = sum(numeric_volumes)
        if model_total <= 0:
            if expected != 0:
                return initial
            factor = 0.0
        else:
            factor = expected / model_total
        slices = [
            {**item, 'volume_m3': float(item['volume_m3']) * factor,
             'allocation': item['allocation'] + '_weight_reconciled_to_period_volume'}
            for item in raw_slices
        ]
        return calculate_gas_cost(
            start, end, volume_m3, self.tariffs, timezone=self.timezone, volume_slices=slices,
        )

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
            'heating_minutes', 'dhw_minutes', 'ambiguous_purpose_minutes',
            'heating_unknown_modulation_minutes', 'dhw_unknown_modulation_minutes',
            'ambiguous_purpose_unknown_modulation_minutes', 'weather_hours', 'degree_hours',
            'temperature_hours', 'target_hours', 'target_degree_hours', 'room_hours', 'room_degree_hours',
        )}
        result['bin_minutes'] = [sum(p['bin_minutes'][i] for p in pieces) for i in range(4)]
        for field in ('heating_bin_minutes', 'dhw_bin_minutes', 'ambiguous_purpose_bin_minutes'):
            result[field] = [sum(p[field][i] for p in pieces) for i in range(4)]
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
            held = bool(source and is_setpoint_series(source['source_type'], source['metric_key']))
            samples: list[tuple[datetime, float | None]] = []
            if source:
                samples = self.db.fetch_numeric_observations(
                    int(source['id']), start if held else start - FRESHNESS, end + FRESHNESS,
                    include_previous=held,
                )
            for index, (moment, value) in enumerate(samples):
                if value is None:
                    continue
                limit = end if held else moment + FRESHNESS
                right = min(end, limit, samples[index+1][0] if index+1 < len(samples) else end)
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
        exposure = self._exposure(w)
        estimate = estimate_exposure(exposure, model, start=start, end=end)
        purpose_split = estimate_gas_purpose_split(exposure, model, estimate)
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
            'timezone_provenance': self.timezone_provenance,
            'average_daily_m3': estimate.volume_m3/days if estimate.volume_m3 is not None else None,
            'average_weekly_m3': estimate.volume_m3/days*7 if estimate.volume_m3 is not None else None,
            'scope': 'boiler' if self.fields.get('has_gas_stove', {}).get('value') is False else 'shared_meter',
            'flame_hours': w['flame_minutes']/60 if w['observed_minutes'] else None,
            'observed_hours': w['observed_minutes']/60,
            'heating_flame_hours': w['heating_minutes']/60 if w['observed_minutes'] else None,
            'dhw_flame_hours': w['dhw_minutes']/60 if w['observed_minutes'] else None,
            'purpose_unknown_flame_hours': max(0, w['flame_minutes']-w['heating_minutes']-w['dhw_minutes'])/60,
            'purpose_split': purpose_split,
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
        from zont_analyzer.analytics.burner_usage import burner_usage

        result.update(burner_usage(result, (end-start).total_seconds()/3600))
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
        whole_meter_allocation = not (
            result.get('scope') == 'whole_meter'
            and self.fields.get('has_gas_stove', {}).get('value') is not False
        )
        result['cost'] = self._cost_for_volume(
            start, end, result.get('volume_m3'), model, can_allocate=whole_meter_allocation,
        )
        from zont_analyzer.application.gas_cost import scale_cost

        result['average_daily_cost'] = scale_cost(
            result['cost'], 1 / days, basis='average_daily_from_period_cost',
        )
        result['average_weekly_cost'] = scale_cost(
            result['cost'], 7 / days, basis='average_weekly_from_period_cost',
        )
        purpose = result.get('purpose_split', {})
        components = purpose.get('components', {}) if isinstance(purpose, dict) else {}
        if isinstance(components, dict):
            for name, component in components.items():
                if isinstance(component, dict):
                    component['cost'] = self._cost_for_volume(
                        start, end, component.get('volume_m3'), model, purpose=name,
                    )
        if isinstance(purpose, dict):
            purpose['cost'] = self._cost_for_volume(
                start, end, purpose.get('total_modelled_m3'), model, purpose='total_modelled',
            )
            purpose['unallocated_cost'] = self._cost_for_volume(
                start, end, purpose.get('unallocated_m3'), model, purpose='unallocated',
            )
        for interval in result.get('measured_intervals', []):
            if isinstance(interval, dict):
                interval['cost'] = self._cost_for_volume(
                    datetime.fromisoformat(interval['start']), datetime.fromisoformat(interval['end']),
                    interval.get('volume_m3'), model, can_allocate=whole_meter_allocation,
                )
        # Canonical context must survive a JSON round trip without tuple/list or
        # datetime/string differences triggering writes on every publication.
        return dict(json.loads(_json(result)))

    @staticmethod
    def _model_from_context(gas: dict[str, Any]) -> GasModel | None:
        payload = gas.get('model')
        if not isinstance(payload, dict):
            return None
        values = dict(payload)
        calibration_end = values.get('calibration_end')
        if isinstance(calibration_end, str):
            values['calibration_end'] = datetime.fromisoformat(calibration_end)
        try:
            return GasModel(**values)
        except (TypeError, ValueError):
            return None

    def _refresh_gas_costs(self, gas: dict[str, Any], start: datetime, end: datetime) -> None:
        model = self._model_from_context(gas)
        can_allocate = not (
            gas.get('scope') == 'whole_meter'
            and self.fields.get('has_gas_stove', {}).get('value') is not False
        )
        gas['cost'] = self._cost_for_volume(
            start, end, gas.get('volume_m3'), model, can_allocate=can_allocate,
        )
        days = (end - start).total_seconds() / 86400
        from zont_analyzer.application.gas_cost import scale_cost

        gas['average_daily_cost'] = scale_cost(
            gas['cost'], 1 / days, basis='average_daily_from_period_cost',
        )
        gas['average_weekly_cost'] = scale_cost(
            gas['cost'], 7 / days, basis='average_weekly_from_period_cost',
        )
        purpose = gas.get('purpose_split')
        if isinstance(purpose, dict):
            components = purpose.get('components')
            if isinstance(components, dict):
                for name, component in components.items():
                    if isinstance(component, dict):
                        component['cost'] = self._cost_for_volume(
                            start, end, component.get('volume_m3'), model, purpose=name,
                        )
            purpose['cost'] = self._cost_for_volume(
                start, end, purpose.get('total_modelled_m3'), model, purpose='total_modelled',
            )
            purpose['unallocated_cost'] = self._cost_for_volume(
                start, end, purpose.get('unallocated_m3'), model, purpose='unallocated',
            )
        measured = gas.get('measured_intervals')
        if isinstance(measured, list):
            for interval in measured:
                if not isinstance(interval, dict):
                    continue
                try:
                    left = datetime.fromisoformat(str(interval['start']))
                    right = datetime.fromisoformat(str(interval['end']))
                except (KeyError, ValueError):
                    continue
                interval['cost'] = self._cost_for_volume(
                    left, right, interval.get('volume_m3'), model, can_allocate=can_allocate,
                )

    def _stored_gas(self, start: datetime, end: datetime) -> dict[str, Any] | None:
        with self.db.session() as session:
            canonical = session.scalar(
                select(ReportRow.canonical_json)
                .where(
                    ReportRow.period_start == int(start.timestamp()),
                    ReportRow.period_end == int(end.timestamp()),
                )
                .order_by(ReportRow.generated_at.desc())
                .limit(1)
            )
        if canonical is None:
            return None
        gas = Report.model_validate_json(canonical).context.get('gas')
        return dict(gas) if isinstance(gas, dict) else None

    @staticmethod
    def _period_bounds(value: Any) -> tuple[datetime, datetime] | None:
        if not isinstance(value, dict):
            return None
        try:
            start = datetime.fromisoformat(str(value['start']))
            end = datetime.fromisoformat(str(value.get('observed_end') or value['end']))
        except (KeyError, ValueError):
            return None
        return start, end

    def _period_gas(
        self, start: datetime, end: datetime, *, current: Report,
    ) -> dict[str, Any] | None:
        if not self.tariffs:
            return None
        gas = current.context.get('gas') if start == current.period_start and end == current.period_end else None
        if not isinstance(gas, dict):
            gas = self._stored_gas(start, end)
        return gas if isinstance(gas, dict) else None

    def _period_cost(
        self, start: datetime, end: datetime, *, current: Report,
    ) -> dict[str, Any]:
        gas = self._period_gas(start, end, current=current)
        if not isinstance(gas, dict):
            return self._cost_for_volume(start, end, None, None)
        self._refresh_gas_costs(gas, start, end)
        cost = gas.get('cost')
        return cost if isinstance(cost, dict) else self._cost_for_volume(start, end, None, None)

    def _cost_comparison(
        self,
        before_start: datetime,
        before_end: datetime,
        after_start: datetime,
        after_end: datetime,
        *,
        current: Report,
        comparable: bool,
    ) -> dict[str, Any]:
        from zont_analyzer.application.gas_cost import subtract_costs, value_volume_by_period_tariffs

        before_gas = self._period_gas(before_start, before_end, current=current)
        after_gas = self._period_gas(after_start, after_end, current=current)
        before = self._period_cost(before_start, before_end, current=current)
        after = self._period_cost(after_start, after_end, current=current)
        change = subtract_costs(after, before)
        effect = self._cost_for_volume(after_start, after_end, None, None)
        if (
            comparable
            and change.get('status') == 'available'
            and isinstance(before_gas, dict)
            and isinstance(after_gas, dict)
        ):
            before_volume, after_volume = before_gas.get('volume_m3'), after_gas.get('volume_m3')
            if isinstance(before_volume, (int, float)) and isinstance(after_volume, (int, float)):
                effect = value_volume_by_period_tariffs(before_volume - after_volume, after)
                effect.update(
                    volume_m3=before_volume - after_volume,
                    comparison_status='comparable',
                    evaluated_period='after',
                    provenance='Разность расхода сопоставимых окон в тарифных весах оцениваемого периода.',
                )
        if not comparable:
            effect['limitations'] = ['periods_not_comparable']
        return {
            'before': before,
            'after': after,
            'actual_change': change,
            'volume_effect_cost': effect,
        }

    def _refresh_period_comparison_costs(self, report: Report) -> None:
        comparisons = report.context.get('period_comparisons')
        if not isinstance(comparisons, list):
            return
        for item in comparisons:
            if not isinstance(item, dict):
                continue
            baseline = self._period_bounds(item.get('baseline_period'))
            current = self._period_bounds(item.get('current_period'))
            if baseline and current:
                item['gas_cost_comparison'] = self._cost_comparison(
                    *baseline, *current, current=report, comparable=False,
                )
            matched = item.get('matched_windows')
            if not isinstance(matched, list):
                continue
            for pair in matched:
                if not isinstance(pair, dict):
                    continue
                try:
                    pair['gas_cost_comparison'] = self._cost_comparison(
                        datetime.fromisoformat(str(pair['before_start'])),
                        datetime.fromisoformat(str(pair['before_end'])),
                        datetime.fromisoformat(str(pair['after_start'])),
                        datetime.fromisoformat(str(pair['after_end'])),
                        current=report,
                        comparable=pair.get('status') == 'comparable',
                    )
                except (KeyError, ValueError):
                    continue

    def _refresh_savings_costs(self, savings: Any) -> None:
        if not isinstance(savings, dict) or not isinstance(savings.get('comparisons'), list):
            return
        from zont_analyzer.application.gas_cost import subtract_costs, value_volume_by_period_tariffs

        for item in savings['comparisons']:
            if not isinstance(item, dict):
                continue
            try:
                before_start = datetime.fromisoformat(str(item['before_start']))
                before_end = datetime.fromisoformat(str(item['before_end']))
                after_start = datetime.fromisoformat(str(item['after_start']))
                after_end = datetime.fromisoformat(str(item['after_end']))
                observed = float(item['observed_m3'])
                before_volume = observed + float(item['raw_savings']['m3'])
                normalized = item['normalized_savings']['m3']
            except (KeyError, TypeError, ValueError):
                continue
            existing = item.get('actual_costs')
            if isinstance(existing, dict):
                # New reports retain month-bounded source volumes in these slices;
                # tariff-only refreshes can reprice them without telemetry/model work.
                before_slices = existing.get('before', {}).get('slices', [])
                after_slices = existing.get('after', {}).get('slices', [])
            else:
                before_slices, after_slices = [], []
            from zont_analyzer.application.gas_cost import calculate_gas_cost

            before_cost = calculate_gas_cost(
                before_start, before_end, before_volume, self.tariffs, timezone=self.timezone,
                volume_slices=before_slices or None,
            )
            after_cost = calculate_gas_cost(
                after_start, after_end, observed, self.tariffs, timezone=self.timezone,
                volume_slices=after_slices or None,
            )
            item['actual_costs'] = {'before': before_cost, 'after': after_cost}
            currencies_comparable = subtract_costs(after_cost, before_cost).get('status') == 'available'
            valued = (
                value_volume_by_period_tariffs(normalized, after_cost)
                if currencies_comparable
                else self._cost_for_volume(after_start, after_end, None, None)
            )
            if not currencies_comparable:
                valued['limitations'] = ['currencies_not_comparable']
            valued.update(
                effect_status=item.get('effect_status'), evaluated_period='after',
                provenance=(
                    'Денежный эквивалент нормализованного объёма в тарифных весах оцениваемого периода.'
                ),
            )
            item['normalized_savings_cost'] = valued

    def refresh_cost(self, report: Report) -> Report:
        """Reprice canonical money only, preserving gas volume, models and AI."""
        result = report.model_copy(deep=True)
        gas = result.context.get('gas')
        if isinstance(gas, dict):
            self._refresh_gas_costs(gas, result.period_start, result.period_end)
        self._refresh_savings_costs(result.context.get('gas_savings'))
        self._refresh_period_comparison_costs(result)
        return result

    def refresh(self, report: Report) -> Report:
        gas = self.context(report.period_start, report.period_end,
                           complete=report.context.get('period', {}).get('complete', True))
        old = report.context.get('gas')
        pilot_reuse = report.context.get('pilot_ai_reuse')
        reused = bool(report.context.get('ai_interpretation_reuse') or (
            pilot_reuse and pilot_reuse.get('facts_changed') is not False
        ))
        stale = report.ai_used and (reused or not old or old.get('model_version') != gas['model_version'] or
                                   old.get('volume_m3') != gas['volume_m3'] or
                                   old.get('burner_usage_version') != gas['burner_usage_version'] or
                                   old.get('ai_stale', False))
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
        return self.refresh_cost(result)

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
            succeeded = bool(getattr(changed, "rowcount", 0))
        if succeeded:
            from zont_analyzer.reports.chart_data import rebind_chart_cache

            rebind_chart_cache(self.db, original, refreshed)
        return succeeded

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
                before_cost = self._cost_for_volume(
                    prior[-1].start, prior[-1].end, training[-1].volume_m3, frozen_model,
                )
                after_cost = self._cost_for_volume(
                    after[0].start, after[0].end, after_weather.volume_m3, frozen_model,
                )
                from zont_analyzer.application.gas_cost import subtract_costs, value_volume_by_period_tariffs

                currencies_comparable = subtract_costs(after_cost, before_cost).get('status') == 'available'
                normalized_cost = (
                    value_volume_by_period_tariffs(item['normalized_savings']['m3'], after_cost)
                    if currencies_comparable
                    else self._cost_for_volume(after[0].start, after[0].end, None, None)
                )
                if not currencies_comparable:
                    normalized_cost['limitations'] = ['currencies_not_comparable']
                normalized_cost['effect_status'] = item['effect_status']
                normalized_cost['evaluated_period'] = 'after'
                normalized_cost['provenance'] = (
                    'Денежный эквивалент нормализованного объёма в тарифных весах оцениваемого периода.'
                )
                item['actual_costs'] = {'before': before_cost, 'after': after_cost}
                item['normalized_savings_cost'] = normalized_cost
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
            if isinstance(item.get('normalized_savings_cost'), dict):
                item['normalized_savings_cost']['effect_status'] = item['effect_status']
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
