from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from zont_analyzer.cloud import integrations
from zont_analyzer.cloud.egress import PolicyClient, UpstreamError, validate_config


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
