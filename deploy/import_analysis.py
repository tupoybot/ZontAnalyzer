"""Import locally accepted derived artifacts, without touching owner state.

Run using the already deployed Python interpreter. Payloads are produced locally
from a verified online copy; this performs bounded optimistic writes, not analysis.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tempfile
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


def install_chart_cache(database: Path, source: Path) -> int:
    """Install prebuilt chart packets only for exactly matching canonical reports.

    Call after importing reports and before starting the worker. Validate the whole
    bundle first; no telemetry is read and a stale bundle cannot replace the cache.
    """
    with sqlite3.connect(database) as connection:
        expected = {
            hashlib.sha256(identifier.encode()).hexdigest(): hashlib.sha256(canonical.encode()).hexdigest()
            for identifier, canonical in connection.execute('select id,canonical_json from reports')
        }
    pending = []
    for path in source.iterdir():
        if path.is_symlink() or not path.is_file() or not re.fullmatch(r'[0-9a-f]{64}\.json', path.name):
            raise ValueError('unexpected chart cache member')
        content = path.read_bytes()
        packet = json.loads(content)
        if (not isinstance(packet, dict) or packet.get('schema_version') != 2
                or not isinstance(packet.get('data'), dict)
                or packet.get('report_digest') != expected.get(path.stem)):
            raise ValueError('chart cache does not match accepted report: '+path.name)
        pending.append((path.name, content))
    target = database.parent / 'chart-data-cache'
    target.mkdir(exist_ok=True)
    owner = database.stat()
    count = 0
    for name, content in pending:
        destination = target / name
        if destination.exists() and destination.read_bytes() == content:
            continue
        descriptor, temporary = tempfile.mkstemp(prefix='.import-chart-', dir=target)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), 0o600)
                if os.geteuid() == 0:
                    os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            count += 1
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if os.geteuid() == 0:
        os.chown(target, owner.st_uid, owner.st_gid)
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('payload', type=Path)
    parser.add_argument('--chart-cache', type=Path, help='Prebuilt cache directory for imported report digests')
    args = parser.parse_args()
    result = import_payload(args.database, json.loads(args.payload.read_text()))
    if args.chart_cache is not None:
        result['chart_cache'] = install_chart_cache(args.database, args.chart_cache)
    print(json.dumps(result))
