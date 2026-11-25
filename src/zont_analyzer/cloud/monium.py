"""Bounded OTLP export for the cloud runtime's explicit structured events."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from typing import cast

import httpx
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)

_SERVICE = "zont-cloud-application"
_METADATA_URL = "http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token"
_LOGS_URL = "https://ingest.monium.yandex.cloud/otlp/v1/logs"
_MAX_RESPONSE_BYTES = 16_384


def _request(client: httpx.Client, method: str, url: str, headers: dict[str, str], body: bytes = b"") -> bytes:
    with client.stream(method, url, headers=headers, content=body) as response:
        if response.status_code != 200:
            raise RuntimeError("Monium request failed")
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=4096):
            data.extend(chunk)
            if len(data) > _MAX_RESPONSE_BYTES:
                raise ValueError("Monium response exceeds byte limit")
        return bytes(data)


def _send(project: str, body: bytes) -> None:
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=1.0) as client:
        metadata = _request(client, "GET", _METADATA_URL, {"Metadata-Flavor": "Google"})
        token = json.loads(metadata)["access_token"]
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,8192}", token):
            raise ValueError("invalid metadata token")
        result = _request(client, "POST", _LOGS_URL, {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-protobuf",
            "x-monium-project": project,
            "x-monium-service": _SERVICE,
        }, body)
    response = ExportLogsServiceResponse.FromString(result)
    if response.partial_success.rejected_log_records:
        raise RuntimeError("Monium rejected records")


def _export(project: str, body: bytes) -> None:
    try:
        _send(project, body)
    except Exception:
        # Transport errors may include request details. Return only the exit status.
        sys.exit(1)


class MoniumLogs:
    def __init__(self, project: str, environment: str, revision: str) -> None:
        self.project = project
        self.environment = environment
        self.revision = revision
        self._slot = threading.BoundedSemaphore(1)

    @classmethod
    def from_environment(cls) -> MoniumLogs | None:
        project = os.environ.get("MONIUM_PROJECT", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,256}", project):
            return None
        return cls(project, os.environ.get("CLOUD_ENVIRONMENT", ""), os.environ.get("CLOUD_REVISION", ""))

    def payload(self, event: Mapping[str, str]) -> bytes:
        request = ExportLogsServiceRequest()
        resource = request.resource_logs.add()
        resource.resource.attributes.add(key="service.name").value.string_value = _SERVICE
        if self.environment in ("dev", "pilot"):
            resource.resource.attributes.add(key="deployment.environment.name").value.string_value = self.environment
        if re.fullmatch(r"[0-9a-f]{40}", self.revision):
            resource.resource.attributes.add(key="service.version").value.string_value = self.revision
        scope = resource.scope_logs.add()
        scope.scope.name = "zont-analyzer"
        level = event["level"]
        record = scope.log_records.add(time_unix_nano=time.time_ns(), severity_number=9 if level == "INFO" else 17,
                                       severity_text=level)
        record.body.string_value = event["message"]
        for key in ("event", "job_id", "smoke_id", "operation", "status", "error_type", "original_error_type"):
            if key in event:
                record.attributes.add(key=key).value.string_value = event[key]
        return cast(bytes, request.SerializeToString())

    def send(self, event: Mapping[str, str]) -> bool:
        if not self._slot.acquire(blocking=False):
            return False
        try:
            process = multiprocessing.get_context("spawn").Process(
                target=_export, args=(self.project, self.payload(event)),
            )
            process.start()
            try:
                process.join(2)
                return process.exitcode == 0
            finally:
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
        finally:
            self._slot.release()
