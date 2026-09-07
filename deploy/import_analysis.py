"""Import locally accepted derived artifacts, without touching owner state.

Run using the already deployed Python interpreter. Payloads are produced locally
from a verified online copy; this performs bounded optimistic writes, not analysis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def import_payload(database: Path, payload: dict[str, Any]) -> dict[str, int]:
    if set(payload) != {'reports', 'app_meta', 'new_rows'}:
        raise ValueError('unexpected payload keys')
    counts = {'reports': 0, 'app_meta': 0, 'new_rows': 0}
    connection = sqlite3.connect(database)
    connection.execute('pragma foreign_keys=on')
    try:
        connection.execute('begin immediate')
        for item in payload['reports']:
            row = connection.execute('select canonical_json from reports where id=?', (item['id'],)).fetchone()
            if row is None:
                raise ValueError('report missing: '+item['id'])
            if row[0] == item['canonical_json']:
                continue
            if hashlib.sha256(row[0].encode()).hexdigest() != item['previous_sha256']:
                raise ValueError('report changed since backup: '+item['id'])
            canonical = json.loads(item['canonical_json'])
            if canonical['id'] != item['id']:
                raise ValueError('canonical report identity mismatch')
            connection.execute('update reports set canonical_json=?,generated_at=? where id=?',
                               (item['canonical_json'], item['generated_at'], item['id']))
            counts['reports'] += 1
        for key, value in payload['app_meta'].items():
            if not key.startswith(('gas-model:', 'gas-exposure:', 'gas-report-revision:')):
                raise ValueError('unsupported metadata key')
            row = connection.execute('select value from app_meta where key=?', (key,)).fetchone()
            if row:
                if row[0] != value:
                    raise ValueError('content-addressed metadata conflict: '+key)
                continue
            connection.execute('insert into app_meta(key,value) values(?,?)', (key, value))
            counts['app_meta'] += 1
        for table, rows in payload['new_rows'].items():
            if table not in {'recommendations', 'llm_calls'}:
                raise ValueError('unsupported append-only table')
            columns = [row[1] for row in connection.execute('pragma table_info('+table+')')]
            for record in rows:
                if set(record) != set(columns):
                    raise ValueError('unexpected row columns')
                existing = connection.execute('select * from '+table+' where id=?', (record['id'],)).fetchone()
                values = tuple(record[key] for key in columns)
                if existing:
                    if tuple(existing) != values:
                        raise ValueError('append-only row conflict: '+record['id'])
                    continue
                connection.execute('insert into '+table+'('+','.join(columns)+') values ('+
                                   ','.join('?' for _ in columns)+')', values)
                counts['new_rows'] += 1
        connection.commit()
        return counts
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('payload', type=Path)
    args = parser.parse_args()
    print(json.dumps(import_payload(args.database, json.loads(args.payload.read_text()))))
