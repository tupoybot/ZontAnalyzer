import hashlib
import importlib.util
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from zont_analyzer.runtime import build_runtime


def importer():
    path = Path(__file__).parents[2]/'deploy/import_analysis.py'
    spec = importlib.util.spec_from_file_location('import_analysis', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.import_payload


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
