"""Sample service used by the durable-agent demonstration."""

from sample_service.client import NotificationClient
from sample_service.config import ServiceConfig

__all__ = ["NotificationClient", "ServiceConfig"]
