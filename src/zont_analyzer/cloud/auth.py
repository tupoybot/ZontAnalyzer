"""Stateless browser sessions within the existing cloud Basic Auth perimeter."""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

COOKIE_NAME = "__Host-zont_session"
SESSION_SECONDS = 12 * 60 * 60
MAX_COOKIE_BYTES = 4096
MAX_TOKEN_BYTES = 512
_TOKEN = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


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
