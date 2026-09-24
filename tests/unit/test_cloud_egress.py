from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from zont_analyzer.cloud import integrations
from zont_analyzer.cloud.egress import (
    MAX_REPORT_REQUEST_BYTES,
    MAX_REPORT_RESPONSE_BYTES,
    PolicyClient,
    ReportTransport,
    UpstreamError,
    validate_config,
)


def client_mock(monkeypatch, status=200, content=b'{}'):
    response = httpx.Response(status, content=content)
    client = MagicMock()
    client.__enter__.return_value = client
    client.stream.return_value.__enter__.return_value = response
    factory = MagicMock(return_value=client)
    monkeypatch.setattr('zont_analyzer.cloud.egress.httpx.Client', factory)
    return factory, client


def test_explicit_routes_ignore_ambient_proxy(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted.invalid:80')
    factory, _ = client_mock(monkeypatch)
    policy = PolicyClient()
    policy.request('https://api.openai.com/v1/models/example')
    assert factory.call_args.kwargs == {
        'proxy': 'http://127.0.0.1:1080', 'trust_env': False, 'follow_redirects': False, 'timeout': 5,
    }
    policy.request('https://my.zont.online/api/devices', method='POST')
    assert factory.call_args.kwargs['proxy'] is None
    assert factory.call_args.kwargs['trust_env'] is False


def test_proxy_failure_never_opens_direct_client(monkeypatch):
    factory, client = client_mock(monkeypatch)
    client.stream.side_effect = httpx.ProxyError('private failure')
    with pytest.raises(httpx.ProxyError):
        PolicyClient().request('https://api.openai.com/v1/models/example')
    assert factory.call_count == 1
    assert factory.call_args.kwargs['proxy'] == 'http://127.0.0.1:1080'


@pytest.mark.parametrize('url,method', [
    ('https://my.zont.online/api/set_state', 'POST'),
    ('https://api.openai.com/v1/responses', 'POST'),
    ('https://api.openai.com:444/v1/models/x', 'GET'),
    ('http://api.openai.com/v1/models/x', 'GET'),
    ('https://user:secret@api.openai.com/v1/models/x', 'GET'),
    ('https://untrusted.invalid/v1/models/x', 'GET'),
    ('https://api.openai.com/v1/models/x?secret=value', 'GET'),
])
def test_disallowed_destination_never_opens_socket(monkeypatch, url, method):
    factory, _ = client_mock(monkeypatch)
    with pytest.raises(ValueError):
        PolicyClient().request(url, method=method)
    factory.assert_not_called()


def test_redirect_is_rejected_without_following(monkeypatch):
    factory, client = client_mock(monkeypatch, status=302)
    with pytest.raises(UpstreamError) as error:
        PolicyClient().request('https://api.openai.com/v1/models/example')
    assert error.value.status == 302
    assert client.stream.call_count == 1
    assert factory.call_args.kwargs['follow_redirects'] is False


def test_response_size_is_bounded(monkeypatch):
    client_mock(monkeypatch, content=b'x' * 65537)
    with pytest.raises(ValueError, match='byte limit'):
        PolicyClient().request('https://api.openai.com/v1/models/example')


def test_xray_rejects_direct_fallback():
    config = {'inbounds': [{'listen': '127.0.0.1', 'port': 1080, 'protocol': 'http'}],
              'outbounds': [{'protocol': 'vless'}, {'protocol': 'freedom'}]}
    with pytest.raises(ValueError, match='tunnel and reject'):
        validate_config(config, 1080)
    config['outbounds'][1]['protocol'] = 'blackhole'
    validate_config(config, 1080)
    config['inbounds'][0]['listen'] = '0.0.0.0'
    with pytest.raises(ValueError, match='loopback'):
        validate_config(config, 1080)


def test_integrations_one_zont_read_no_private_response(monkeypatch):
    monkeypatch.setenv('ZONT_TOKEN', 'private-token')
    monkeypatch.setenv('ZONT_CLIENT_EMAIL', 'private@example.invalid')
    request = MagicMock(return_value=b'{"ok":true,"devices":[{"name":"private"}]}')
    monkeypatch.setattr(PolicyClient, 'request', request)
    result = integrations.check({'service': 'zont'})
    assert result == {'service': 'zont', 'http_status': 200, 'route': 'direct', 'read_only': True}
    request.assert_called_once()
    assert request.call_args.kwargs['body'] == b'{"load_io":false}'
    assert 'private' not in json.dumps(result)


def test_openai_requires_access_confirmation_before_request(monkeypatch):
    monkeypatch.delenv('CLOUD_OPENAI_ACCESS_CONFIRMED', raising=False)
    request = MagicMock()
    monkeypatch.setattr(PolicyClient, 'request', request)
    with pytest.raises(PermissionError):
        integrations.check({'service': 'openai'})
    request.assert_not_called()


def test_openai_exactly_one_metadata_request_without_generation(monkeypatch):
    monkeypatch.setenv('CLOUD_OPENAI_ACCESS_CONFIRMED', 'true')
    monkeypatch.setenv('CLOUD_OPENAI_MODEL', 'example-model')
    monkeypatch.setenv('OPENAI_API_KEY', 'private-token')
    request = MagicMock(return_value=b'{"id":"example-model","object":"model"}')
    monkeypatch.setattr(PolicyClient, 'request', request)
    result = integrations.check({'service': 'openai'})
    request.assert_called_once_with('https://api.openai.com/v1/models/example-model',
                                    headers={'Authorization': 'Bearer private-token'})
    assert result['generation'] is False


def test_report_transport_routes_zont_direct_and_responses_through_proxy(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted.invalid:80')
    calls: list[tuple[str, str]] = []

    def route(name):
        def answer(request):
            calls.append((name, request.url.path))
            return httpx.Response(200, content=b'{}')
        return httpx.MockTransport(answer)

    with httpx.Client(transport=ReportTransport(direct=route('direct'), proxied=route('xray')),
                      follow_redirects=False, trust_env=False) as client:
        assert client.post('https://my.zont.online/api/load_data', json={'requests': []}).status_code == 200
        assert client.post('https://my.zont.online/api/raw_events', json={}).status_code == 200
        assert client.post('https://api.openai.com/v1/responses', json={'input': []}).status_code == 200
        assert client.get('https://api.openai.com/v1/models/example').status_code == 200
    assert calls == [('direct', '/api/load_data'), ('direct', '/api/raw_events'),
                     ('xray', '/v1/responses'), ('xray', '/v1/models/example')]


@pytest.mark.parametrize('url,method', [
    ('https://my.zont.online/api/set_state', 'POST'),
    ('https://api.openai.com/v1/chat/completions', 'POST'),
    ('https://api.openai.com/v1/responses', 'GET'),
    ('https://api.openai.com/v1/responses?foo=bar', 'POST'),
    ('http://api.openai.com/v1/responses', 'POST'),
    ('https://other.invalid/api/load_data', 'POST'),
])
def test_report_transport_rejects_unlisted_destination(url, method):
    calls = []
    backend = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200))
    transport = ReportTransport(direct=backend, proxied=backend)
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError, match='destination denied'):
        client.request(method, url)
    assert calls == []


def test_report_transport_bounds_bodies_and_rejects_redirects():
    calls = []
    backend = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(302))
    with httpx.Client(transport=ReportTransport(direct=backend, proxied=backend)) as client:
        with pytest.raises(ValueError, match='request byte limit'):
            client.post('https://my.zont.online/api/devices', content=b'x' * (MAX_REPORT_REQUEST_BYTES + 1))
        assert calls == []
        with pytest.raises(ValueError, match='redirect denied'):
            client.post('https://my.zont.online/api/devices', json={})
    oversized = httpx.MockTransport(lambda _request: httpx.Response(200, content=b'x' *
                                                                (MAX_REPORT_RESPONSE_BYTES + 1)))
    with (httpx.Client(transport=ReportTransport(direct=oversized, proxied=oversized)) as client,
          pytest.raises(ValueError, match='response byte limit')):
        client.post('https://my.zont.online/api/devices', json={})
