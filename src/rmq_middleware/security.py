"""Security module for HTTP-to-AMQP Bridge.

Provides:
- API Key authentication
- Input validation for queue/exchange names
- Security headers middleware
- Rate limiting helpers
"""

import re
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# =============================================================================
# Security Configuration
# =============================================================================


class SecuritySettings(BaseSettings):
    """Security-related configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Rate Limiting
    rate_limit_enabled: bool = Field(
        default=False,
        description="Enable rate limiting",
    )
    rate_limit_requests: int = Field(
        default=100,
        ge=1,
        le=10000,
        description="Max requests per window",
    )
    rate_limit_window_seconds: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Rate limit window in seconds",
    )

    # Request Size Limits
    max_request_body_bytes: int = Field(
        default=1_048_576,  # 1 MB
        ge=1024,
        le=104_857_600,  # 100 MB max
        description="Maximum request body size in bytes",
    )

    # Input Validation
    max_name_length: int = Field(
        default=255,
        ge=1,
        le=1000,
        description="Maximum length for queue/exchange names",
    )


_security_settings: SecuritySettings | None = None


def get_security_settings() -> SecuritySettings:
    """Get cached security settings instance."""
    global _security_settings
    if _security_settings is None:
        _security_settings = SecuritySettings()
    return _security_settings


# =============================================================================
# Input Validation
# =============================================================================

# Valid characters for AMQP names: alphanumeric, dot, dash, underscore
AMQP_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")

# Reserved/dangerous patterns to reject
DANGEROUS_PATTERNS = [
    "..",  # Path traversal
    "//",  # Double slash
    "amq.",  # RabbitMQ internal prefix
]


class InputValidationError(Exception):
    """Raised when input validation fails."""

    pass


def validate_amqp_name(name: str, field_name: str = "name") -> str:
    """Validate AMQP resource name (exchange, queue, routing key).

    Args:
        name: The name to validate.
        field_name: Field name for error messages.

    Returns:
        The validated name.

    Raises:
        InputValidationError: If validation fails.
    """
    settings = get_security_settings()

    # Check length
    if not name:
        raise InputValidationError(f"{field_name} cannot be empty")

    if len(name) > settings.max_name_length:
        raise InputValidationError(
            f"{field_name} exceeds maximum length of {settings.max_name_length}"
        )

    # Check for dangerous patterns
    for pattern in DANGEROUS_PATTERNS:
        if pattern in name:
            raise InputValidationError(f"{field_name} contains forbidden pattern: '{pattern}'")

    # Check character set (allow # and * for routing keys)
    if not AMQP_NAME_PATTERN.match(name.replace("#", "").replace("*", "")):
        raise InputValidationError(
            f"{field_name} contains invalid characters. "
            f"Allowed: a-z, A-Z, 0-9, '.', '-', '_', '#', '*'"
        )

    return name


def validate_exchange_name(name: str) -> str:
    """Validate exchange name."""
    return validate_amqp_name(name, "exchange")


def validate_queue_name(name: str) -> str:
    """Validate queue name."""
    return validate_amqp_name(name, "queue")


def validate_routing_key(key: str) -> str:
    """Validate routing key."""
    return validate_amqp_name(key, "routing_key")


# =============================================================================
# Authentication (HTTP Basic)
# =============================================================================

security = HTTPBasic()


async def get_amqp_credentials(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> HTTPBasicCredentials:
    """Extract AMQP credentials from Basic Auth header.

    Returns:
        The credentials object containing username and password.
    """
    if not credentials.username or not credentials.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Username and password are required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials


# =============================================================================
# Rate Limiting (Simple In-Memory)
# =============================================================================


class RateLimiter:
    """Simple in-memory rate limiter using sliding window."""

    def __init__(self):
        self._requests: dict[str, list[float]] = {}

    def is_allowed(self, key: str) -> tuple[bool, int]:
        """Check if request is allowed.

        Args:
            key: Client identifier (e.g., IP or API key).

        Returns:
            Tuple of (allowed, remaining_requests).
        """
        settings = get_security_settings()

        if not settings.rate_limit_enabled:
            return True, settings.rate_limit_requests

        now = time.time()
        window_start = now - settings.rate_limit_window_seconds

        # Get or create request list
        if key not in self._requests:
            self._requests[key] = []

        # Remove old requests outside window
        self._requests[key] = [ts for ts in self._requests[key] if ts > window_start]

        remaining = settings.rate_limit_requests - len(self._requests[key])

        if remaining <= 0:
            return False, 0

        # Record this request
        self._requests[key].append(now)
        return True, remaining - 1

    def cleanup(self) -> None:
        """Remove stale entries to prevent memory leak."""
        settings = get_security_settings()
        now = time.time()
        window_start = now - settings.rate_limit_window_seconds

        keys_to_remove = []
        for key, timestamps in self._requests.items():
            self._requests[key] = [ts for ts in timestamps if ts > window_start]
            if not self._requests[key]:
                keys_to_remove.append(key)

        for key in keys_to_remove:
            del self._requests[key]


# Global rate limiter instance
rate_limiter = RateLimiter()


async def check_rate_limit(request: Request) -> None:
    """Check rate limit for request.

    Raises:
        HTTPException: If rate limit exceeded.
    """
    settings = get_security_settings()

    if not settings.rate_limit_enabled:
        return

    # Use client IP as key (could also use API key)
    client_ip = request.client.host if request.client else "unknown"

    allowed, remaining = rate_limiter.is_allowed(client_ip)

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "detail": (
                    f"Rate limit exceeded. Try again in "
                    f"{settings.rate_limit_window_seconds} seconds."
                ),
            },
            headers={
                "Retry-After": str(settings.rate_limit_window_seconds),
                "X-RateLimit-Remaining": "0",
            },
        )


# =============================================================================
# Security Headers Middleware
# =============================================================================

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline' https:; "
        "style-src 'self' 'unsafe-inline' https:; img-src 'self' data: https:; "
        "font-src 'self' https:; connect-src 'self'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware:
    """Middleware to add security headers to all responses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                for key, value in SECURITY_HEADERS.items():
                    header_key = key.lower().encode()
                    if header_key not in headers:
                        headers[header_key] = value.encode()
                message["headers"] = list(headers.items())
            await send(message)

        await self.app(scope, receive, send_with_headers)


# =============================================================================
# Request Size Validation
# =============================================================================


async def validate_request_size(request: Request) -> None:
    """Validate request body size.

    Raises:
        HTTPException: If body exceeds size limit.
    """
    settings = get_security_settings()

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            size = int(content_length)
            if size > settings.max_request_body_bytes:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "error": "request_too_large",
                        "detail": (
                            f"Request body exceeds maximum size of "
                            f"{settings.max_request_body_bytes} bytes"
                        ),
                    },
                )
        except ValueError:
            pass  # Invalid content-length, let FastAPI handle it
