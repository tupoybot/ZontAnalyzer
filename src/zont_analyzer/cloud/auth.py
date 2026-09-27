"""Bounded browser authentication and legacy stateless sessions."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt

COOKIE_NAME = "__Host-zont_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_COOKIE_BYTES = 4096
MAX_TOKEN_BYTES = 512
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
MAX_OIDC_TOKEN_BYTES = 8192
MAX_JWKS_BYTES = 65536
MAX_JWKS_KEYS = 128
JWKS_CACHE_SECONDS = 300
JWKS_REFRESH_SECONDS = 30
JWKS_FETCH_SECONDS = 2.0
_JWT = re.compile(r"\A[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\Z")
_KEY_ID = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    audience: str
    jwks_uri: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.audience):
            raise ValueError("invalid CLOUD_OIDC_AUDIENCE")
        if self.issuer not in {"https://auth.yandex.cloud", "https://auth.yandex.cloud/oauth/" + self.audience}:
            raise ValueError("invalid CLOUD_OIDC_ISSUER")
        if self.jwks_uri != "https://auth.yandex.cloud/oauth/jwks/keys":
            raise ValueError("invalid CLOUD_OIDC_JWKS_URI")

    @classmethod
    def from_environment(cls) -> OidcConfig | None:
        values = [os.environ.get(name) for name in
                  ("CLOUD_OIDC_ISSUER", "CLOUD_OIDC_AUDIENCE", "CLOUD_OIDC_JWKS_URI")]
        if all(value is None for value in values):
            return None
        if not all(values):
            raise ValueError("complete CLOUD_OIDC configuration is required")
        return cls(str(values[0]), str(values[1]), str(values[2]))


def _bearer_token(headers: Any) -> str | None:
    values = headers.get_all("Authorization", [])
    if len(values) != 1 or len(values[0]) > MAX_OIDC_TOKEN_BYTES + len("Bearer "):
        return None
    scheme, separator, token = values[0].partition(" ")
    return token if separator and scheme.lower() == "bearer" else None


class OidcVerifier:
    """Verify Identity Hub JWTs against a bounded, process-local public-key cache."""

    def __init__(self, config: OidcConfig, *, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires = 0.0
        self._next_fetch = 0.0

    def authorized(self, headers: Any) -> bool:
        token = _bearer_token(headers)
        if not token or len(token) > MAX_OIDC_TOKEN_BYTES or _JWT.fullmatch(token) is None:
            return False
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (header.get("alg") != "RS256" or not isinstance(kid, str) or _KEY_ID.fullmatch(kid) is None
                    or any(name in header for name in ("crit", "jku", "jwk", "x5u"))):
                return False
            key = self._key(kid)
            if key is None:
                return False
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"], issuer=self.config.issuer,
                audience=self.config.audience, leeway=30,
                options={"require": ["iss", "aud", "exp", "iat", "sub"], "strict_aud": True},
            )
            issued, expiry = claims["iat"], claims["exp"]
            return (type(issued) is int and type(expiry) is int and 0 <= issued < expiry
                    and issued <= time.time() + 30 and expiry > time.time()
                    and isinstance(claims["sub"], str) and 0 < len(claims["sub"]) <= 256)
        except (jwt.PyJWTError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            return False

    def _key(self, kid: str) -> jwt.PyJWK | None:
        if not self._lock.acquire(timeout=JWKS_FETCH_SECONDS + 1):
            return None
        try:
            now = self._clock()
            if now < self._expires and kid in self._keys:
                return self._keys[kid]
            if now < self._next_fetch:
                return None
            self._next_fetch = now + JWKS_REFRESH_SECONDS
            try:
                keys = self._fetch_keys()
            except (httpx.HTTPError, jwt.PyJWTError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
                return None
            self._keys = keys
            self._expires = self._clock() + JWKS_CACHE_SECONDS
            return keys.get(kid)
        finally:
            self._lock.release()

    def _fetch_keys(self) -> dict[str, jwt.PyJWK]:
        deadline = time.monotonic() + JWKS_FETCH_SECONDS
        with (
            httpx.Client(transport=self._transport, trust_env=False, follow_redirects=False,
                         timeout=httpx.Timeout(0.75), headers={"Accept-Encoding": "identity"}) as client,
            client.stream("GET", self.config.jwks_uri) as response,
        ):
            if response.status_code != 200 or response.headers.get("Content-Encoding", "identity") != "identity":
                raise ValueError("OIDC public keys unavailable")
            raw = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline or len(raw) + len(chunk) > MAX_JWKS_BYTES:
                    raise ValueError("OIDC public keys exceed bounds")
                raw.extend(chunk)
        if time.monotonic() >= deadline:
            raise ValueError("OIDC public keys exceed bounds")
        value = json.loads(raw)
        entries = value.get("keys") if isinstance(value, dict) else None
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_JWKS_KEYS:
            raise ValueError("invalid OIDC public keys")
        keys = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid OIDC public key")
            if entry.get("kty") != "RSA" or entry.get("use", "sig") != "sig" or entry.get("alg", "RS256") != "RS256":
                continue
            kid = entry.get("kid")
            if (not isinstance(kid, str) or _KEY_ID.fullmatch(kid) is None or kid in keys
                    or entry.get("key_ops", ["verify"]) != ["verify"]):
                raise ValueError("invalid OIDC public key")
            key = jwt.PyJWK.from_dict(entry, algorithm="RS256")
            if not 2048 <= key.key.key_size <= 8192:
                raise ValueError("invalid OIDC RSA key size")
            keys[kid] = key
        if not keys:
            raise ValueError("no OIDC signing keys")
        return keys


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


def same_origin(headers: Any, configured_origin: str | None) -> bool:
    """Require an explicit browser Origin; never trust forwarded-host headers."""
    if headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    supplied = headers.get("Origin")
    host = headers.get("Host", "")
    expected = configured_origin or "https://" + host
    return bool(supplied and _origin(supplied) is not None and _origin(supplied) == _origin(expected))


def credentials_match(username: str, password: str, authorization: str) -> bool:
    if not username or ":" in username or not password:
        return False
    submitted = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return hmac.compare_digest(submitted, authorization)


def parse_credentials(raw: bytes) -> tuple[str, str] | None:
    try:
        fields = parse_qs(raw.decode("utf-8", "strict"), keep_blank_values=True,
                          strict_parsing=True, max_num_fields=3)
    except (UnicodeDecodeError, ValueError):
        return None
    if set(fields) != {"username", "password"} or any(len(items) != 1 for items in fields.values()):
        return None
    return fields["username"][0], fields["password"][0]


def _key(authorization: str) -> bytes:
    return hmac.new(authorization.encode("ascii"), b"zont-cloud-browser-session-v1", hashlib.sha256).digest()


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    if _encode(raw) != value:
        raise ValueError("noncanonical session token")
    return raw


def issue_session(authorization: str, *, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    payload = json.dumps({"v": 1, "iat": issued, "exp": issued + SESSION_SECONDS,
                          "n": secrets.token_urlsafe(18)}, separators=(",", ":")).encode()
    encoded = _encode(payload)
    signature = _encode(hmac.new(_key(authorization), encoded.encode("ascii"), hashlib.sha256).digest())
    return encoded + "." + signature


def valid_session(token: str, authorization: str, *, now: int | None = None) -> bool:
    if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES or _TOKEN.fullmatch(token) is None:
        return False
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_key(authorization), encoded.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_decode(signature), expected):
            return False
        value = json.loads(_decode(encoded))
        current = int(time.time()) if now is None else now
        issued, expiry = value["iat"], value["exp"]
        return (value["v"] == 1 and type(issued) is int and type(expiry) is int
                and isinstance(value["n"], str) and 16 <= len(value["n"]) <= 64
                and expiry == issued + SESSION_SECONDS and issued - 60 <= current < expiry)
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        return False


def session_from_headers(headers: Any, authorization: str) -> bool:
    cookie_headers = headers.get_all("Cookie", [])
    if sum(len(value) for value in cookie_headers) > MAX_COOKIE_BYTES:
        return False
    sessions = []
    for header in cookie_headers:
        for field in header.split(";"):
            name, separator, value = field.strip().partition("=")
            if name == COOKIE_NAME:
                if not separator:
                    return False
                sessions.append(value)
    return len(sessions) == 1 and valid_session(sessions[0], authorization)


def session_cookie(token: str) -> str:
    return (f"{COOKIE_NAME}={token}; Max-Age={SESSION_SECONDS}; Path=/; "
            "Secure; HttpOnly; SameSite=Strict")


def clear_cookie() -> str:
    return (f"{COOKIE_NAME}=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT; "
            "Path=/; Secure; HttpOnly; SameSite=Strict")


def login_page(*, authenticated: bool = False, failed: bool = False) -> bytes:
    message = ("<p>Неверные данные для входа.</p>" if failed else "")
    form = ("<form method='post' action='/logout'><button type='submit'>Выйти</button></form>"
            if authenticated else
            "<form method='post' action='/login'>"
            "<label>Имя пользователя<input name='username' autocomplete='username' required></label>"
            "<label>Пароль<input name='password' type='password' autocomplete='current-password' required></label>"
            "<button type='submit'>Войти</button></form>")
    return ("<!doctype html><html lang='ru'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Вход</title><style>body{font:1rem system-ui;max-width:26rem;margin:3rem auto;padding:1rem}"
            "label{display:block;margin:1rem 0}input{display:block;width:100%;padding:.5rem}"
            "button{padding:.5rem 1rem}</style><h1>Вход</h1>" + message + form + "</html>").encode("utf-8")
