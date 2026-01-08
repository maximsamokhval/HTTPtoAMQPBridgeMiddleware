"""API route definitions for HTTP-to-AMQP bridge.

Provides endpoints for:
- Publishing messages to RabbitMQ
- Fetching messages from queues (long-polling)
- Acknowledging/rejecting messages
- Health and readiness probes
"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel, Field

from rmq_middleware.amqp_wrapper import (
    AMQPClient,
    AMQPClientError,
    AMQPConnectionError,
    AMQPPublishError,
    AMQPConsumeError,
)
from rmq_middleware.middleware import get_request_id


# Request/Response Models

class PublishRequest(BaseModel):
    """Request body for message publishing."""
    payload: dict[str, Any] | str = Field(
        ...,
        description="Message body (object or string)",
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="Optional message headers",
    )
    persistent: bool = Field(
        default=True,
        description="If true, message survives broker restart",
    )


class PublishResponse(BaseModel):
    """Response for successful publish."""
    status: str = "accepted"
    request_id: str | None = None
    exchange: str
    routing_key: str


class MessageResponse(BaseModel):
    """Response containing a consumed message."""
    delivery_tag: int
    body: Any
    routing_key: str
    exchange: str
    correlation_id: str | None
    headers: dict[str, Any]
    redelivered: bool


class AckRequest(BaseModel):
    """Request body for message acknowledgment."""
    requeue: bool = Field(
        default=False,
        description="If rejecting, whether to requeue the message",
    )


class AckResponse(BaseModel):
    """Response for acknowledgment operations."""
    status: str
    delivery_tag: int


class HealthResponse(BaseModel):
    """Response for health check endpoints."""
    status: str
    service: str = "rmq-middleware"


class ReadyResponse(BaseModel):
    """Response for readiness probe."""
    status: str
    service: str = "rmq-middleware"
    amqp_connected: bool
    amqp_ready: bool
    amqp_state: str
    pending_messages: int


class ErrorResponse(BaseModel):
    """Standard error response."""
    error: str
    detail: str | None = None
    request_id: str | None = None


# Routers

router = APIRouter()
v1_router = APIRouter(prefix="/v1", tags=["AMQP Operations"])
health_router = APIRouter(tags=["Health"])


# V1 API Endpoints

@v1_router.post(
    "/publish/{exchange}/{routing_key}",
    response_model=PublishResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
        504: {"model": ErrorResponse, "description": "Publish timeout"},
    },
)
async def publish_message(
    exchange: str,
    routing_key: str,
    body: PublishRequest,
) -> PublishResponse:
    """Publish a message to RabbitMQ.
    
    Returns 202 Accepted only after the broker confirms the message is persisted.
    Returns 503 if RabbitMQ is unavailable (no RAM buffering).
    """
    request_id = get_request_id()
    
    try:
        client = await AMQPClient.get_instance()
        
        await client.publish(
            exchange=exchange,
            routing_key=routing_key,
            payload=body.payload,
            headers=body.headers,
            persistent=body.persistent,
        )
        
        return PublishResponse(
            request_id=request_id,
            exchange=exchange,
            routing_key=routing_key,
        )
        
    except AMQPConnectionError as e:
        logger.error(f"Publish failed - not connected: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="service_unavailable",
                detail="RabbitMQ connection is not available",
                request_id=request_id,
            ).model_dump(),
        )
    except AMQPPublishError as e:
        if "timeout" in str(e).lower():
            logger.error(f"Publish timeout: {e}")
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail=ErrorResponse(
                    error="publish_timeout",
                    detail=str(e),
                    request_id=request_id,
                ).model_dump(),
            )
        logger.error(f"Publish failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="publish_failed",
                detail=str(e),
                request_id=request_id,
            ).model_dump(),
        )


@v1_router.get(
    "/fetch/{queue}",
    response_model=MessageResponse | None,
    responses={
        200: {"model": MessageResponse, "description": "Message retrieved"},
        204: {"description": "No message available"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
    },
)
async def fetch_message(
    queue: str,
    timeout: float = 30.0,
) -> MessageResponse | JSONResponse:
    """Fetch a single message from a queue (long-polling style).
    
    Returns 200 with message if available, 204 if no message within timeout.
    The message requires explicit acknowledgment via POST /v1/ack/{delivery_tag}.
    """
    request_id = get_request_id()
    
    # Clamp timeout to reasonable bounds
    timeout = max(1.0, min(timeout, 300.0))
    
    try:
        client = await AMQPClient.get_instance()
        
        message = await client.consume_one(queue_name=queue, timeout=timeout)
        
        if message is None:
            return JSONResponse(
                status_code=status.HTTP_204_NO_CONTENT,
                content=None,
            )
        
        return MessageResponse(
            delivery_tag=message.delivery_tag,
            body=message.body,
            routing_key=message.routing_key,
            exchange=message.exchange,
            correlation_id=message.correlation_id,
            headers=message.headers,
            redelivered=message.redelivered,
        )
        
    except AMQPConnectionError as e:
        logger.error(f"Fetch failed - not connected: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="service_unavailable",
                detail="RabbitMQ connection is not available",
                request_id=request_id,
            ).model_dump(),
        )
    except AMQPConsumeError as e:
        logger.error(f"Fetch failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="consume_failed",
                detail=str(e),
                request_id=request_id,
            ).model_dump(),
        )


@v1_router.post(
    "/ack/{delivery_tag}",
    response_model=AckResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid delivery tag"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
    },
)
async def acknowledge_message(
    delivery_tag: int,
    body: AckRequest | None = None,
) -> AckResponse:
    """Acknowledge successful message processing.
    
    Must be called after processing a message fetched via GET /v1/fetch/{queue}.
    """
    request_id = get_request_id()
    
    try:
        client = await AMQPClient.get_instance()
        
        success = await client.acknowledge(delivery_tag)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(
                    error="invalid_delivery_tag",
                    detail=f"Delivery tag {delivery_tag} not found in pending messages",
                    request_id=request_id,
                ).model_dump(),
            )
        
        return AckResponse(
            status="acknowledged",
            delivery_tag=delivery_tag,
        )
        
    except AMQPConnectionError as e:
        logger.error(f"Ack failed - not connected: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="service_unavailable",
                detail="RabbitMQ connection is not available",
                request_id=request_id,
            ).model_dump(),
        )


@v1_router.post(
    "/reject/{delivery_tag}",
    response_model=AckResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid delivery tag"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
    },
)
async def reject_message(
    delivery_tag: int,
    body: AckRequest | None = None,
) -> AckResponse:
    """Reject a message and optionally requeue it.
    
    If requeue=false (default), message goes to Dead Letter Exchange.
    """
    request_id = get_request_id()
    requeue = body.requeue if body else False
    
    try:
        client = await AMQPClient.get_instance()
        
        success = await client.reject(delivery_tag, requeue=requeue)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=ErrorResponse(
                    error="invalid_delivery_tag",
                    detail=f"Delivery tag {delivery_tag} not found in pending messages",
                    request_id=request_id,
                ).model_dump(),
            )
        
        return AckResponse(
            status="rejected" if not requeue else "requeued",
            delivery_tag=delivery_tag,
        )
        
    except AMQPConnectionError as e:
        logger.error(f"Reject failed - not connected: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="service_unavailable",
                detail="RabbitMQ connection is not available",
                request_id=request_id,
            ).model_dump(),
        )


# Health Check Endpoints

@health_router.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
)
async def health_check() -> HealthResponse:
    """Liveness probe - returns 200 if application is running.
    
    This endpoint should always return 200 as long as the HTTP server
    is responding. Use /ready for readiness checks.
    """
    return HealthResponse(status="healthy")


@health_router.get(
    "/ready",
    response_model=ReadyResponse,
    responses={
        503: {"model": ErrorResponse, "description": "Not ready"},
    },
)
async def readiness_check() -> ReadyResponse:
    """Readiness probe - checks RabbitMQ connection status.
    
    Returns 200 if the application is ready to handle requests,
    503 if RabbitMQ connection is not available.
    """
    try:
        client = await AMQPClient.get_instance()
        health = await client.health_check()
        
        if not health["ready"]:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=ErrorResponse(
                    error="not_ready",
                    detail=f"AMQP state: {health['state']}",
                ).model_dump(),
            )
        
        return ReadyResponse(
            status="ready",
            amqp_connected=health["connected"],
            amqp_ready=health["ready"],
            amqp_state=health["state"],
            pending_messages=health["pending_messages"],
        )
        
    except Exception as e:
        logger.error(f"Readiness check failed: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ErrorResponse(
                error="not_ready",
                detail=str(e),
            ).model_dump(),
        )


# Combine routers
router.include_router(health_router)
router.include_router(v1_router)
