import pytest
from pydantic import ValidationError

from durable_agent.domain.retry import RetryPolicy


def test_retry_is_bounded_and_deterministic() -> None:
    policy = RetryPolicy(
        maximum_attempts=3,
        base_delay_seconds=1,
        maximum_delay_seconds=5,
        jitter_ratio=0.2,
    )
    first = policy.delay_for(2, jitter_key="run-1")
    assert first == policy.delay_for(2, jitter_key="run-1")
    assert 1.6 <= first <= 2.4
    assert policy.should_retry(1, retryable=True)
    assert not policy.should_retry(3, retryable=True)
    assert not policy.should_retry(1, retryable=False)


def test_backoff_caps_and_validates_attempts() -> None:
    policy = RetryPolicy(base_delay_seconds=2, maximum_delay_seconds=3, jitter_ratio=0)
    assert policy.delay_for(10) == 3
    with pytest.raises(ValueError, match="at least one"):
        policy.delay_for(0)
    with pytest.raises(ValidationError, match="at least base"):
        RetryPolicy(base_delay_seconds=2, maximum_delay_seconds=1)
