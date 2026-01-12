"""API route definitions for HTTP-to-AMQP bridge.

Provides endpoints for:
- Publishing messages to RabbitMQ
- Fetching messages from queues (long-polling)
- Acknowledging/rejecting messages
- Health and readiness probes
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasicCredentials
from loguru import logger
from pydantic import BaseModel

from rmq_middleware.amqp_wrapper import (
    AMQPClient,
    AMQPConnectionError,
    AMQPPublishError,
    AMQPConsumeError,
)
from rmq_middleware.middleware import get_request_id
from rmq_middleware.models import PublishRequest, FetchRequest, DeliveryMode
from rmq_middleware.security import (
    InputValidationError,
    check_rate_limit,
    get_amqp_credentials,
    validate_exchange_name,
    validate_queue_name,
    validate_request_size,
    validate_routing_key,
)


# Response Models


class PublishResponse(BaseModel):
    """Response for successful publish."""

    status: str = "accepted"
    request_id: str | None = None
    exchange: str
    routing_key: str
    message_id: str | None = None
    correlation_id: str | None = None


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

    requeue: bool = False


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
    active_sessions: int


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str | None = None
    request_id: str | None = None


# Routers

router = APIRouter()

# V1 router with security dependencies (Rate limit and Size limit only)
# Auth is handled per-endpoint to inject credentials
v1_router = APIRouter(
    prefix="/v1",
    tags=["AMQP Operations"],
    dependencies=[
        Depends(check_rate_limit),
        Depends(validate_request_size),
    ],
)

health_router = APIRouter(tags=["Health"])


# V1 API Endpoints


@v1_router.post(
    "/publish",
    response_model=PublishResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
        504: {"model": ErrorResponse, "description": "Publish timeout"},
    },
)
async def publish_message(
    body: PublishRequest,
    credentials: Annotated[HTTPBasicCredentials, Depends(get_amqp_credentials)],
) -> PublishResponse:
    """Publish a message to RabbitMQ with strict schema validation.

    Returns 202 Accepted only after the broker confirms the message is persisted.
    Returns 503 if RabbitMQ is unavailable (no RAM buffering).
    Requires Basic Auth.
    """
    request_id = get_request_id()

    # Validate input format (business logic validation)
    try:
        validate_exchange_name(body.exchange)
        validate_routing_key(body.routing_key)
    except InputValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="validation_error",
                detail=str(e),
                request_id=request_id,
            ).model_dump(),
        )

    try:
        client = await AMQPClient.get_instance()

        await client.publish(
            exchange=body.exchange,
            routing_key=body.routing_key,
            payload=body.payload,
            credentials=(credentials.username, credentials.password),
            headers=body.headers,
            persistent=(body.persistence == DeliveryMode.PERSISTENT),
            mandatory=body.mandatory,
            message_id=body.message_id,
            correlation_id=body.correlation_id,
            priority=body.priority,
        )

        return PublishResponse(
            request_id=request_id,
            exchange=body.exchange,
            routing_key=body.routing_key,
            message_id=body.message_id,
            correlation_id=body.correlation_id or request_id,
        )

    except AMQPConnectionError as e:
        logger.error(f"Publish failed - connection error: {e}")
        # Could be auth error or connection error
        if "Authentication failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ErrorResponse(
                    error="authentication_failed",
                    detail=str(e),
                    request_id=request_id,
                ).model_dump(),
                headers={"WWW-Authenticate": "Basic"},
            )

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


@v1_router.post(
    "/fetch",
    response_model=MessageResponse | None,
    responses={
        200: {"model": MessageResponse, "description": "Message retrieved"},
        204: {"description": "No message available"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
    },
)
async def fetch_message_post(
    body: FetchRequest,
    credentials: Annotated[HTTPBasicCredentials, Depends(get_amqp_credentials)],
) -> MessageResponse | JSONResponse:
    """Fetch a single message from a queue (long-polling) using POST body params.

    Supports:
    - Custom timeout (0-300s)
    - Auto-acknowledgment (auto_ack=True)

    Returns 200 with message if available, 204 if no message within timeout.
    Requires Basic Auth.
    """
    request_id = get_request_id()

    # Validate queue name
    try:
        validate_queue_name(body.queue)
    except InputValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ErrorResponse(
                error="validation_error",
                detail=str(e),
                request_id=request_id,
            ).model_dump(),
        )

    try:
        client = await AMQPClient.get_instance()

        message = await client.consume_one(
            queue_name=body.queue,
            credentials=(credentials.username, credentials.password),
            timeout=float(body.timeout),
            auto_ack=body.auto_ack,
        )

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
        logger.error(f"Fetch failed - connection error: {e}")
        if "Authentication failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ErrorResponse(
                    error="authentication_failed",
                    detail=str(e),
                    request_id=request_id,
                ).model_dump(),
                headers={"WWW-Authenticate": "Basic"},
            )
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
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
    },
)
async def acknowledge_message(
    delivery_tag: int,
    credentials: Annotated[HTTPBasicCredentials, Depends(get_amqp_credentials)],
    body: AckRequest | None = None,
) -> AckResponse:
    """Acknowledge successful message processing.

    Must be called after processing a message fetched via /fetch endpoint (if auto_ack=False).
    Requires Basic Auth.
    """
    request_id = get_request_id()

    try:
        client = await AMQPClient.get_instance()

        success = await client.acknowledge(
            delivery_tag, credentials=(credentials.username, credentials.password)
        )

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
        logger.error(f"Ack failed - connection error: {e}")
        if "Authentication failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ErrorResponse(
                    error="authentication_failed",
                    detail=str(e),
                    request_id=request_id,
                ).model_dump(),
                headers={"WWW-Authenticate": "Basic"},
            )
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
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        503: {"model": ErrorResponse, "description": "RabbitMQ unavailable"},
    },
)
async def reject_message(
    delivery_tag: int,
    credentials: Annotated[HTTPBasicCredentials, Depends(get_amqp_credentials)],
    body: AckRequest | None = None,
) -> AckResponse:
    """Reject a message and optionally requeue it.

    If requeue=false (default), message goes to Dead Letter Exchange.
    Requires Basic Auth.
    """
    request_id = get_request_id()
    requeue = body.requeue if body else False

    try:
        client = await AMQPClient.get_instance()

        success = await client.reject(
            delivery_tag,
            credentials=(credentials.username, credentials.password),
            requeue=requeue,
        )

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
        logger.error(f"Reject failed - connection error: {e}")
        if "Authentication failed" in str(e):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ErrorResponse(
                    error="authentication_failed",
                    detail=str(e),
                    request_id=request_id,
                ).model_dump(),
                headers={"WWW-Authenticate": "Basic"},
            )
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
    """Liveness probe - returns 200 if application is running."""
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

    Returns 200 if the application is ready (system connection active).
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
            active_sessions=health["active_sessions"],
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
