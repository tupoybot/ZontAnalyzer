"""Find the newest generated report without rereading every report payload.

The report table has no generated-at index.  A compact, disposable app_meta
projection records that timestamp at each report revision.  Existing writers
that do not maintain the projection remain correct: a revision mismatch causes
one lazy payload read.  A first lookup over an uncached archive necessarily
loads its payloads once; later lookups scan only id/revision metadata and read
the selected report's payload.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

import ydb  # type: ignore[import-untyped]

from zont_analyzer.domain import Report

from .database import Transaction

if TYPE_CHECKING:
    from .application import Database


_PREFIX = 'latest-report:v1:'
_PAGE_SIZE = 100
_CACHE_BATCH = 128
_PAYLOAD_BATCH = 32


def _key(report_id: str) -> str:
    return _PREFIX + report_id


def _value(report: Report, revision: int) -> str:
    return json.dumps({
        'revision': revision,
        'period_end': int(report.period_end.timestamp()),
        'generated_at': report.generated_at.isoformat(),
    }, separators=(',', ':'), sort_keys=True)


def cache_parameters(report: Report, revision: int) -> dict[str, str]:
    """Bind a report's compact metadata alongside its primary report UPSERT."""
    return {'$catalogue_key': _key(report.id), '$catalogue_value': _value(report, revision)}


def _batches(values: list[Any], size: int) -> Iterator[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset:offset + size]


def _catalogue(db: Database) -> list[tuple[str, int, int]]:
    catalogue: list[tuple[str, int, int]] = []
    after = ''
    while True:
        rows = db.storage.execute(
            'DECLARE $after AS Utf8; DECLARE $limit AS Uint64; '
            'SELECT id,period_end,revision FROM reports VIEW by_id '
            'WHERE id>$after ORDER BY id LIMIT $limit;',
            {'$after': after, '$limit': ydb.TypedValue(_PAGE_SIZE, ydb.PrimitiveType.Uint64)},
        )[0].rows
        for row in rows:
            report_id = str(row.id)
            if catalogue and report_id == catalogue[-1][0]:
                raise ValueError('duplicate report ID')
            catalogue.append((report_id, int(row.period_end), int(row.revision or 0)))
        if len(rows) < _PAGE_SIZE:
            return catalogue
        after = str(rows[-1].id)


def _cached(db: Database, catalogue: list[tuple[str, int, int]]) -> dict[str, datetime]:
    stamps: dict[str, datetime] = {}
    for batch in _batches(catalogue, _CACHE_BATCH):
        keys = [_key(report_id) for report_id, _, _ in batch]
        rows = db.storage.execute(
            'DECLARE $keys AS List<Utf8>; SELECT key,value FROM app_meta WHERE key IN $keys;',
            {'$keys': ydb.TypedValue(keys, ydb.ListType(ydb.PrimitiveType.Utf8))},
        )[0].rows
        expected = {_key(report_id): (report_id, period_end, revision)
                    for report_id, period_end, revision in batch}
        for row in rows:
            wanted = expected.get(str(row.key))
            if wanted is None or row.value is None:
                continue
            report_id, period_end, revision = wanted
            try:
                value = json.loads(str(row.value))
                if value['revision'] == revision and value['period_end'] == period_end:
                    generated = datetime.fromisoformat(value['generated_at'])
                    if generated.tzinfo is not None:
                        stamps[report_id] = generated
            except (KeyError, TypeError, ValueError):
                continue
    return stamps


def _save_cache_batch(
    db: Database, batch: list[tuple[str, int, int]], cache_rows: list[dict[str, str]],
) -> None:
    ids = [report_id for report_id, _, _ in batch]
    parameter = {'$ids': ydb.TypedValue(ids, ydb.ListType(ydb.PrimitiveType.Utf8))}
    row_type = (ydb.StructType().add_member('key', ydb.PrimitiveType.Utf8)
                .add_member('value', ydb.PrimitiveType.Utf8))

    def write(tx: Transaction) -> None:
        fresh = tx.execute(
            'DECLARE $ids AS List<Utf8>; '
            'SELECT id,period_end,revision FROM reports VIEW by_id WHERE id IN $ids;',
            parameter,
        )[0].rows
        observed = [(str(row.id), int(row.period_end), int(row.revision or 0)) for row in fresh]
        if len(observed) != len(batch) or sorted(observed) != sorted(batch):
            raise ValueError('reports changed while latest report was selected')
        tx.execute(
            'DECLARE $rows AS List<Struct<key:Utf8,value:Utf8>>; '
            'UPSERT INTO app_meta SELECT * FROM AS_TABLE($rows);',
            {'$rows': ydb.TypedValue(cache_rows, ydb.ListType(row_type))},
        )

    db.storage.transaction(write)


def _fill(db: Database, missing: list[tuple[str, int, int]]) -> dict[str, datetime]:
    stamps: dict[str, datetime] = {}
    for batch in _batches(missing, _PAYLOAD_BATCH):
        ids = [report_id for report_id, _, _ in batch]
        parameter = {'$ids': ydb.TypedValue(ids, ydb.ListType(ydb.PrimitiveType.Utf8))}
        rows = db.storage.execute(
            'DECLARE $ids AS List<Utf8>; '
            'SELECT id,period_end,revision,payload FROM reports VIEW by_id WHERE id IN $ids;',
            parameter,
        )[0].rows
        expected = {report_id: (period_end, revision) for report_id, period_end, revision in batch}
        found: dict[str, Report] = {}
        for row in rows:
            report_id = str(row.id)
            if report_id in found or report_id not in expected or (
                int(row.period_end), int(row.revision or 0)
            ) != expected[report_id]:
                raise ValueError('reports changed while latest report was selected')
            report = Report.model_validate(json.loads(row.payload)['report'])
            if report.id != report_id or int(report.period_end.timestamp()) != int(row.period_end):
                raise ValueError('report payload and index disagree')
            found[report_id] = report
        if set(found) != set(ids):
            raise ValueError('reports changed while latest report was selected')

        cache_rows = [{'key': _key(report_id), 'value': _value(found[report_id], expected[report_id][1])}
                      for report_id in ids]
        _save_cache_batch(db, batch, cache_rows)
        stamps.update({report_id: found[report_id].generated_at for report_id in ids})
    return stamps


def latest_report(db: Database) -> Report | None:
    """Match ``max(_all_reports(), generated_at)`` and abort a mixed read."""
    catalogue = _catalogue(db)
    if not catalogue:
        if _catalogue(db):
            raise ValueError('reports changed while latest report was selected')
        return None
    stamps = _cached(db, catalogue)
    missing = [item for item in catalogue if item[0] not in stamps]
    if missing:
        stamps.update(_fill(db, missing))

    # _all_reports iterated in period_end,id order; max kept the first item
    # when generated_at tied.  Retain that exact tie rule.
    best_id, best_end, _ = catalogue[0]
    best_time = stamps[best_id]
    for report_id, period_end, _ in catalogue[1:]:
        generated = stamps[report_id]
        if generated > best_time or (generated == best_time and
                                     (period_end, report_id) < (best_end, best_id)):
            best_id, best_end, best_time = report_id, period_end, generated

    rows = db.storage.execute(
        'DECLARE $id AS Utf8; SELECT id,period_end,revision,payload '
        'FROM reports VIEW by_id WHERE id=$id LIMIT 2;', {'$id': best_id},
    )[0].rows
    expected = next(item for item in catalogue if item[0] == best_id)
    if len(rows) != 1 or (str(rows[0].id), int(rows[0].period_end), int(rows[0].revision or 0)) != expected:
        raise ValueError('reports changed while latest report was selected')
    report = Report.model_validate(json.loads(rows[0].payload)['report'])
    if report.generated_at != best_time or report.id != best_id:
        raise ValueError('reports changed while latest report was selected')
    if _catalogue(db) != catalogue:
        raise ValueError('reports changed while latest report was selected')
    return report
