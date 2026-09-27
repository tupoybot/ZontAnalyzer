from __future__ import annotations

import base64
import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from zont_analyzer.cloud import auth
from zont_analyzer.cloud.runtime import CloudServer, RuntimeConfig

CONFIG = auth.OidcConfig("https://auth.yandex.cloud", "fixture-client",
                         "https://auth.yandex.cloud/oauth/jwks/keys")


@pytest.fixture(scope="module")
def keys() -> tuple[Any, Any]:
    return tuple(rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2))


def _jwk(key: Any, kid: str = "first") -> dict[str, Any]:
    result = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    return {**result, "kid": kid, "use": "sig", "alg": "RS256"}


def _claims() -> dict[str, Any]:
    now = int(time.time())
    return {"iss": CONFIG.issuer, "aud": CONFIG.audience, "iat": now - 1, "exp": now + 600, "sub": "fixture-user"}


def _token(key: Any, *, claims: dict[str, Any] | None = None, kid: str = "first",
           headers: dict[str, Any] | None = None) -> str:
    return jwt.encode(claims or _claims(), key, algorithm="RS256", headers={"kid": kid, **(headers or {})})


def _headers(token: str) -> Message:
    headers = Message()
    headers.add_header("Cookie", f"{auth.OIDC_COOKIE_NAME}={token}")
    return headers


def test_oidc_configuration_is_all_or_none_and_pins_official_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    names = ("CLOUD_OIDC_ISSUER", "CLOUD_OIDC_AUDIENCE", "CLOUD_OIDC_JWKS_URI")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    assert auth.OidcConfig.from_environment() is None
    monkeypatch.setenv(names[0], CONFIG.issuer)
    with pytest.raises(ValueError):
        auth.OidcConfig.from_environment()
    for name, value in zip(names, (CONFIG.issuer, CONFIG.audience, CONFIG.jwks_uri), strict=True):
        monkeypatch.setenv(name, value)
    assert auth.OidcConfig.from_environment() == CONFIG
    monkeypatch.setenv("CLOUD_ENVIRONMENT", "dev")
    monkeypatch.setenv("CLOUD_WEB_CREDENTIALS", "fixture:secret")
    assert RuntimeConfig.from_environment().oidc == CONFIG
    assert auth.OidcConfig(CONFIG.issuer + "/oauth/" + CONFIG.audience, CONFIG.audience, CONFIG.jwks_uri)
    for issuer, audience, jwks in [
        ("http://auth.yandex.cloud", CONFIG.audience, CONFIG.jwks_uri),
        (CONFIG.issuer + "/oauth/other-client", CONFIG.audience, CONFIG.jwks_uri),
        (CONFIG.issuer, "../client", CONFIG.jwks_uri),
        (CONFIG.issuer, CONFIG.audience, "https://other.example/keys"),
        (CONFIG.issuer, CONFIG.audience, CONFIG.jwks_uri + "?target=other"),
        (CONFIG.issuer, CONFIG.audience, "https://auth.yandex.cloud:443/oauth/jwks/keys"),
    ]:
        with pytest.raises(ValueError):
            auth.OidcConfig(issuer, audience, jwks)


def test_oidc_verifies_signature_claims_and_bounded_unambiguous_cookie(keys: tuple[Any, Any]) -> None:
    first, second = keys
    requests = []

    def jwks(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"keys": [_jwk(first)]})

    verifier = auth.OidcVerifier(CONFIG, transport=httpx.MockTransport(jwks))
    token = _token(first)
    assert verifier.authorized(_headers(token))
    assert len(requests) == 1 and str(requests[0].url) == CONFIG.jwks_uri
    assert "Cookie" not in requests[0].headers and "Authorization" not in requests[0].headers
    mutations = [
        {"iss": CONFIG.issuer + "/wrong"}, {"aud": "wrong"}, {"aud": [CONFIG.audience]},
        {"exp": int(time.time()) - 1}, {"iat": int(time.time()) + 90},
        {"iat": "123"}, {"iat": True}, {"exp": "9999999999"}, {"sub": ""},
        {"nbf": int(time.time()) + 90},
    ]
    for mutation in mutations:
        assert not verifier.authorized(_headers(_token(first, claims={**_claims(), **mutation})))
    for missing in ("iss", "aud", "exp", "iat", "sub"):
        claims = _claims()
        claims.pop(missing)
        assert not verifier.authorized(_headers(_token(first, claims=claims)))
    assert not verifier.authorized(_headers(_token(second)))
    assert not verifier.authorized(_headers(_token(first, headers={"jku": "https://other.example/keys"})))
    assert not verifier.authorized(_headers(jwt.encode(_claims(), "fixture-secret" * 4, algorithm="HS256",
                                                        headers={"kid": "first"})))
    duplicate = _headers(token)
    duplicate.add_header("Cookie", f"{auth.OIDC_COOKIE_NAME}={token}")
    assert not verifier.authorized(duplicate)
    assert not verifier.authorized(_headers("x" * (auth.MAX_OIDC_TOKEN_BYTES + 1)))
    assert not verifier.authorized(_headers("malformed.token"))
    assert len(requests) == 1


def test_oidc_cache_bounds_fetches_rotates_and_never_uses_expired_keys(keys: tuple[Any, Any]) -> None:
    first, second = keys
    now = [1000.0]
    status = [200]
    document = {"keys": [_jwk(first)]}
    calls = []

    def jwks(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        time.sleep(0.01)
        return httpx.Response(status[0], json=document)

    verifier = auth.OidcVerifier(CONFIG, transport=httpx.MockTransport(jwks), clock=lambda: now[0])
    with ThreadPoolExecutor(max_workers=8) as workers:
        assert all(workers.map(lambda _: verifier.authorized(_headers(_token(first))), range(8)))
    assert len(calls) == 1
    rotated = _token(second, kid="second")
    for _ in range(3):
        assert not verifier.authorized(_headers(rotated))
    assert len(calls) == 1
    now[0] += auth.JWKS_REFRESH_SECONDS + 1
    document["keys"] = [_jwk(second, "second")]
    assert verifier.authorized(_headers(rotated))
    assert len(calls) == 2
    assert not verifier.authorized(_headers(_token(first)))
    now[0] += auth.JWKS_CACHE_SECONDS + 1
    status[0] = 503
    assert not verifier.authorized(_headers(rotated))
    assert not verifier.authorized(_headers(rotated))
    assert len(calls) == 3
    now[0] += auth.JWKS_REFRESH_SECONDS + 1
    status[0] = 200
    assert verifier.authorized(_headers(rotated))
    assert len(calls) == 4


@pytest.mark.parametrize("count", [35, auth.MAX_JWKS_KEYS])
def test_oidc_selects_last_key_from_large_rotation_history(keys: tuple[Any, Any], count: int) -> None:
    current, previous = keys
    document = {"keys": [_jwk(previous, f"previous-{number}") for number in range(count - 1)]
                       + [_jwk(current, "current")]}
    assert len(json.dumps(document).encode()) < auth.MAX_JWKS_BYTES
    verifier = auth.OidcVerifier(CONFIG, transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json=document),
    ))
    assert verifier.authorized(_headers(_token(current, kid="current")))


def test_oidc_rejects_redirects_oversized_and_invalid_jwks(keys: tuple[Any, Any]) -> None:
    key = keys[0]
    replies = [
        lambda: httpx.Response(302, headers={"Location": "https://other.example/keys"}),
        lambda: httpx.Response(200, content=b" " * (auth.MAX_JWKS_BYTES + 1)),
        lambda: httpx.Response(200, json={"keys": [_jwk(key, f"key-{i}") for i in range(auth.MAX_JWKS_KEYS + 1)]}),
        lambda: httpx.Response(200, json={"keys": [_jwk(key), _jwk(key)]}),
        lambda: httpx.Response(200, json={"keys": [{**_jwk(key), "use": "enc"}]}),
        lambda: httpx.Response(200, content=b"not JSON"),
    ]
    for reply in replies:
        requests = []

        def jwks(request: httpx.Request, requests: Any = requests, reply: Any = reply) -> httpx.Response:
            requests.append(request)
            return reply()

        verifier = auth.OidcVerifier(CONFIG, transport=httpx.MockTransport(jwks))
        assert not verifier.authorized(_headers(_token(key)))
        assert len(requests) == 1


def test_oidc_rejects_jwks_after_overall_fetch_deadline(
    keys: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth, "JWKS_FETCH_SECONDS", 0.01)

    def slow_jwks(_request: httpx.Request) -> httpx.Response:
        time.sleep(0.02)
        return httpx.Response(200, json={"keys": [_jwk(keys[0])]})

    verifier = auth.OidcVerifier(CONFIG, transport=httpx.MockTransport(slow_jwks))
    assert not verifier.authorized(_headers(_token(keys[0])))


class _Runtime:
    def __init__(self) -> None:
        from zont_analyzer.config import AppConfig

        self.config = AppConfig()
        self.db = self

    def close(self) -> None:
        return


@pytest.fixture
def server(keys: tuple[Any, Any]) -> Any:
    authorization = "Basic " + base64.b64encode(b"fixture:secret").decode()
    config = RuntimeConfig("dev", 0, 3, "fixture", authorization, oidc=CONFIG)
    instance = CloudServer(("127.0.0.1", 0), config, None, runtime_factory=_Runtime)
    instance.tunnel = type("Tunnel", (), {"ready": lambda _: True})()
    instance.oidc_verifier = auth.OidcVerifier(CONFIG, transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"keys": [_jwk(keys[0])]}),
    ))
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=2)


def _request(server: CloudServer, method: str, path: str, headers: dict[str, str],
             body: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
    try:
        connection.request(method, path, body, {"Host": "app.example", **headers})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_oidc_http_boundary_disables_legacy_auth_and_keeps_basic_operations(
    server: CloudServer, keys: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    monkeypatch.setattr(server, "runtime_factory", lambda: calls.append(True) or _Runtime())
    monkeypatch.setattr("zont_analyzer.cloud.site.serve", lambda *_: (200, b"report", "text/html"))
    legacy_cookie = f"{auth.COOKIE_NAME}={auth.issue_session(server.config.authorization)}"
    basic = {"Authorization": server.config.authorization}
    for path in ("/", "/latest.html", "/reports.json", "/api/health", "/za/api/health"):
        for headers in (basic, {"Cookie": legacy_cookie}, {}):
            status, response_headers, body = _request(server, "GET", path, headers)
            assert status == 401 and json.loads(body) == {"error": "unauthorized"}
            assert "WWW-Authenticate" not in response_headers and "Set-Cookie" not in response_headers
    assert calls == []
    oidc = {"Cookie": f"{auth.OIDC_COOKIE_NAME}={_token(keys[0])}"}
    assert _request(server, "GET", "/latest.html", oidc)[0] == 200
    for path in ("/ready", "/diagnostics"):
        assert _request(server, "GET", path, oidc)[0] == 401
        assert _request(server, "GET", path, basic)[0] == 200
    for path in ("/login", "/logout"):
        for method in ("GET", "POST"):
            status, response_headers, _ = _request(server, method, path, basic)
            assert status == 404 and "Set-Cookie" not in response_headers
    assert _request(server, "POST", "/jobs/analytics", oidc, b"{}")[0] == 401
    monkeypatch.setattr("zont_analyzer.cloud.runtime.run_bounded", lambda *_: {"ok": True})
    assert _request(server, "POST", "/jobs/analytics", {**basic, "Content-Type": "application/json"}, b"{}")[0] == 200


def test_oidc_mutations_require_explicit_same_origin_before_opening_database(
    server: CloudServer, keys: tuple[Any, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened, queued = [], []
    monkeypatch.setattr(server, "runtime_factory", lambda: opened.append(True) or _Runtime())

    def enqueue(_runtime: Any, report_id: str, _question: str | None) -> dict[str, Any]:
        queued.append(report_id)
        return {"report_id": report_id, "status": "queued"}

    monkeypatch.setattr("zont_analyzer.cloud.user_jobs.enqueue_regeneration", enqueue)
    headers = {"Cookie": f"{auth.OIDC_COOKIE_NAME}={_token(keys[0])}", "Content-Type": "application/json"}
    for origin in ({}, {"Origin": "https://other.example"}, {"Origin": "null"},
                   {"Origin": "https://app.example", "Sec-Fetch-Site": "cross-site"}):
        assert _request(server, "POST", "/api/reports/daily-1/regenerate", {**headers, **origin}, b"{}")[0] == 403
    assert opened == queued == []
    assert _request(server, "POST", "/za/api/reports/daily-1/regenerate",
                    {**headers, "Origin": "https://app.example"}, b"{}")[0] == 202
    assert opened == [True] and queued == ["daily-1"]
