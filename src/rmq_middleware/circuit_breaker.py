"""Circuit Breaker pattern implementation for AMQP operations.

Implements the Circuit Breaker pattern to protect the system from cascade failures
when RabbitMQ becomes unavailable. The circuit breaker has three states:
- CLOSED: Normal operation, requests pass through
- OPEN: Requests fail immediately, protecting the broker from overload
- HALF_OPEN: Testing if the service has recovered

Based on ADR-002 from ADD Iteration 1.
"""

import asyncio
import time
from collections import deque
from enum import Enum
from functools import wraps
from typing import Any, Callable, TypeVar

from loguru import logger

from rmq_middleware.config import Settings


class CircuitBreakerState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpen(Exception):
    """Raised when circuit breaker is open and request is rejected."""

    def __init__(
        self,
        message: str = "Circuit breaker is OPEN",
        state: CircuitBreakerState = CircuitBreakerState.OPEN,
    ):
        self.state = state
        super().__init__(message)


class CircuitBreaker:
    """Circuit Breaker implementation with sliding window error tracking.

    Attributes:
        state: Current state of the circuit breaker
        failure_threshold: Number of failures before opening the circuit
        failure_window: Time window for counting failures (seconds)
        recovery_timeout: Time the circuit stays open before testing (seconds)
        half_open_requests: Number of test requests in half-open state
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        failure_window: float = 10.0,
        recovery_timeout: float = 30.0,
        half_open_requests: int = 1,
        settings: Settings | None = None,
    ):
        """Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures to trigger open state
            failure_window: Sliding window in seconds for failure counting
            recovery_timeout: Time in seconds before transitioning to half-open
            half_open_requests: Number of test requests allowed in half-open state
            settings: Optional settings to override defaults
        """
        if settings:
            failure_threshold = settings.cb_failure_threshold
            failure_window = settings.cb_failure_window_seconds
            recovery_timeout = settings.cb_recovery_timeout
            half_open_requests = settings.cb_half_open_requests

        self._failure_threshold = failure_threshold
        self._failure_window = failure_window
        self._recovery_timeout = recovery_timeout
        self._half_open_requests = half_open_requests

        # State tracking
        self._state = CircuitBreakerState.CLOSED
        self._failures: deque[float] = deque()  # Timestamps of failures
        self._last_failure_time: float | None = None
        self._opened_at: float | None = None
        self._half_open_successes = 0
        self._half_open_failures = 0

        # Metrics tracking
        self._total_trips = 0
        self._total_successes = 0
        self._total_failures = 0

        # Thread safety
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitBreakerState:
        """Current state of the circuit breaker."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Number of failures in the current window."""
        self._clean_old_failures()
        return len(self._failures)

    @property
    def total_trips(self) -> int:
        """Total number of times the circuit has opened."""
        return self._total_trips

    @property
    def metrics(self) -> dict[str, Any]:
        """Get circuit breaker metrics."""
        return {
            "state": self._state.value,
            "failure_count": self.failure_count,
            "total_trips": self._total_trips,
            "total_successes": self._total_successes,
            "total_failures": self._total_failures,
        }

    def _clean_old_failures(self) -> None:
        """Remove failures outside the sliding window."""
        now = time.monotonic()
        cutoff = now - self._failure_window
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _should_transition_to_half_open(self) -> bool:
        """Check if circuit should transition from open to half-open."""
        if self._state != CircuitBreakerState.OPEN:
            return False
        if self._opened_at is None:
            return False
        return time.monotonic() - self._opened_at >= self._recovery_timeout

    async def allow_request(self) -> bool:
        """Check if a request is allowed to proceed.

        Returns:
            True if request should proceed, False if circuit is open
        """
        async with self._lock:
            # Check for state transition
            if self._should_transition_to_half_open():
                self._state = CircuitBreakerState.HALF_OPEN
                self._half_open_successes = 0
                self._half_open_failures = 0
                logger.info(
                    "Circuit breaker transitioning to HALF_OPEN",
                    recovery_timeout=self._recovery_timeout,
                )

            if self._state == CircuitBreakerState.CLOSED:
                return True

            if self._state == CircuitBreakerState.HALF_OPEN:
                # Allow limited requests in half-open state
                current_requests = self._half_open_successes + self._half_open_failures
                if current_requests < self._half_open_requests:
                    return True
                return False

            # OPEN state
            return False

    async def record_success(self) -> None:
        """Record a successful operation."""
        async with self._lock:
            self._total_successes += 1

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._half_open_successes += 1
                if self._half_open_successes >= self._half_open_requests:
                    self._state = CircuitBreakerState.CLOSED
                    self._failures.clear()
                    self._opened_at = None
                    logger.info("Circuit breaker CLOSED after successful recovery")

    async def record_failure(self) -> None:
        """Record a failed operation."""
        async with self._lock:
            now = time.monotonic()
            self._total_failures += 1
            self._last_failure_time = now

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._half_open_failures += 1
                # Any failure in half-open reopens the circuit
                self._state = CircuitBreakerState.OPEN
                self._opened_at = now
                self._total_trips += 1
                logger.warning(
                    "Circuit breaker REOPENED from HALF_OPEN after failure",
                    recovery_timeout=self._recovery_timeout,
                )
                return

            if self._state == CircuitBreakerState.CLOSED:
                self._failures.append(now)
                self._clean_old_failures()

                if len(self._failures) >= self._failure_threshold:
                    self._state = CircuitBreakerState.OPEN
                    self._opened_at = now
                    self._total_trips += 1
                    logger.warning(
                        "Circuit breaker OPENED",
                        failure_count=len(self._failures),
                        threshold=self._failure_threshold,
                        window_seconds=self._failure_window,
                        recovery_timeout=self._recovery_timeout,
                    )

    async def reset(self) -> None:
        """Manually reset the circuit breaker to closed state."""
        async with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._failures.clear()
            self._opened_at = None
            self._half_open_successes = 0
            self._half_open_failures = 0
            logger.info("Circuit breaker manually reset to CLOSED")


# Type variable for generic decorator
F = TypeVar("F", bound=Callable[..., Any])


def circuit_breaker_protected(cb: CircuitBreaker) -> Callable[[F], F]:
    """Decorator to protect async functions with a circuit breaker.

    Usage:
        @circuit_breaker_protected(my_circuit_breaker)
        async def my_operation():
            # ... operation that might fail

    Args:
        cb: CircuitBreaker instance to use

    Returns:
        Decorated function that respects circuit breaker state
    """

    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not await cb.allow_request():
                raise CircuitBreakerOpen(
                    f"Circuit breaker is {cb.state.value} - request rejected",
                    state=cb.state,
                )

            try:
                result = await func(*args, **kwargs)
                await cb.record_success()
                return result
            except CircuitBreakerOpen:
                # Don't record CB exceptions as failures
                raise
            except Exception:
                await cb.record_failure()
                raise

        return wrapper  # type: ignore

    return decorator
