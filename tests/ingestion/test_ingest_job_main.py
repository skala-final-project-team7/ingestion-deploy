"""ingest job consumer 정책 단위 테스트 — 재시도/DLQ/파싱 검증."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.ingestion.workers import ingest_job_main as ingest_job_main
from app.ingestion.workers.ingestion_worker import IngestJobCommand


class _FakeMethod:
    def __init__(self, delivery_tag: int) -> None:
        self.delivery_tag = delivery_tag


class _FakeProperties:
    def __init__(
        self,
        *,
        headers: dict[str, object] | None = None,
        content_type: str | None = "application/json",
        delivery_mode: int = 2,
        content_encoding: str | None = None,
        correlation_id: str | None = None,
        reply_to: str | None = None,
    ) -> None:
        self.headers = headers
        self.content_type = content_type
        self.delivery_mode = delivery_mode
        self.content_encoding = content_encoding
        self.correlation_id = correlation_id
        self.reply_to = reply_to


class _FakeChannel:
    def __init__(self, bodies: list[tuple[_FakeMethod, _FakeProperties, bytes]], *, publish_fail: bool = False) -> None:
        self._bodies = list(bodies)
        self.publish_fail = publish_fail
        self.acked: list[int] = []
        self.rejected: list[tuple[int, bool]] = []
        self.published: list[dict[str, object]] = []
        self.qos_calls: list[int] = []
        self.consume_calls: list[tuple[str, bool]] = []

    def basic_qos(self, prefetch_count: int) -> None:
        self.qos_calls.append(prefetch_count)

    def consume(self, queue: str, auto_ack: bool = False):
        self.consume_calls.append((queue, auto_ack))
        yield from self._bodies

    def basic_ack(self, delivery_tag: int) -> None:
        self.acked.append(delivery_tag)

    def basic_reject(self, delivery_tag: int, requeue: bool) -> None:
        self.rejected.append((delivery_tag, requeue))

    def basic_publish(self, *, exchange: str, routing_key: str, body: bytes, properties: object) -> None:
        if self.publish_fail:
            raise RuntimeError("publish failed")
        self.published.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_get_retry_count_from_headers() -> None:
    assert ingest_job_main._get_retry_count(None) == 0
    assert ingest_job_main._get_retry_count({"x-retry-count": 0}) == 0
    assert ingest_job_main._get_retry_count({"x-retry-count": "2"}) == 2
    assert ingest_job_main._get_retry_count({"x-retry-count": -1}) == 0


def test_parse_message_requires_json_dict() -> None:
    assert ingest_job_main._parse_message(
        b'{"jobId":"job-1","adminUserId":"admin-1","mode":"full","requestedAt":"2026-06-12T00:00:00Z"}'
    )
    with pytest.raises(ValueError, match="malformed message body"):
        ingest_job_main._parse_message(b"{not-json}")
    with pytest.raises(ValueError, match="must be dict"):
        ingest_job_main._parse_message(b'["not", "dict"]')


def test_publish_retry_message_updates_retry_header() -> None:
    channel = _FakeChannel([])
    properties = _FakeProperties(headers={"x-retry-count": 0, "keep": "v"})
    ingest_job_main._publish_retry_message(
        channel,
        queue="lina.data-ingestion.ingest",
        body=b'{"jobId":"job-1"}',
        properties=properties,
        retry_count=3,
    )

    assert len(channel.published) == 1
    assert channel.published[0]["exchange"] == ""
    assert channel.published[0]["routing_key"] == "lina.data-ingestion.ingest"
    assert channel.published[0]["body"] == b'{"jobId":"job-1"}'
    published_properties = channel.published[0]["properties"]
    assert getattr(published_properties, "content_type", None) == "application/json"
    assert getattr(published_properties, "delivery_mode", None) == 2
    assert published_properties.headers == {"x-retry-count": 3, "keep": "v"}


def test_handle_failure_retries_when_retry_count_remaining(monkeypatch) -> None:
    channel = _FakeChannel([])
    method = _FakeMethod(7)
    sleeps: list[float] = []

    monkeypatch.setattr(ingest_job_main.time, "sleep", lambda seconds: sleeps.append(seconds))
    ingest_job_main._handle_failure(
        channel,
        queue="lina.data-ingestion.ingest",
        method=method,
        body=b"body",
        properties=_FakeProperties(),
        error=RuntimeError("downstream failed"),
    )

    assert sleeps == [ingest_job_main._RETRY_BACKOFF_SECONDS[0]]
    assert channel.published
    assert channel.acked == [7]
    assert channel.rejected == []
    assert len(channel.published) == 1
    assert (
        channel.published[0]["properties"].headers
        == {"x-retry-count": 1}
    )


def test_handle_failure_rejects_when_publish_fails_and_dlq_marked(monkeypatch) -> None:
    channel = _FakeChannel([], publish_fail=True)
    method = _FakeMethod(11)
    sleeps: list[float] = []

    monkeypatch.setattr(ingest_job_main.time, "sleep", lambda seconds: sleeps.append(seconds))
    ingest_job_main._handle_failure(
        channel,
        queue="lina.data-ingestion.ingest",
        method=method,
        body=b"body",
        properties=_FakeProperties(headers={}),
        error=RuntimeError("downstream failed"),
    )

    assert sleeps == [ingest_job_main._RETRY_BACKOFF_SECONDS[0]]
    assert channel.rejected == [(11, False)]
    assert channel.acked == []


def test_handle_failure_rejects_on_retry_exhaustion() -> None:
    channel = _FakeChannel([])
    method = _FakeMethod(3)

    ingest_job_main._handle_failure(
        channel,
        queue="lina.data-ingestion.ingest",
        method=method,
        body=b"body",
        properties=_FakeProperties(headers={"x-retry-count": 4}),
        error=RuntimeError("downstream failed"),
    )

    assert channel.rejected == [(3, False)]
    assert channel.acked == []
    assert channel.published == []


def test_consume_until_shutdown_acks_successful_message(monkeypatch) -> None:
    channel = _FakeChannel(
        [
            (
                _FakeMethod(101),
                _FakeProperties(),
                b'{"jobId":"job-1","adminUserId":"admin-1","mode":"full","requestedAt":"2026-06-12T00:00:00Z"}',
            )
        ]
    )
    connection = _FakeConnection()
    run_calls: list[dict[str, object]] = []
    handle_calls: list[dict[str, object]] = []

    def _fake_run(_deps: object, payload: dict[str, object], credential_lookup: object | None) -> None:
        run_calls.append(payload)

    monkeypatch.setattr(ingest_job_main, "build_ingest_deps", lambda settings: object())
    monkeypatch.setattr(ingest_job_main, "open_rabbitmq_channel", lambda settings: (connection, channel))
    monkeypatch.setattr(ingest_job_main, "run_ingest_job_from_payload", _fake_run)
    monkeypatch.setattr(
        ingest_job_main,
        "_handle_failure",
        lambda **kwargs: handle_calls.append(kwargs),  # type: ignore[misc]
    )

    ingest_job_main._consume_until_shutdown(Settings(ingest_job_queue="lina.data-ingestion.ingest"))

    assert run_calls == [
        {
            "jobId": "job-1",
            "adminUserId": "admin-1",
            "mode": "full",
            "requestedAt": "2026-06-12T00:00:00Z",
        }
    ]
    assert channel.acked == [101]
    assert handle_calls == []
    assert channel.rejected == []
    assert connection.closed is True


def test_consume_until_shutdown_passes_credential_lookup(monkeypatch) -> None:
    class _FakeDeps:
        @staticmethod
        def credential_lookup(admin_user_id: str) -> tuple[str, str]:
            return f"resolved-{admin_user_id}", "resolved-cloud"

    channel = _FakeChannel(
        [
            (
                _FakeMethod(404),
                _FakeProperties(),
                b'{"jobId":"job-1","adminUserId":"admin-1","mode":"full","requestedAt":"2026-06-12T00:00:00Z"}',
            )
        ]
    )
    connection = _FakeConnection()
    run_calls: list[tuple[dict[str, object], object | None]] = []

    def _fake_run(
        _deps: object, payload: dict[str, object], credential_lookup: object | None = None
    ) -> None:
        run_calls.append((payload, credential_lookup))

    monkeypatch.setattr(ingest_job_main, "build_ingest_deps", lambda settings: _FakeDeps())
    monkeypatch.setattr(ingest_job_main, "open_rabbitmq_channel", lambda settings: (connection, channel))
    monkeypatch.setattr(ingest_job_main, "run_ingest_job_from_payload", _fake_run)
    monkeypatch.setattr(
        ingest_job_main,
        "_handle_failure",
        lambda **kwargs: None,  # type: ignore[misc]
    )

    ingest_job_main._consume_until_shutdown(Settings(ingest_job_queue="lina.data-ingestion.ingest"))

    assert run_calls == [
        (
            {
                "jobId": "job-1",
                "adminUserId": "admin-1",
                "mode": "full",
                "requestedAt": "2026-06-12T00:00:00Z",
            },
            _FakeDeps.credential_lookup,
        )
    ]
    assert channel.acked == [404]
    assert connection.closed is True


def test_consume_until_shutdown_calls_failure_handler_on_worker_error(monkeypatch) -> None:
    channel = _FakeChannel(
        [
            (
                _FakeMethod(202),
                _FakeProperties(),
                b'{"jobId":"job-1","adminUserId":"admin-1","mode":"full","requestedAt":"2026-06-12T00:00:00Z"}',
            )
        ]
    )
    connection = _FakeConnection()
    handle_calls: list[dict[str, object]] = []

    def _fake_run(_deps: object, _payload: dict[str, object], credential_lookup: object | None = None) -> None:
        raise RuntimeError("downstream failed")

    monkeypatch.setattr(ingest_job_main, "build_ingest_deps", lambda settings: object())
    monkeypatch.setattr(ingest_job_main, "open_rabbitmq_channel", lambda settings: (connection, channel))
    monkeypatch.setattr(ingest_job_main, "run_ingest_job_from_payload", _fake_run)
    monkeypatch.setattr(
        ingest_job_main,
        "_handle_failure",
        lambda channel, queue, method, body, properties, error: handle_calls.append(
            {"queue": queue, "method": method, "error": error}
        ),
    )

    ingest_job_main._consume_until_shutdown(Settings(ingest_job_queue="lina.data-ingestion.ingest"))

    assert len(handle_calls) == 1
    assert handle_calls[0]["queue"] == "lina.data-ingestion.ingest"
    assert handle_calls[0]["method"].delivery_tag == 202
    assert handle_calls[0]["error"].args[0] == "downstream failed"
    assert channel.acked == []
    assert connection.closed is True


def test_consume_until_shutdown_calls_failure_handler_on_parse_error(monkeypatch) -> None:
    channel = _FakeChannel(
        [
            (
                _FakeMethod(303),
                _FakeProperties(),
                b"bad-json",
            )
        ]
    )
    connection = _FakeConnection()
    handle_calls: list[dict[str, object]] = []

    def _fake_run(_deps: object, _payload: dict[str, object], credential_lookup: object | None = None) -> None:
        raise AssertionError("run_ingest_job_from_payload should not be called for parse error")

    monkeypatch.setattr(ingest_job_main, "build_ingest_deps", lambda settings: object())
    monkeypatch.setattr(ingest_job_main, "open_rabbitmq_channel", lambda settings: (connection, channel))
    monkeypatch.setattr(ingest_job_main, "run_ingest_job_from_payload", _fake_run)
    monkeypatch.setattr(
        ingest_job_main,
        "_handle_failure",
        lambda channel, queue, method, body, properties, error: handle_calls.append(
            {"error": error, "method": method}
        ),
    )

    ingest_job_main._consume_until_shutdown(Settings(ingest_job_queue="lina.data-ingestion.ingest"))

    assert len(handle_calls) == 1
    assert handle_calls[0]["method"].delivery_tag == 303
    assert isinstance(handle_calls[0]["error"], ValueError)
    assert channel.acked == []
    assert connection.closed is True


def test_ingest_job_command_requires_required_fields() -> None:
    with pytest.raises(ValueError, match="missing"):
        IngestJobCommand.from_payload(
            {
                "adminUserId": "admin-1",
                "mode": "full",
                "requestedAt": "2026-06-12T00:00:00Z",
            }
        )
