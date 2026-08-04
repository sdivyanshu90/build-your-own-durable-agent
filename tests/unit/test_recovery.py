import pytest

from durable_agent.domain.errors import ProviderRetryableError
from durable_agent.providers.fakes import DeterministicClock
from durable_agent.recovery import CircuitBreaker


def test_circuit_breaker_opens_and_half_opens() -> None:
    clock = DeterministicClock()
    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=10, clock=clock)
    assert breaker.allow()
    breaker.failure()
    assert not breaker.is_open
    breaker.failure()
    assert breaker.is_open
    try:
        breaker.before_call()
    except ProviderRetryableError:
        pass
    else:
        raise AssertionError("open circuit accepted a call")
    clock.advance(10)
    breaker.before_call()
    breaker.success()
    assert not breaker.is_open


def test_circuit_breaker_rejects_invalid_limits() -> None:
    clock = DeterministicClock()
    with pytest.raises(ValueError, match="positive"):
        CircuitBreaker(failure_threshold=0, clock=clock)
    with pytest.raises(ValueError, match="positive"):
        CircuitBreaker(recovery_seconds=0, clock=clock)
