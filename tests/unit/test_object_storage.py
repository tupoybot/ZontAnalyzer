from __future__ import annotations

import httpx
import pytest

from zont_analyzer.cloud.object_storage import MetadataToken, ObjectStorage, StorageError


def test_immutable_upload_and_retry_do_not_overwrite():
    objects = {}
    methods = []

    def respond(request):
        methods.append(request.method)
        assert request.headers['authorization'] == 'Bearer test-token'
        assert request.headers['accept-encoding'] == 'identity'
        assert request.url.host == 'storage.yandexcloud.net'
        if request.method == 'PUT':
            assert request.headers['if-none-match'] == '*'
            if request.url.path in objects:
                return httpx.Response(412)
            objects[request.url.path] = request.content
            return httpx.Response(200, headers={'ETag': 'version'})
        return httpx.Response(200, content=objects[request.url.path], headers={'ETag': 'version'})

    storage = ObjectStorage('test-bucket', token=lambda: 'test-token', transport=httpx.MockTransport(respond))
    assert storage.put('publication/report.html', b'complete', 'text/html') == 'version'
    assert storage.put('publication/report.html', b'complete', 'text/html') == 'version'
    with pytest.raises(StorageError, match='conflict'):
        storage.put('publication/report.html', b'changed', 'text/html')
    assert list(objects.values()) == [b'complete']
    assert methods == ['PUT', 'PUT', 'GET', 'PUT', 'GET']


def test_storage_rejects_paths_redirects_and_redacts_errors():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={'Location': 'https://private.invalid/token'}, content=b'secret')

    storage = ObjectStorage('test-bucket', token=lambda: 'secret', transport=httpx.MockTransport(respond))
    for key in ('../private', '/private', 'a//b', 'a/./b', 'a?token=secret', 'a%2fb'):
        with pytest.raises(ValueError):
            storage.get(key)
    assert requests == []
    with pytest.raises(StorageError) as error:
        storage.get('report.html')
    assert len(requests) == 1
    assert 'secret' not in str(error.value) and 'private' not in str(error.value)


def test_storage_missing_and_bounded_response(monkeypatch):
    monkeypatch.setattr('zont_analyzer.cloud.object_storage.MAX_OBJECT_BYTES', 3)
    storage = ObjectStorage('test-bucket', token=lambda: 'test', transport=httpx.MockTransport(
        lambda request: (httpx.Response(404) if request.url.path.endswith('missing')
                         else httpx.Response(200, content=b'four'))
    ))
    assert storage.get('missing') is None
    assert storage.head('missing') == ''
    with pytest.raises(StorageError, match='byte limit'):
        storage.get('too-large')
    with pytest.raises(StorageError, match='byte limit'):
        storage.put('too-large', b'four', 'text/html')


def test_metadata_token_refresh_does_not_use_ambient_proxy(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY', 'http://untrusted.invalid')
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == '169.254.169.254'
        assert request.headers['Metadata-Flavor'] == 'Google'
        return httpx.Response(200, json={'access_token': 'short-lived', 'expires_in': 3600})

    token = MetadataToken(httpx.MockTransport(respond))
    assert token() == token() == 'short-lived'
    assert len(requests) == 1
    token.expires = 0
    assert token() == 'short-lived'
    assert len(requests) == 2
