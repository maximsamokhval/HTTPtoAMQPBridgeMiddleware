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
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator

from rmq_middleware import __version__
from rmq_middleware.amqp_wrapper import AMQPClient, AMQPClientError
from rmq_middleware.config import get_settings
from rmq_middleware.middleware import RequestIDMiddleware, setup_logging, get_request_id
from rmq_middleware.routes import router
from rmq_middleware.security import SecurityHeadersMiddleware


# Metrics
AMQP_STATUS = Gauge("rmq_middleware_amqp_status", "RabbitMQ connection status (1=connected, 0=disconnected)")
PENDING_MESSAGES = Gauge("rmq_middleware_pending_messages", "Number of unacknowledged messages")


async def update_metrics() -> None:
    """Background task to update custom metrics."""
    client = await AMQPClient.get_instance()
    while True:
        try:
            health = await client.health_check()
            AMQP_STATUS.set(1 if health["connected"] else 0)
            PENDING_MESSAGES.set(health["pending_messages"])
        except Exception:
            AMQP_STATUS.set(0)
        
        # Update every 5 seconds
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break


# Global shutdown event removed - relying on ASGI lifecycle


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    settings = get_settings()
    
    # Startup
    logger.info(
        "Starting RMQ Middleware",
        version=__version__,
        rabbitmq_url=settings.rabbitmq_url_masked,
    )
    
    # Start metrics update task
    metrics_task = asyncio.create_task(update_metrics())

    # Connect to RabbitMQ
    client = await AMQPClient.get_instance()
    try:
        await client.connect()
    except Exception as e:
        logger.error(f"Failed to connect to RabbitMQ on startup: {e}")
    
    logger.info("RMQ Middleware started successfully")
    
    yield
    
    # Shutdown
    logger.info("Shutting down RMQ Middleware")
    
    # Cancel metrics task
    metrics_task.cancel()
    try:
        await metrics_task
    except asyncio.CancelledError:
        pass

    await asyncio.sleep(0.5)
    
    try:
        await client.disconnect()
    except Exception as e:
        logger.error(f"Error during AMQP disconnect: {e}")
    
    logger.info("RMQ Middleware shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
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
    
    # Setup Prometheus instrumentation
    Instrumentator().instrument(app).expose(app)
    
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIDMiddleware)
    
    app.include_router(router)
    
    @app.exception_handler(AMQPClientError)
    async def amqp_error_handler(request: Request, exc: AMQPClientError) -> JSONResponse:
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


app = create_app()


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
