"""Health endpoint behaviour.

These run without AWS, without network, and without a container — which is the
point of keeping the app free of import-time I/O.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from guardrail_service import observability
from guardrail_service.app import REQUEST_ID_HEADER, app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    """Capture log output through Powertools' real JSON formatter.

    pytest's logging plugin intercepts records before they reach the formatter, so
    reading stdout shows plain text rather than the structured JSON that actually
    ships to CloudWatch. Attaching our own handler with the registered formatter
    tests the format that production really emits.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(observability.logger.registered_formatter)

    underlying = logging.getLogger("guardrail")
    underlying.addHandler(handler)
    try:
        yield stream
    finally:
        underlying.removeHandler(handler)


def _access_log_lines(stream: io.StringIO) -> list[dict[str, object]]:
    """Parse the structured access-log entries out of captured output."""
    lines = []
    for raw in stream.getvalue().splitlines():
        if not raw.startswith("{"):
            continue
        entry = json.loads(raw)
        if entry.get("message") == "request":
            lines.append(entry)
    return lines


def test_healthz_reports_ok(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["uptime_seconds"] >= 0


def test_healthz_does_not_require_dependencies(client: TestClient) -> None:
    """Liveness must not depend on storage, or a transient DynamoDB outage would
    get healthy processes restarted and deepen the incident."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert "dependencies" not in response.json()


def test_readyz_enumerates_dependencies(client: TestClient) -> None:
    response = client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    names = {dep["name"] for dep in body["dependencies"]}
    assert names == {"audit_table", "decisions_table", "policies_table"}


def test_version_is_reported(client: TestClient) -> None:
    response = client.get("/version")

    assert response.status_code == 200
    assert response.json()["service"] == "guardrail"


def test_request_id_is_echoed_when_supplied(client: TestClient) -> None:
    """A caller-supplied id must survive, so one trace spans agent, SDK, and service."""
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: "trace-abc-123"})

    assert response.headers[REQUEST_ID_HEADER] == "trace-abc-123"


def test_request_id_is_generated_when_absent(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.headers.get(REQUEST_ID_HEADER)


def test_openapi_schema_is_served(client: TestClient) -> None:
    """The OpenAPI document is a deliverable: the SDK and console are generated from it."""
    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert "/healthz" in response.json()["paths"]


def test_access_log_carries_the_request_id(client: TestClient, log_stream: io.StringIO) -> None:
    """The access log line must contain request_id.

    Powertools keys are cleared after the response is logged, not before -- clearing
    them first strips the id from the very line it exists to correlate.
    """
    client.get("/version", headers={REQUEST_ID_HEADER: "corr-42"})

    entries = _access_log_lines(log_stream)

    assert entries, f"no access log captured; got: {log_stream.getvalue()[:400]}"
    assert entries[-1]["request_id"] == "corr-42"


def test_request_id_does_not_leak_between_requests(
    client: TestClient, log_stream: io.StringIO
) -> None:
    """Lambda reuses warm containers, so a leaked key would mislabel later requests."""
    client.get("/version", headers={REQUEST_ID_HEADER: "first"})
    client.get("/version", headers={REQUEST_ID_HEADER: "second"})

    entries = _access_log_lines(log_stream)

    assert [entry["request_id"] for entry in entries[-2:]] == ["first", "second"]


def test_access_log_records_status_and_duration(
    client: TestClient, log_stream: io.StringIO
) -> None:
    """Latency and status must be queryable in Logs Insights, since the 10-metric
    CloudWatch budget cannot afford a per-endpoint metric."""
    client.get("/healthz")

    entry = _access_log_lines(log_stream)[-1]

    assert entry["status_code"] == 200
    assert entry["path"] == "/healthz"
    assert isinstance(entry["duration_ms"], (int, float))
