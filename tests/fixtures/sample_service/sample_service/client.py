"""Notification client with an intentionally non-configurable retry bound."""

from __future__ import annotations

import time

from sample_service.config import ServiceConfig
from sample_service.transport import TransientTransportError, Transport


class NotificationClient:
    """Send a message, retrying transient transport failures."""

    def __init__(self, transport: Transport, config: ServiceConfig | None = None) -> None:
        self._transport = transport
        self._config = config or ServiceConfig()

    def notify(self, message: str) -> str:
        last_error: TransientTransportError | None = None
        for _attempt in range(3):
            try:
                return self._transport.send(message)
            except TransientTransportError as error:
                last_error = error
                time.sleep(self._config.retry_delay_seconds)
        if last_error is None:
            raise RuntimeError("retry loop completed without a result")
        raise last_error
