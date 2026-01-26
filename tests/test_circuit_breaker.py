"""Unit tests for Circuit Breaker implementation."""

import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from rmq_middleware.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerState,
    CircuitBreakerOpen,
    circuit_breaker_protected,
)


class TestCircuitBreakerStates:
    """Tests for circuit breaker state transitions."""

    @pytest.mark.asyncio
    async def test_initial_state_is_closed(self):
        """Circuit breaker should start in CLOSED state."""
        cb = CircuitBreaker()
        assert cb.state == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_allow_request_when_closed(self):
        """Requests should be allowed when circuit is CLOSED."""
        cb = CircuitBreaker()
        assert await cb.allow_request() is True

    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self):
        """Circuit should OPEN after reaching failure threshold."""
        cb = CircuitBreaker(failure_threshold=3, failure_window=10.0)

        # Record failures up to threshold
        await cb.record_failure()
        assert cb.state == CircuitBreakerState.CLOSED
        await cb.record_failure()
        assert cb.state == CircuitBreakerState.CLOSED
        await cb.record_failure()

        # Should now be open
        assert cb.state == CircuitBreakerState.OPEN

    @pytest.mark.asyncio
    async def test_rejects_requests_when_open(self):
        """Requests should be rejected when circuit is OPEN."""
        cb = CircuitBreaker(failure_threshold=2)
        await cb.record_failure()
        await cb.record_failure()

        assert cb.state == CircuitBreakerState.OPEN
        assert await cb.allow_request() is False

    @pytest.mark.asyncio
    async def test_transitions_to_half_open_after_recovery_timeout(self):
        """Circuit should transition to HALF_OPEN after recovery timeout."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.1)
        await cb.record_failure()
        await cb.record_failure()

        assert cb.state == CircuitBreakerState.OPEN

        # Wait for recovery timeout
        await asyncio.sleep(0.15)

        # Calling allow_request should trigger transition
        result = await cb.allow_request()
        assert cb.state == CircuitBreakerState.HALF_OPEN
        assert result is True

    @pytest.mark.asyncio
    async def test_closes_after_successful_half_open_request(self):
        """Circuit should CLOSE after successful request in HALF_OPEN state."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05, half_open_requests=1)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.1)
        await cb.allow_request()

        assert cb.state == CircuitBreakerState.HALF_OPEN

        await cb.record_success()
        assert cb.state == CircuitBreakerState.CLOSED

    @pytest.mark.asyncio
    async def test_reopens_after_failure_in_half_open(self):
        """Circuit should reopen after failure in HALF_OPEN state."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)
        await cb.record_failure()
        await cb.record_failure()
        await asyncio.sleep(0.1)
        await cb.allow_request()

        assert cb.state == CircuitBreakerState.HALF_OPEN

        await cb.record_failure()
        assert cb.state == CircuitBreakerState.OPEN


class TestCircuitBreakerMetrics:
    """Tests for circuit breaker metrics tracking."""

    @pytest.mark.asyncio
    async def test_metrics_tracking(self):
        """Circuit breaker should track metrics correctly."""
        cb = CircuitBreaker(failure_threshold=3)

        await cb.record_success()
        await cb.record_success()
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_failure()

        metrics = cb.metrics
        assert metrics["total_successes"] == 2
        assert metrics["total_failures"] == 3
        assert metrics["total_trips"] == 1  # Opened once
        assert metrics["state"] == "open"

    @pytest.mark.asyncio
    async def test_failure_count_property(self):
        """failure_count should return failures in current window."""
        cb = CircuitBreaker(failure_threshold=10)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.failure_count == 2

    @pytest.mark.asyncio
    async def test_total_trips_increments_on_open(self):
        """total_trips should increment each time circuit opens."""
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.05)

        # First trip
        await cb.record_failure()
        await cb.record_failure()
        assert cb.total_trips == 1

        # Recover
        await asyncio.sleep(0.1)
        await cb.allow_request()
        await cb.record_success()

        # Second trip
        await cb.record_failure()
        await cb.record_failure()
        assert cb.total_trips == 2


class TestCircuitBreakerReset:
    """Tests for circuit breaker reset functionality."""

    @pytest.mark.asyncio
    async def test_manual_reset(self):
        """reset() should restore circuit to CLOSED state."""
        cb = CircuitBreaker(failure_threshold=2)
        await cb.record_failure()
        await cb.record_failure()
        assert cb.state == CircuitBreakerState.OPEN

        await cb.reset()
        assert cb.state == CircuitBreakerState.CLOSED
        assert await cb.allow_request() is True


class TestCircuitBreakerDecorator:
    """Tests for the circuit_breaker_protected decorator."""

    @pytest.mark.asyncio
    async def test_decorator_allows_request_when_closed(self):
        """Decorated function should execute when circuit is CLOSED."""
        cb = CircuitBreaker()
        mock_func = AsyncMock(return_value="success")
        decorated = circuit_breaker_protected(cb)(mock_func)

        result = await decorated()
        assert result == "success"
        mock_func.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_decorator_raises_when_open(self):
        """Decorated function should raise when circuit is OPEN."""
        cb = CircuitBreaker(failure_threshold=2)
        await cb.record_failure()
        await cb.record_failure()

        mock_func = AsyncMock()
        decorated = circuit_breaker_protected(cb)(mock_func)

        with pytest.raises(CircuitBreakerOpen):
            await decorated()
        mock_func.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decorator_records_success(self):
        """Decorator should record success on successful execution."""
        cb = CircuitBreaker()
        mock_func = AsyncMock(return_value="ok")
        decorated = circuit_breaker_protected(cb)(mock_func)

        await decorated()
        assert cb.metrics["total_successes"] == 1

    @pytest.mark.asyncio
    async def test_decorator_records_failure_on_exception(self):
        """Decorator should record failure when function raises."""
        cb = CircuitBreaker()
        mock_func = AsyncMock(side_effect=ValueError("test error"))
        decorated = circuit_breaker_protected(cb)(mock_func)

        with pytest.raises(ValueError):
            await decorated()
        assert cb.metrics["total_failures"] == 1


class TestCircuitBreakerSlidingWindow:
    """Tests for sliding window failure tracking."""

    @pytest.mark.asyncio
    async def test_old_failures_expire(self):
        """Failures outside the window should not count."""
        cb = CircuitBreaker(failure_threshold=3, failure_window=0.1)

        await cb.record_failure()
        await cb.record_failure()
        assert cb.failure_count == 2

        # Wait for failures to expire
        await asyncio.sleep(0.15)
        assert cb.failure_count == 0

        # New failure should not trip the breaker
        await cb.record_failure()
        assert cb.state == CircuitBreakerState.CLOSED
