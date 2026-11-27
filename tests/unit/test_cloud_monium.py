from __future__ import annotations

import httpx
import pytest
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
    ExportLogsServiceResponse,
)

from zont_analyzer.cloud import monium


def test_exports_only_structured_job_attributes_with_metadata_iam_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received = []

    def respond(request: httpx.Request) -> httpx.Response:
        received.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"access_token": "fixture-iam-token"})
        return httpx.Response(200, content=b"")

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(monium.httpx, "Client", lambda **_: client)
    logger = monium.MoniumLogs("fixture-project", "dev", "a" * 40)
    monium._send(logger.project, logger.payload({
        "level": "INFO", "message": "cloud job", "event": "cloud_job",
        "job_id": "fixture-job-id", "status": "ok", "operation": "analytics",
        "unlisted": "private request details",
    }))

    metadata, ingest = received
    assert str(metadata.url) == monium._METADATA_URL
    assert metadata.headers["Metadata-Flavor"] == "Google"
    assert str(ingest.url) == monium._LOGS_URL
    assert ingest.headers["Authorization"] == "Bearer fixture-iam-token"
    assert ingest.headers["x-monium-project"] == "fixture-project"
    assert ingest.headers["x-monium-service"] == "zont-cloud-application"
    assert ingest.headers["Content-Type"] == "application/x-protobuf"
    resource = ExportLogsServiceRequest.FromString(ingest.content).resource_logs[0]
    assert {attribute.key: attribute.value.string_value for attribute in resource.resource.attributes} == {
        "service.name": "zont-cloud-application",
        "deployment.environment.name": "dev",
        "service.version": "a" * 40,
    }
    record = resource.scope_logs[0].log_records[0]
    assert record.body.string_value == "cloud job"
    assert {attribute.key: attribute.value.string_value for attribute in record.attributes} == {
        "event": "cloud_job", "job_id": "fixture-job-id", "status": "ok", "operation": "analytics",
    }


@pytest.mark.parametrize(("status", "body"), [
    (302, b""),
    (204, b""),
    (200, b"invalid protobuf"),
    (200, ExportLogsServiceResponse(partial_success={"rejected_log_records": 1}).SerializeToString()),
])
def test_rejects_invalid_otlp_acknowledgement(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"access_token": "fixture-iam-token"})
        return httpx.Response(status, content=body)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(monium.httpx, "Client", lambda **_: client)
    with pytest.raises(SystemExit) as error:
        monium._export("fixture-project", b"fixture")
    assert error.value.code == 1
