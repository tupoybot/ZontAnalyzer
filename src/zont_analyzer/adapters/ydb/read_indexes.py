"""Additive read indexes compatible with the original schema-v2 writers.

The base table hash deliberately stays unchanged: indexes neither change stored
columns nor require a data conversion, and older releases can keep writing (or
be restored) while YDB maintains the synchronous index. Deployment can call
``ensure_read_indexes`` before switching the application image.
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import ydb  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from .database import YdbDatabase

SUPPLEMENTAL_INDEXES: dict[str, dict[str, tuple[str, ...]]] = {
    'reports': {'by_kind_end': ('kind', 'period_end')},
    'coverage': {'by_end': ('device_id', 'data_type', 'ended_at')},
}


def ensure_read_indexes(db: YdbDatabase) -> None:
    session = db.driver.table_client.session().create()
    try:
        for table, indexes in SUPPLEMENTAL_INDEXES.items():
            path = db.path + '/' + table
            for name, columns in indexes.items():
                _ensure_index(session, path, table, name, columns)
    finally:
        session.delete()


def _ensure_index(session: ydb.Session, path: str, table: str, name: str, columns: tuple[str, ...]) -> None:
    def state() -> object:
        for index in session.describe_table(path).indexes:
            if index.name == name:
                if index.index_columns != list(columns) or index.data_columns:
                    raise ValueError(f'incompatible {table}.{name} index')
                return index.status
        return None

    current = state()
    if current == ydb.IndexStatus.READY:
        return
    operation = None
    if current is None:
        try:
            operation = session.alter_table(path, add_indexes=[
                ydb.TableIndex(name).with_global_index().with_index_columns(*columns),
            ])
        except (ydb.AlreadyExists, ydb.SchemeError):
            # Concurrent initialization is safe only for the same index.
            if state() is None:
                raise
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if operation is not None and not operation.ready:
            operation = operation.get()
        if (operation is None or operation.ready) and state() == ydb.IndexStatus.READY:
            return
        time.sleep(0.25)
    raise TimeoutError(f'{table}.{name} index is not ready; retry initialization after the build')
