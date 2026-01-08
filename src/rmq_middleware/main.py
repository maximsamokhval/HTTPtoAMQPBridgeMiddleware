"""FastAPI application entry point with lifecycle management.

Handles:
- Application startup and shutdown
- Signal handling for graceful shutdown
- Middleware registration
- Error handlers
"""

import asyncio
import signal
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger

from rmq_middleware import __version__
from rmq_middleware.amqp_wrapper import AMQPClient, AMQPClientError
from rmq_middleware.config import get_settings
from rmq_middleware.middleware import RequestIDMiddleware, setup_logging, get_request_id
from rmq_middleware.routes import router
from rmq_middleware.security import SecurityHeadersMiddleware


# Global shutdown event
shutdown_event = asyncio.Event()


def handle_signal(sig: signal.Signals) -> None:
    """Handle termination signals for graceful shutdown."""
    logger.info(f"Received signal {sig.name}, initiating graceful shutdown")
    shutdown_event.set()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager.
    
    Handles startup and shutdown sequences:
    
    Startup:
    1. Configure logging
    2. Connect to RabbitMQ
    
    Shutdown:
    1. Stop accepting new requests (handled by FastAPI)
    2. Wait for in-flight requests (brief delay)
    3. Disconnect from RabbitMQ
    4. Log completion
    """
    settings = get_settings()
    
    # Startup
    logger.info(
        "Starting RMQ Middleware",
        version=__version__,
        rabbitmq_url=settings.rabbitmq_url_masked,
    )
    
    # Connect to RabbitMQ
    client = await AMQPClient.get_instance()
    try:
        await client.connect()
    except Exception as e:
        logger.error(f"Failed to connect to RabbitMQ on startup: {e}")
        # Continue anyway - will retry on first request or via background task
    
    # Register signal handlers (Unix-like systems)
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, handle_signal, sig)
    
    logger.info("RMQ Middleware started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down RMQ Middleware")
    
    # Brief delay to allow in-flight requests to complete
    await asyncio.sleep(0.5)
    
    # Disconnect from RabbitMQ
    try:
        await client.disconnect()
    except Exception as e:
        logger.error(f"Error during AMQP disconnect: {e}")
    
    logger.info("RMQ Middleware shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()
    
    # Configure logging first
    setup_logging()
    
    app = FastAPI(
        title="RMQ Middleware",
        description="HTTP-to-AMQP Bridge for ERP Integration (1C:Enterprise)",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    
    # Add middleware (order matters - security headers first, then request ID)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    
    # Include routes
    app.include_router(router)
    
    # Register exception handlers
    @app.exception_handler(AMQPClientError)
    async def amqp_error_handler(request: Request, exc: AMQPClientError) -> JSONResponse:
        """Handle AMQP client errors."""
        request_id = get_request_id()
        logger.error(f"AMQP error: {exc}", request_id=request_id)
        return JSONResponse(
            status_code=503,
            content={
                "error": "amqp_error",
                "detail": str(exc),
                "request_id": request_id,
            },
        )
    
    @app.exception_handler(Exception)
    async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """Handle unexpected errors."""
        request_id = get_request_id()
        logger.exception(f"Unexpected error: {exc}", request_id=request_id)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "detail": "An unexpected error occurred",
                "request_id": request_id,
            },
        )
    
    return app


# Create application instance
app = create_app()


# Windows signal handling
if sys.platform == "win32":
    def windows_signal_handler(sig, frame):
        """Windows-compatible signal handler."""
        logger.info(f"Received signal {sig}, initiating shutdown")
        shutdown_event.set()
        # Force exit after brief delay if graceful shutdown fails
        asyncio.get_event_loop().call_later(5.0, sys.exit, 0)
    
    signal.signal(signal.SIGINT, windows_signal_handler)
    signal.signal(signal.SIGTERM, windows_signal_handler)


if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    
    uvicorn.run(
        "rmq_middleware.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
        log_level=settings.log_level.lower(),
        access_log=True,
    )
