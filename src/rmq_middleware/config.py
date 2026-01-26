"""Configuration module using Pydantic Settings v2.

Provides validated configuration from environment variables with support for
.env files in local development.
"""

from functools import lru_cache
from typing import Literal

from pydantic import AmqpDsn, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration with validation.

    All settings are loaded from environment variables with optional .env file
    support. Sensitive values use SecretStr to prevent accidental logging.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # RabbitMQ Connection
    rabbitmq_url: AmqpDsn = Field(
        ...,
        description="Full AMQP connection URL (e.g., amqp://user:pass@host:5672/vhost)",
    )
    rabbitmq_prefetch_count: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Maximum unacknowledged messages per consumer",
    )

    # Retry Configuration
    retry_attempts: int = Field(
        default=60,
        ge=1,
        le=120,
        description="Maximum connection retry attempts",
    )
    retry_base_delay: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description="Base delay in seconds for exponential backoff",
    )

    # Timeout Configuration
    publish_timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Timeout in seconds for publisher confirms",
    )
    consume_timeout: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Timeout in seconds for long-polling consume",
    )

    # Logging Configuration
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="DEBUG",
        description="Logging level",
    )
    log_format: Literal["json", "text"] = Field(
        default="json",
        description="Log output format (json for production, text for development)",
    )
    log_file: str | None = Field(
        default=None,
        description="Path to log file. If None, logs only to stdout.",
    )
    log_rotation: str = Field(
        default="500 MB",
        description="Log rotation condition (size, time, etc.)",
    )
    log_retention: str = Field(
        default="10 days",
        description="Log retention duration",
    )

    # Message Size Limits
    max_message_size_bytes: int = Field(
        default=10_485_760,  # 10 MB
        ge=1024,
        le=104_857_600,  # 100 MB max
        description="Maximum allowed message size in bytes for AMQP messages",
    )

    # Application Settings
    app_name: str = Field(
        default="rmq-middleware",
        description="Application name for logging and tracing",
    )
    app_host: str = Field(
        default="0.0.0.0",  # nosec
        description="Host to bind the HTTP server",
    )
    app_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="Port to bind the HTTP server",
    )

    # Monitoring Configuration
    disable_prometheus: bool = Field(
        default=False,
        description="Disable Prometheus instrumentation (useful for debugging)",
    )

    # Circuit Breaker Configuration (ADR-002)
    cb_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Number of failures before opening circuit breaker",
    )
    cb_failure_window_seconds: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="Sliding window in seconds for failure counting",
    )
    cb_recovery_timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Time in seconds circuit stays open before half-open",
    )
    cb_half_open_requests: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Number of test requests allowed in half-open state",
    )

    @property
    def rabbitmq_url_str(self) -> str:
        """Return RabbitMQ URL as string for aio-pika."""
        return str(self.rabbitmq_url)

    @property
    def rabbitmq_url_masked(self) -> str:
        """Return RabbitMQ URL with password masked for logging."""
        url = str(self.rabbitmq_url)
        # Simple masking - replace password between : and @
        if "@" in url and "://" in url:
            prefix, rest = url.split("://", 1)
            if "@" in rest:
                user_pass, host = rest.rsplit("@", 1)
                if ":" in user_pass:
                    user, _ = user_pass.split(":", 1)
                    return f"{prefix}://{user}:****@{host}"
        return url


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance.

    Uses lru_cache to ensure settings are only loaded once.
    """
    return Settings()
