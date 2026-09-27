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


def test_conditional_create_and_replace_reject_stale_etag_without_overwrite():
    current = None
    requests = []

    def respond(request):
        nonlocal current
        requests.append(request)
        assert request.method == 'PUT'
        assert request.headers['authorization'] == 'Bearer test-token'
        assert request.headers['accept-encoding'] == 'identity'
        assert request.headers['cache-control'] == 'private, no-store'
        assert request.url.path == '/test-bucket/reports/site-index.json'
        expected = request.headers.get('if-match')
        if expected is None:
            assert request.headers['if-none-match'] == '*'
            if current is not None:
                return httpx.Response(412)
        else:
            assert 'if-none-match' not in request.headers
            if current is None or expected != current[0]:
                return httpx.Response(412)
        current = (f'"revision-{len(requests)}"', request.content)
        return httpx.Response(200, headers={'ETag': current[0]})

    storage = ObjectStorage('test-bucket', token=lambda: 'test-token', transport=httpx.MockTransport(respond))
    assert storage.compare_and_swap('site-index.json', b'first', 'application/json', expected_etag=None)
    assert not storage.compare_and_swap('site-index.json', b'old-create', 'application/json', expected_etag=None)
    assert storage.compare_and_swap('site-index.json', b'newest', 'application/json', expected_etag='"revision-1"')
    assert not storage.compare_and_swap('site-index.json', b'stale', 'application/json', expected_etag='"revision-1"')
    assert current == ('"revision-3"', b'newest')
    assert len(requests) == 4
    for invalid in ('', '*', 'W/"weak"', 'etag\nheader'):
        with pytest.raises(ValueError, match='ETag'):
            storage.compare_and_swap('site-index.json', b'unsafe', 'application/json', expected_etag=invalid)
    assert len(requests) == 4


def test_conditional_request_conflict_is_retryable_but_other_failures_are_errors():
    for status in (409, 412, 403, 500):
        storage = ObjectStorage('test-bucket', token=lambda: 'secret', transport=httpx.MockTransport(
            lambda request, code=status: httpx.Response(code, content=b'private-provider-error')
        ))
        if status in (409, 412):
            assert not storage.compare_and_swap('site-index.json', b'index', 'application/json', expected_etag=None)
        else:
            with pytest.raises(StorageError, match='request failed'):
                storage.compare_and_swap('site-index.json', b'index', 'application/json', expected_etag=None)


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
