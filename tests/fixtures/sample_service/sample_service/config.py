"""Configuration for the sample notification service."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceConfig:
    """User-visible service settings."""

    retry_delay_seconds: float = 0.01

    def __post_init__(self) -> None:
        if self.retry_delay_seconds < 0:
            raise ValueError("retry delay cannot be negative")
