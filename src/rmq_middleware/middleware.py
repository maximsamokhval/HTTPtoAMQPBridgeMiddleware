"""Request tracing and structured logging middleware.

Provides Request-ID propagation and Loguru configuration for JSON structured
logging with correlation IDs.
"""

import sys
import uuid
from contextvars import ContextVar
from typing import Awaitable, Callable

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from rmq_middleware.config import get_settings

# Context variable for request ID propagation across async boundaries
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Get the current request ID from context.

    Returns empty string if not in a request context.
    """
    return request_id_ctx.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Middleware for Request-ID extraction and propagation.

    Extracts X-Request-ID from incoming request headers or generates a new UUID.
    The ID is stored in a context variable for access throughout the request
    lifecycle and added to response headers.
    """

    HEADER_NAME = "X-Request-ID"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process request with Request-ID tracking."""
        # Extract or generate request ID
        request_id = request.headers.get(self.HEADER_NAME)
        if not request_id:
            request_id = str(uuid.uuid4())

        # Store in context variable
        token = request_id_ctx.set(request_id)

        try:
            # Process request with Loguru context
            with logger.contextualize(request_id=request_id):
                logger.debug(
                    "Request started",
                    method=request.method,
                    path=request.url.path,
                )

                response = await call_next(request)

                logger.debug(
                    "Request completed",
                    method=request.method,
                    path=request.url.path,
                    status_code=response.status_code,
                )

                # Add request ID to response headers
                response.headers[self.HEADER_NAME] = request_id

                return response
        finally:
            # Reset context variable
            request_id_ctx.reset(token)


def text_formatter(record: dict) -> str:
    """Human-readable formatter for development.

    Includes request_id when available for easier debugging.
    """
    request_id = record["extra"].get("request_id", "")
    request_id_str = f"[{request_id[:8]}] " if request_id else ""

    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        f"<cyan>{request_id_str}</cyan>"
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>\n"
    )


def json_sink(message) -> None:
    """Custom sink that outputs JSON formatted logs."""
    record = message.record
    import json
    from datetime import datetime, timezone

    # Build log entry
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": record["level"].name,
        "message": record["message"],
        "logger": record["name"],
        "module": record["module"],
        "function": record["function"],
        "line": record["line"],
    }

    # Add request_id from context if available
    if "request_id" in record["extra"]:
        log_entry["request_id"] = record["extra"]["request_id"]

    # Add any additional extra fields
    for key, value in record["extra"].items():
        if key != "request_id":
            # Skip complex objects that can't be serialized
            try:
                json.dumps(value)
                log_entry[key] = value
            except (TypeError, ValueError):
                log_entry[key] = str(value)

    # Add exception info if present
    if record["exception"] is not None:
        log_entry["exception"] = {
            "type": record["exception"].type.__name__ if record["exception"].type else None,
            "value": str(record["exception"].value) if record["exception"].value else None,
            "traceback": record["exception"].traceback is not None,
        }

    sys.stdout.write(json.dumps(log_entry) + "\n")
    sys.stdout.flush()


def setup_logging() -> None:
    """Configure Loguru for the application.

    Sets up structured logging based on configuration:
    - JSON format for production (LOG_FORMAT=json)
    - Human-readable format for development (LOG_FORMAT=text)
    """
    settings = get_settings()

    # Remove default handler
    logger.remove()

    # Configure based on format setting
    if settings.log_format == "json":
        logger.add(
            json_sink,
            level=settings.log_level,
            backtrace=True,
            diagnose=False,  # Disable diagnose in production for security
        )
    else:
        logger.add(
            sys.stdout,
            format=text_formatter,
            level=settings.log_level,
            colorize=True,
            backtrace=True,
            diagnose=True,
        )

    # Configure file logging if enabled
    if settings.log_file:
        logger.add(
            settings.log_file,
            rotation=settings.log_rotation,
            retention=settings.log_retention,
            level=settings.log_level,
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[request_id]} | "
                "{name}:{function}:{line} - {message}"
            )
            if settings.log_format == "text"
            else "{message}",
            serialize=True if settings.log_format == "json" else False,
            backtrace=True,
            diagnose=False,
        )

    logger.info(
        "Logging configured",
        level=settings.log_level,
        format=settings.log_format,
        log_file=settings.log_file,
    )


# Intercept standard library logging
class InterceptHandler:
    """Handler to redirect standard library logging to Loguru."""

    def emit(self, record):
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find caller from where originated the logged message
        frame, depth = sys._getframe(6), 6
        while frame and frame.f_code.co_filename == __file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())
