import hashlib
import importlib.util
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from zont_analyzer.runtime import build_runtime


def importer(name="import_payload"):
    path = Path(__file__).parents[2]/'deploy/import_analysis.py'
    spec = importlib.util.spec_from_file_location('import_analysis', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


def test_derived_import_is_atomic_idempotent_and_preserves_owner_state(tmp_path):
    r = build_runtime(None, tmp_path)
    reports = [r.analysis(no_ai=True).analyze_daily(date(2026,1,d), use_ai=False) for d in [1,2]]
    rid = reports[0].recommendations[0].id
    r.db.set_recommendation_feedback(rid, 'rejected', 'keep')
    before = r.db.recommendation(rid)
    with sqlite3.connect(r.db.path) as c:
        rows = c.execute('select id,canonical_json,generated_at from reports order by id').fetchall()
    payload = {'reports': [], 'app_meta': {'gas-model:hash': '{}'}, 'new_rows': {}}
    for row in rows:
        value = json.loads(row[1])
        value['context']['gas'] = {'model_version':'new'}
        payload['reports'].append(dict(id=row[0], previous_sha256=hashlib.sha256(row[1].encode()).hexdigest(),
                                       canonical_json=json.dumps(value), generated_at=row[2]))
    good_hash = payload['reports'][1]['previous_sha256']
    payload['reports'][1]['previous_sha256'] = 'wrong'
    apply = importer()
    with pytest.raises(ValueError, match='changed since backup'):
        apply(r.db.path, payload)
    assert r.db.report(reports[0].id).context.get('gas', {}).get('model_version') != 'new'
    assert r.db.get_app_meta('gas-model:hash') is None
    payload['reports'][1]['previous_sha256'] = good_hash
    assert apply(r.db.path, payload)['reports'] == 2
    assert apply(r.db.path, payload) == {'reports':0,'app_meta':0,'new_rows':0}
    assert r.db.recommendation(rid) == before
    payload['app_meta'] = {'owner-profile:forbidden': '{}'}
    with pytest.raises(ValueError, match='unsupported metadata'):
        apply(r.db.path, payload)


def test_prebuilt_chart_cache_is_guarded_and_idempotent(tmp_path):
    r = build_runtime(None, tmp_path/'data')
    report = r.analysis(no_ai=True).analyze_daily(date(2026, 1, 1), use_ai=False)
    with sqlite3.connect(r.db.path) as connection:
        canonical = connection.execute('select canonical_json from reports where id=?', (report.id,)).fetchone()[0]
    source = tmp_path/'prepared'
    source.mkdir()
    name = hashlib.sha256(report.id.encode()).hexdigest()+'.json'
    packet = {'schema_version': 1, 'report_digest': hashlib.sha256(canonical.encode()).hexdigest(),
              'data': {'series': {}, 'timezone': 'UTC'}}
    path = source/name
    path.write_text(json.dumps(packet))
    install = importer('install_chart_cache')
    assert install(r.db.path, source) == 1
    assert install(r.db.path, source) == 0
    destination = r.db.path.parent/'chart-data-cache'/name
    assert destination.read_bytes() == path.read_bytes()
    packet['report_digest'] = 'stale'
    path.write_text(json.dumps(packet))
    before = destination.read_bytes()
    with pytest.raises(ValueError, match='does not match'):
        install(r.db.path, source)
    assert destination.read_bytes() == before


def test_chart_cache_bundle_rejects_non_cache_paths(tmp_path):
    r = build_runtime(None, tmp_path/'data')
    source = tmp_path/'prepared'
    source.mkdir()
    (source/'unexpected.txt').write_text('{}')
    with pytest.raises(ValueError, match='unexpected'):
        importer('install_chart_cache')(r.db.path, source)
