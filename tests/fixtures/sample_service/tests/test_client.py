from sample_service import NotificationClient, ServiceConfig
from sample_service.transport import ScriptedTransport, TransientTransportError


def test_success_without_retry() -> None:
    transport = ScriptedTransport(["sent"])
    client = NotificationClient(transport, ServiceConfig(retry_delay_seconds=0))
    assert client.notify("hello") == "sent:hello"
    assert transport.calls == 1


def test_historical_three_attempt_limit() -> None:
    transport = ScriptedTransport(
        [TransientTransportError("one"), TransientTransportError("two"), "sent"]
    )
    client = NotificationClient(transport, ServiceConfig(retry_delay_seconds=0))
    assert client.notify("hello") == "sent:hello"
    assert transport.calls == 3
