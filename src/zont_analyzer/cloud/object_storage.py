"""Bounded private Object Storage access with short-lived service-account tokens."""
from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote

import httpx

MAX_OBJECT_BYTES = 16 * 1024 * 1024
METADATA_TOKEN_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"


class StorageError(RuntimeError):
    """Deliberately excludes bucket names, tokens and provider response bodies."""


@dataclass(frozen=True)
class StoredObject:
    body: bytes
    content_type: str
    etag: str


class MetadataToken:
    def __init__(self, transport: httpx.BaseTransport | None = None) -> None:
        self.transport = transport
        self.token = ""
        self.expires = 0.0

    def __call__(self) -> str:
        if self.token and time.monotonic() < self.expires:
            return self.token
        try:
            with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False, timeout=2) as client:
                response = client.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"})
                if response.status_code != 200 or len(response.content) > 65536:
                    raise StorageError("metadata authentication failed")
                data = response.json()
            token = data["access_token"]
            ttl = int(data["expires_in"])
            if not isinstance(token, str) or not token or ttl <= 0 or "\n" in token or "\r" in token:
                raise ValueError("invalid token")
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise StorageError("metadata authentication failed") from None
        self.token = token
        self.expires = time.monotonic() + max(0, ttl - 60)
        return token


def valid_key(key: str) -> bool:
    return bool(key and len(key) <= 1024 and re.fullmatch(r"[a-zA-Z0-9._/-]+", key)
                and all(part not in {"", ".", ".."} for part in key.split("/")))


class ObjectStorage:
    """Only immutable PUT, bounded GET and HEAD; no anonymous or redirect fallback."""

    def __init__(self, bucket: str, prefix: str = "reports", *,
                 token: Callable[[], str] | None = None, transport: httpx.BaseTransport | None = None) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("invalid publication bucket")
        if not valid_key(prefix):
            raise ValueError("invalid publication prefix")
        self.bucket, self.prefix = bucket, prefix
        self.token = token or MetadataToken()
        self.transport = transport

    @classmethod
    def from_environment(cls) -> ObjectStorage:
        return cls(os.environ["CLOUD_PUBLICATION_BUCKET"], os.environ.get("CLOUD_PUBLICATION_PREFIX", "reports"))

    def _request(self, method: str, key: str, body: bytes = b"", content_type: str = "") -> StoredObject | None:
        if not valid_key(key):
            raise ValueError("invalid publication key")
        if len(body) > MAX_OBJECT_BYTES:
            raise StorageError("publication object exceeds byte limit")
        url = f"https://storage.yandexcloud.net/{self.bucket}/{quote(self.prefix + '/' + key, safe='/')}"
        headers = {"Authorization": "Bearer " + self.token(), "Accept-Encoding": "identity"}
        if method == "PUT":
            headers.update({"Content-Type": content_type, "If-None-Match": "*",
                            "Cache-Control": "private, no-store"})
        try:
            with (
                httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False, timeout=10) as client,
                client.stream(method, url, headers=headers, content=body) as response,
            ):
                if response.status_code == 404 and method in {"GET", "HEAD"}:
                    return None
                if response.status_code == 412 and method == "PUT":
                    # An identical retry is safe; a conflicting immutable object is not.
                    conflict = True
                elif response.status_code not in {200, 201, 204}:
                    raise StorageError("object storage request failed")
                else:
                    conflict = False
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_OBJECT_BYTES:
                        raise StorageError("publication object exceeds byte limit")
                result = StoredObject(bytes(data), response.headers.get("content-type", "application/octet-stream"),
                                      response.headers.get("etag", ""))
        except httpx.HTTPError:
            raise StorageError("object storage request failed") from None
        if conflict:
            existing = self.get(key)
            if existing is None or hashlib.sha256(existing.body).digest() != hashlib.sha256(body).digest():
                raise StorageError("immutable publication object conflict")
            return existing
        return result

    def get(self, key: str) -> StoredObject | None:
        return self._request("GET", key)

    def head(self, key: str) -> str:
        value = self._request("HEAD", key)
        return value.etag if value is not None else ""

    def put(self, key: str, body: bytes, content_type: str) -> str:
        result = self._request("PUT", key, body, content_type)
        assert result is not None
        return result.etag
