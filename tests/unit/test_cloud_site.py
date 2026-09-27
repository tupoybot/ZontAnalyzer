from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from zont_analyzer.cloud import site
from zont_analyzer.cloud.object_storage import StorageError, StoredObject


def test_site_uses_only_committed_objects_and_never_renders(monkeypatch):
    meta = {'manifest_key': 'manifests/active.json', 'latest_key': 'latest/active.html'}
    objects = {
        'manifests/active.json': json.dumps({'reports': [{'href': 'daily/2026-09-01.html',
            'html_key': 'reports/complete.html', 'json_key': 'reports/complete.json'}]}).encode(),
        'latest/active.html': b'<html>latest</html>',
        'reports/complete.html': b'<html>archive</html>',
        'reports/complete.json': b'{"canonical":true}',
    }
    storage = Mock()
    storage.get.side_effect = lambda key: StoredObject(objects[key], '', 'etag') if key in objects else None
    monkeypatch.setattr(site.ObjectStorage, 'from_environment', lambda: storage)
    monkeypatch.setattr(site, 'PublicationRepository', lambda db: SimpleNamespace(load_meta=lambda: dict(meta)))
    runtime = SimpleNamespace(db=SimpleNamespace(storage=object()))
    for path, expected in [('/', b'<html>latest</html>'), ('/latest.html', b'<html>latest</html>'),
                           ('/daily/2026-09-01.html', b'<html>archive</html>'),
                           ('/daily/2026-09-01.json', b'{"canonical":true}')]:
        status, body, _ = site.serve(runtime, path)
        assert status == 200 and body == expected
    for path in ['/daily/', '/private/probe.txt', '/reports/complete.html', '/daily/../latest.html',
                 '/daily/%2e%2e/latest.html', 'https://evil.invalid/latest.html']:
        assert site.serve(runtime, path)[0] == 404
    assert site.serve(runtime, '/monthly/2026-01-01.html')[0] == 404
    del objects['reports/complete.html']
    with pytest.raises(StorageError, match='missing'):
        site.serve(runtime, '/daily/2026-09-01.html')


def test_empty_publication_is_not_rebuilt_on_read(monkeypatch):
    storage = Mock()
    monkeypatch.setattr(site.ObjectStorage, 'from_environment', lambda: storage)
    monkeypatch.setattr(site, 'PublicationRepository', lambda db: SimpleNamespace(load_meta=lambda: {}))
    runtime = SimpleNamespace(db=SimpleNamespace(storage=object()))
    assert site.serve(runtime, '/latest.html')[0] == 404
    storage.get.assert_not_called()
