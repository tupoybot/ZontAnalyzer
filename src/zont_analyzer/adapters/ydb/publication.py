"""Durable publication index and change queries in native YDB operations."""

from __future__ import annotations

import json
from typing import Any

import ydb  # type: ignore[import-untyped]

from zont_analyzer.domain import Report

from .database import Transaction, YdbDatabase


def _limit(value: int = 1000) -> ydb.TypedValue:
    return ydb.TypedValue(value, ydb.PrimitiveType.Uint64)


class PublicationRepository:
    def __init__(self, db: YdbDatabase) -> None:
        self.db = db

    def load_meta(self) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT name,value FROM metadata WHERE name>='publication:' AND name<'publication;';"
        )[0].rows
        return {str(row.name)[12:]: str(row.value) for row in rows}

    def has_dirty(self) -> bool:
        rows = self.db.execute(
            "SELECT href FROM publication_items VIEW by_queue WHERE dirty>0 "
            "ORDER BY dirty,queued_at LIMIT 1;"
        )[0].rows
        return bool(rows)

    def audit_page(self, after: str, limit: int = 16) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
            "SELECT href,json_stamp,html_stamp FROM publication_items WHERE href>$after "
            "ORDER BY href LIMIT $limit;",
            {"$after": after, "$limit": _limit(limit)},
        )[0].rows
        return [dict(row) for row in rows]

    def latest_daily(self) -> dict[str, Any] | None:
        rows = self.db.execute(
            "SELECT href,report_id,period_start FROM publication_items VIEW by_kind_start "
            "WHERE kind='daily' ORDER BY period_start DESC LIMIT 1;"
        )[0].rows
        return dict(rows[0]) if rows else None

    def load(self) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        items: dict[str, dict[str, Any]] = {}
        after = ""
        while True:
            rows = self.db.execute(
                "DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
                "SELECT * FROM publication_items WHERE href>$after ORDER BY href LIMIT $limit;",
                {"$after": after, "$limit": _limit()},
            )[0].rows
            for row in rows:
                item = dict(row)
                item["start"] = int(item.pop("period_start"))
                item["end"] = int(item.pop("period_end"))
                item["generated"] = int(item.pop("generated_at"))
                item["queued"] = int(item.pop("queued_at"))
                item["comparisons"] = bool(item["comparisons"])
                item["dirty"] = int(item["dirty"])
                items[str(item["href"])] = item
            if len(rows) < 1000:
                break
            after = str(rows[-1].href)
        return items, self.load_meta()

    def changes_since(self, revision: int, upper: int) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        after = revision
        while after < upper:
            rows = self.db.execute(
                "DECLARE $after AS Int64; DECLARE $upper AS Int64; DECLARE $limit AS Uint64; "
                "SELECT scope,identifier,revision,payload FROM publication_changes VIEW by_revision "
                "WHERE revision>$after AND revision<=$upper ORDER BY revision LIMIT $limit;",
                {"$after": after, "$upper": upper, "$limit": _limit()},
            )[0].rows
            changes.extend(dict(row) for row in rows)
            if len(rows) < 1000:
                break
            after = int(rows[-1].revision)
        return changes

    def reading_span(self) -> tuple[str | None, str | None]:
        rows = self.db.execute("SELECT MIN(reading_day) AS lo,MAX(reading_day) AS hi FROM gas_readings;")[0].rows
        return (str(rows[0].lo) if rows[0].lo is not None else None,
                str(rows[0].hi) if rows[0].hi is not None else None) if rows else (None, None)

    def next_tariff_start(self, start: str) -> str | None:
        # Effective times are UTC ISO strings in the canonical tariff payload.
        following: str | None = None
        after_scope, after_month = "", ""
        while True:
            rows = self.db.execute(
                "DECLARE $scope AS Utf8; DECLARE $month AS Utf8; DECLARE $limit AS Uint64; "
                "SELECT scope,effective_month,payload FROM gas_tariffs "
                "WHERE scope>$scope OR (scope=$scope AND effective_month>$month) "
                "ORDER BY scope,effective_month LIMIT $limit;",
                {"$scope": after_scope, "$month": after_month, "$limit": _limit()},
            )[0].rows
            for row in rows:
                value = str(json.loads(row.payload)["effective_from"])
                if value > start and (following is None or value < following):
                    following = value
            if len(rows) < 1000:
                return following
            after_scope, after_month = str(rows[-1].scope), str(rows[-1].effective_month)

    def canonical_reports(self, now: Any) -> list[Report]:
        reports: list[Report] = []
        after = ""
        while True:
            rows = self.db.execute(
                "DECLARE $after AS Utf8; DECLARE $limit AS Uint64; "
                "SELECT id,payload FROM reports VIEW by_id WHERE id>$after ORDER BY id LIMIT $limit;",
                {"$after": after, "$limit": _limit()},
            )[0].rows
            for row in rows:
                report = Report.model_validate(json.loads(row.payload)["report"])
                if report.period_end <= now and report.generated_at >= report.period_end:
                    reports.append(report)
            if len(rows) < 1000:
                return reports
            after = str(rows[-1].id)

    def save(
        self,
        items: dict[str, dict[str, Any]],
        previous: dict[str, dict[str, Any]],
        meta: dict[str, str],
        old_meta: dict[str, str],
        *,
        expected_checkpoint: int,
        lease_owner: str,
        lease_attempt: int,
    ) -> bool:
        changed = [item for href, item in items.items() if previous.get(href) != item]
        removed = [href for href in previous if href not in items]
        changed_meta = {key: value for key, value in meta.items() if old_meta.get(key) != value}

        def write(tx: Transaction) -> bool:
            rows = tx.execute(
                "SELECT owner,attempt,lease_until,state FROM jobs WHERE job_key='publication';"
            )[0].rows
            import time

            if not rows or str(rows[0].owner) != lease_owner or int(rows[0].attempt) != lease_attempt \
                    or rows[0].state != "active" or int(rows[0].lease_until) <= time.time_ns() // 1000:
                return False
            rows = tx.execute("SELECT value FROM metadata WHERE name='publication:checkpoint';")[0].rows
            current = int(rows[0].value) if rows else 0
            if current != expected_checkpoint:
                return False
            for href in removed:
                tx.execute("DECLARE $href AS Utf8; DELETE FROM publication_items WHERE href=$href;", {"$href": href})
            for item in changed:
                tx.execute(
                    "DECLARE $href AS Utf8; DECLARE $report_id AS Utf8; DECLARE $kind AS Utf8; "
                    "DECLARE $start AS Int64; DECLARE $end AS Int64; DECLARE $generated AS Int64; "
                    "DECLARE $digest AS Utf8; DECLARE $lo AS Double; DECLARE $hi AS Double; "
                    "DECLARE $comparisons AS Bool; DECLARE $dirty AS Int64; DECLARE $queued AS Int64; "
                    "DECLARE $entry AS Utf8?; DECLARE $json_stamp AS Utf8; DECLARE $html_stamp AS Utf8; "
                    "UPSERT INTO publication_items (href,report_id,kind,period_start,period_end,generated_at,"
                    "digest,lo,hi,comparisons,dirty,queued_at,entry,json_stamp,html_stamp) VALUES "
                    "($href,$report_id,$kind,$start,$end,$generated,$digest,$lo,$hi,$comparisons,$dirty,"
                    "$queued,$entry,$json_stamp,$html_stamp);",
                    {"$href": item["href"], "$report_id": item["report_id"], "$kind": item["kind"],
                     "$start": item["start"], "$end": item["end"], "$generated": item["generated"],
                     "$digest": item["digest"], "$lo": float(item["lo"]), "$hi": float(item["hi"]),
                     "$comparisons": item["comparisons"], "$dirty": item["dirty"], "$queued": item["queued"],
                     "$entry": ydb.TypedValue(item["entry"], ydb.OptionalType(ydb.PrimitiveType.Utf8)),
                     "$json_stamp": item["json_stamp"], "$html_stamp": item["html_stamp"]},
                )
            for key, value in changed_meta.items():
                tx.execute(
                    "DECLARE $name AS Utf8; DECLARE $value AS Utf8; "
                    "UPSERT INTO metadata (name,value) VALUES ($name,$value);",
                    {"$name": "publication:" + key, "$value": value},
                )
            return True

        return self.db.transaction(write)
