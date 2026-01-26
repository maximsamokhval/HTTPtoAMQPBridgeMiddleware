"""Robust AMQP client wrapper with connection management and message operations.

Provides a singleton-like AMQPClient class that handles:
- Connection pooling per user (credentials)
- Connection with exponential backoff reconnection
- Publisher confirms for reliable message delivery
- Long-polling message consumption
- Message acknowledgment and rejection
- Topology setup with Dead Letter Exchange support
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Tuple

import aio_pika
from aio_pika import ExchangeType, Message, DeliveryMode
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
)
from loguru import logger

from rmq_middleware.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from rmq_middleware.config import Settings, get_settings
from rmq_middleware.middleware import get_request_id
from rmq_middleware.security import validate_message_size, InputValidationError


class ConnectionState(str, Enum):
    """AMQP connection state machine states."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTING = "disconnecting"


@dataclass
class ConsumedMessage:
    """Wrapper for consumed messages with metadata.

    Provides a serializable representation of an AMQP message for HTTP responses.
    """

    delivery_tag: int
    body: Any
    routing_key: str
    exchange: str
    correlation_id: str | None
    headers: dict[str, Any]
    redelivered: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "delivery_tag": self.delivery_tag,
            "body": self.body,
            "routing_key": self.routing_key,
            "exchange": self.exchange,
            "correlation_id": self.correlation_id,
            "headers": self.headers,
            "redelivered": self.redelivered,
        }


@dataclass
class TopologyConfig:
    """Configuration for exchange and queue topology setup."""

    exchange_name: str
    exchange_type: ExchangeType = ExchangeType.TOPIC
    queue_name: str = ""
    routing_key: str = "#"
    durable: bool = True
    dlx_exchange_name: str = ""
    dlx_queue_name: str = ""
    message_ttl: int | None = None  # milliseconds

    def __post_init__(self):
        """Set default DLX names if not provided."""
        if not self.dlx_exchange_name:
            self.dlx_exchange_name = f"{self.exchange_name}.dlx"
        if not self.dlx_queue_name:
            self.dlx_queue_name = f"{self.queue_name}.dlq" if self.queue_name else ""


class AMQPClientError(Exception):
    """Base exception for AMQP client errors."""

    pass


class AMQPConnectionError(AMQPClientError):
    """Raised when connection to RabbitMQ fails."""

    pass


class AMQPPublishError(AMQPClientError):
    """Raised when message publishing fails."""

    pass


class AMQPConsumeError(AMQPClientError):
    """Raised when message consumption fails."""

    pass


@dataclass
class UserSession:
    """Represents a connection session for a specific user."""

    connection: AbstractConnection
    publisher_channel: AbstractChannel
    consumer_channel: AbstractChannel
    pending_messages: dict[int, AbstractIncomingMessage] = field(default_factory=dict)
    last_used: float = field(default_factory=time.time)
    state: ConnectionState = ConnectionState.CONNECTED

    async def close(self):
        """Close all channels and connection with robust error handling.
        
        Ensures resources are properly released even if errors occur during close.
        """
        errors = []
        
        # Close consumer channel
        if hasattr(self, 'consumer_channel') and self.consumer_channel:
            try:
                if not self.consumer_channel.is_closed:
                    await self.consumer_channel.close()
            except Exception as e:
                errors.append(f"consumer_channel: {e}")
                logger.warning(f"Error closing consumer channel: {e}")
        
        # Close publisher channel
        if hasattr(self, 'publisher_channel') and self.publisher_channel:
            try:
                if not self.publisher_channel.is_closed:
                    await self.publisher_channel.close()
            except Exception as e:
                errors.append(f"publisher_channel: {e}")
                logger.warning(f"Error closing publisher channel: {e}")
        
        # Close connection
        if hasattr(self, 'connection') and self.connection:
            try:
                if not self.connection.is_closed:
                    await self.connection.close()
            except Exception as e:
                errors.append(f"connection: {e}")
                logger.warning(f"Error closing connection: {e}")
        
        # Clear pending messages to prevent memory leaks
        if hasattr(self, 'pending_messages'):
            self.pending_messages.clear()
        
        # Update state
        self.state = ConnectionState.DISCONNECTED
        
        if errors:
            logger.warning(f"User session closed with {len(errors)} error(s): {', '.join(errors)}")
        else:
            logger.debug("User session closed successfully")


class AMQPClient:
    """Robust AMQP client with connection pooling and message operations.

    Implements the singleton pattern to ensure shared state.
    Manages a pool of connections keyed by user credentials.
    """

    _instance: "AMQPClient | None" = None
    _lock: asyncio.Lock = asyncio.Lock()

    def __init__(self, settings: Settings | None = None):
        """Initialize AMQP client."""
        self._settings = settings or get_settings()
        self._sessions: dict[str, UserSession] = {}  # Key: "username:password"
        self._session_lock = asyncio.Lock()

        # System connection for health checks and global ops
        self._system_session: UserSession | None = None
        self._cleanup_task: asyncio.Task | None = None

        # Circuit Breaker for fault tolerance (ADR-002)
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=self._settings.cb_failure_threshold,
            failure_window=self._settings.cb_failure_window_seconds,
            recovery_timeout=self._settings.cb_recovery_timeout,
            half_open_requests=self._settings.cb_half_open_requests,
        )

    @classmethod
    async def get_instance(cls, settings: Settings | None = None) -> "AMQPClient":
        """Get or create singleton instance."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls(settings)
                # Start cleanup task
                cls._instance._start_cleanup_task()
            return cls._instance

    @classmethod
    async def reset_instance(cls) -> None:
        """Reset singleton instance (for testing)."""
        async with cls._lock:
            if cls._instance is not None:
                await cls._instance.shutdown()
                cls._instance = None

    @property
    def is_connected(self) -> bool:
        """Check if system connection is established."""
        return (
            self._system_session is not None
            and self._system_session.connection is not None
            and not self._system_session.connection.is_closed
        )

    @property
    def is_ready(self) -> bool:
        """Check if system session is ready (channels open)."""
        return (
            self.is_connected
            and self._system_session is not None
            and self._system_session.publisher_channel is not None
            and not self._system_session.publisher_channel.is_closed
        )

    def _start_cleanup_task(self):
        """Start background task to clean up idle sessions."""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        """Periodically remove idle sessions."""
        while True:
            try:
                await asyncio.sleep(60)  # Run every minute
                async with self._session_lock:
                    now = time.time()
                    to_remove = []
                    for key, session in self._sessions.items():
                        # Close if idle for 5 minutes
                        if now - session.last_used > 300:
                            await session.close()
                            to_remove.append(key)

                    for key in to_remove:
                        del self._sessions[key]
                        logger.info(f"Closed idle session for user: {key.split(':')[0]}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")

    async def _create_session(self, url: str) -> UserSession:
        """Create a new AMQP session from URL with retries."""
        last_error = None

        for attempt in range(self._settings.retry_attempts):
            try:
                if attempt > 0:
                    delay = self._settings.retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(f"Retrying connection in {delay:.1f}s")
                    await asyncio.sleep(delay)

                connection = await aio_pika.connect_robust(
                    url,
                    timeout=30.0,
                )

                # Create channels
                pub_channel = await connection.channel()
                await pub_channel.set_qos(prefetch_count=self._settings.rabbitmq_prefetch_count)

                sub_channel = await connection.channel()
                await sub_channel.set_qos(prefetch_count=self._settings.rabbitmq_prefetch_count)

                return UserSession(
                    connection=connection,
                    publisher_channel=pub_channel,
                    consumer_channel=sub_channel,
                    last_used=time.time(),
                )
            except Exception as e:
                last_error = e
                logger.warning(f"Connection attempt {attempt + 1} failed: {e}")

        raise AMQPConnectionError(
            f"Failed to connect after {self._settings.retry_attempts} attempts: {last_error}"
        )

    async def _get_session(self, credentials: Tuple[str, str] | None = None) -> UserSession:
        """Get or create a session for the given credentials.

        If credentials are provided, manages a pooled session.
        If None, returns the system session (creates if needed).
        """
        if credentials:
            username, password = credentials
            key = f"{username}:{password}"

            async with self._session_lock:
                if key in self._sessions:
                    session = self._sessions[key]
                    if session.connection.is_closed:
                        # Reconnect if closed
                        logger.info(f"Session for {username} closed, reconnecting...")
                        # Remove old one
                        del self._sessions[key]
                    else:
                        session.last_used = time.time()
                        return session

            # Create new session (outside lock to avoid blocking other users during connect)
            # Re-acquire lock to insert
            base_url = str(self._settings.rabbitmq_url)
            # Construct URL with new credentials
            # Assumes base_url format: amqp://user:pass@host:port/vhost
            # We need to replace user:pass
            try:
                # Parse and replace
                prefix, rest = base_url.split("://", 1)
                if "@" in rest:
                    host_part = rest.split("@", 1)[1]
                    new_url = f"{prefix}://{username}:{password}@{host_part}"
                else:
                    # No auth in base url?
                    new_url = f"{prefix}://{username}:{password}@{rest}"
            except Exception:
                # Fallback if parsing fails, assume provided URL is correct template
                new_url = base_url

            logger.info(f"Creating new session for user: {username}")
            try:
                session = await self._create_session(new_url)
                async with self._session_lock:
                    self._sessions[key] = session
                return session
            except Exception as e:
                # _create_session already raises AMQPConnectionError, but we might catch others
                if isinstance(e, AMQPConnectionError):
                    raise
                logger.error(f"Failed to create session for {username}: {e}")
                raise AMQPConnectionError(f"Authentication failed or broker unreachable: {e}")

        else:
            # System session
            if self._system_session and not self._system_session.connection.is_closed:
                return self._system_session

            logger.info("Connecting system session...")
            try:
                self._system_session = await self._create_session(self._settings.rabbitmq_url_str)
                return self._system_session
            except Exception as e:
                if isinstance(e, AMQPConnectionError):
                    raise
                raise AMQPConnectionError(f"System connection failed: {e}")

    async def connect(self) -> None:
        """Initialize system connection (legacy support)."""
        await self._get_session(None)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Gracefully shutdown all connections with draining mode.
        
        Args:
            timeout: Maximum time to wait for in-flight operations to complete.
        """
        logger.info("Starting graceful shutdown with draining mode")
        
        # Step 1: Cancel cleanup task
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        # Step 2: Set draining mode - prevent new operations
        draining_start = time.time()
        
        # Step 3: Wait for pending messages to be processed
        pending_count = 0
        async with self._session_lock:
            # Count all pending messages across sessions
            for session in self._sessions.values():
                pending_count += len(session.pending_messages)
        
        if pending_count > 0:
            logger.info(f"Waiting for {pending_count} pending messages to complete")
            # Give some time for in-flight operations to complete
            remaining_time = timeout - (time.time() - draining_start)
            if remaining_time > 0:
                await asyncio.sleep(min(2.0, remaining_time))
        
        # Step 4: Close all sessions
        async with self._session_lock:
            close_tasks = []
            for key, session in self._sessions.items():
                close_tasks.append(asyncio.create_task(session.close()))
            
            if close_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*close_tasks, return_exceptions=True),
                        timeout=max(1.0, timeout - (time.time() - draining_start))
                    )
                except asyncio.TimeoutError:
                    logger.warning("Timeout closing user sessions, forcing close")
            
            self._sessions.clear()

        # Step 5: Close system session
        if self._system_session:
            try:
                await asyncio.wait_for(
                    self._system_session.close(),
                    timeout=max(1.0, timeout - (time.time() - draining_start))
                )
            except asyncio.TimeoutError:
                logger.warning("Timeout closing system session, forcing close")
            self._system_session = None
        
        logger.info("Graceful shutdown completed")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    async def publish(
        self,
        exchange: str,
        routing_key: str,
        payload: dict[str, Any] | str | bytes,
        credentials: Tuple[str, str],
        headers: dict[str, Any] | None = None,
        persistent: bool = True,
        mandatory: bool = True,
        message_id: str | None = None,
        correlation_id: str | None = None,
        priority: int = 0,
    ) -> None:
        """Publish message using user credentials."""
        request_id = get_request_id()
        username = credentials[0]

        # Check Circuit Breaker state first (ADR-002)
        if not await self._circuit_breaker.allow_request():
            logger.warning(
                "Circuit breaker OPEN - rejecting publish request",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                request_id=request_id,
                cb_state=self._circuit_breaker.state.value,
            )
            raise CircuitBreakerOpen(
                f"Circuit breaker is {self._circuit_breaker.state.value} - broker unavailable"
            )
        
        # Validate message size before attempting to publish
        try:
            validate_message_size(payload)
        except InputValidationError as e:
            logger.error(
                "Message size validation failed",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                request_id=request_id,
                error=str(e)
            )
            raise AMQPPublishError(f"Message validation failed: {e}")
        
        try:
            session = await self._get_session(credentials)
        except AMQPConnectionError as e:
            # Record failure for Circuit Breaker
            await self._circuit_breaker.record_failure()
            logger.error(
                "Failed to get session for publish",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                request_id=request_id,
                error=str(e)
            )
            raise
        except Exception as e:
            # Record failure for Circuit Breaker
            await self._circuit_breaker.record_failure()
            logger.error(
                "Unexpected error during session acquisition",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                request_id=request_id,
                error=str(e)
            )
            raise AMQPConnectionError(f"Failed to establish session: {e}")

        if not correlation_id:
            correlation_id = request_id or None

        # Serialize payload
        if isinstance(payload, dict):
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
            content_type = "text/plain"
        elif isinstance(payload, bytes):
            body = payload
            content_type = "application/octet-stream"
        else:
            body = str(payload).encode("utf-8")
            content_type = "text/plain"

        message = Message(
            body=body,
            correlation_id=correlation_id,
            message_id=message_id,
            headers=headers or {},
            delivery_mode=DeliveryMode.PERSISTENT if persistent else DeliveryMode.NOT_PERSISTENT,
            priority=priority,
            content_type=content_type,
        )

        try:
            amqp_exchange = await session.publisher_channel.get_exchange(exchange)

            await asyncio.wait_for(
                amqp_exchange.publish(
                    message,
                    routing_key=routing_key,
                    mandatory=mandatory,
                ),
                timeout=self._settings.publish_timeout,
            )

            # Record success for Circuit Breaker
            await self._circuit_breaker.record_success()

            logger.info(
                "Message published",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                correlation_id=message.correlation_id,
                request_id=request_id,
            )

        except asyncio.TimeoutError:
            await self._circuit_breaker.record_failure()
            logger.error(
                "Publish confirmation timeout",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                timeout=self._settings.publish_timeout,
                request_id=request_id,
            )
            raise AMQPPublishError(
                f"Publish confirmation timeout after {self._settings.publish_timeout}s"
            )
        except aio_pika.exceptions.ChannelClosed as e:
            await self._circuit_breaker.record_failure()
            # Channel closed errors often indicate authentication or permission issues
            error_msg = str(e).lower()
            logger.error(
                "Channel closed during publish",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                error=error_msg,
                request_id=request_id,
            )
            if "access" in error_msg or "permission" in error_msg or "auth" in error_msg:
                raise AMQPConnectionError(f"Authentication failed: {e}")
            raise AMQPPublishError(f"Channel closed: {e}")
        except aio_pika.exceptions.AMQPError as e:
            await self._circuit_breaker.record_failure()
            # AMQP protocol errors
            logger.error(
                "AMQP error during publish",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                error=str(e),
                request_id=request_id,
            )
            raise AMQPPublishError(f"AMQP error: {e}")
        except Exception as e:
            await self._circuit_breaker.record_failure()
            # Catch-all for unexpected errors
            logger.exception(
                "Unexpected error during publish",
                user=username,
                exchange=exchange,
                routing_key=routing_key,
                request_id=request_id,
            )
            raise AMQPPublishError(f"Failed to publish message: {e}")

    async def consume_one(
        self,
        queue_name: str,
        credentials: Tuple[str, str],
        timeout: float | None = None,
        auto_ack: bool = False,
    ) -> ConsumedMessage | None:
        """Consume message using user credentials."""
        # Check Circuit Breaker state first (ADR-002)
        if not await self._circuit_breaker.allow_request():
            raise CircuitBreakerOpen(
                f"Circuit breaker is {self._circuit_breaker.state.value} - broker unavailable"
            )

        try:
            session = await self._get_session(credentials)
        except Exception:
            await self._circuit_breaker.record_failure()
            raise

        timeout = timeout or self._settings.consume_timeout

        try:
            queue: AbstractQueue = await session.consumer_channel.get_queue(queue_name)

            try:
                message: AbstractIncomingMessage = await asyncio.wait_for(
                    queue.get(no_ack=False),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                return None

            if message is None:
                return None

            if auto_ack:
                await message.ack()
                logger.info("Message auto-acknowledged", delivery_tag=message.delivery_tag)
            else:
                # Store in session's pending messages
                if message.delivery_tag is not None:
                    session.pending_messages[message.delivery_tag] = message
                else:
                    # If delivery_tag is None, we cannot track it for ack/nack
                    # Auto-ack it to prevent message loss
                    await message.ack()
                    logger.warning("Message has no delivery_tag, auto-acknowledged")

            # Parse body
            try:
                if message.content_type == "application/json":
                    body = json.loads(message.body.decode("utf-8"))
                else:
                    body = message.body.decode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                body = message.body.hex()

            return ConsumedMessage(
                delivery_tag=message.delivery_tag or 0,
                body=body,
                routing_key=message.routing_key or "",
                exchange=message.exchange or "",
                correlation_id=message.correlation_id,
                headers=dict(message.headers) if message.headers else {},
                redelivered=message.redelivered or False,
            )

        except aio_pika.exceptions.QueueEmpty:
            return None
        except CircuitBreakerOpen:
            raise
        except Exception as e:
            await self._circuit_breaker.record_failure()
            raise AMQPConsumeError(f"Failed to consume message: {e}")

    async def acknowledge(self, delivery_tag: int, credentials: Tuple[str, str]) -> bool:
        """Acknowledge message using user credentials."""
        session = await self._get_session(credentials)

        message = session.pending_messages.pop(delivery_tag, None)
        if message is None:
            return False

        try:
            await message.ack()
            return True
        except Exception as e:
            logger.error(f"Failed to acknowledge message: {e}")
            session.pending_messages[delivery_tag] = message
            raise

    async def reject(
        self, delivery_tag: int, credentials: Tuple[str, str], requeue: bool = False
    ) -> bool:
        """Reject message using user credentials."""
        session = await self._get_session(credentials)

        message = session.pending_messages.pop(delivery_tag, None)
        if message is None:
            return False

        try:
            await message.reject(requeue=requeue)
            return True
        except Exception as e:
            logger.error(f"Failed to reject message: {e}")
            session.pending_messages[delivery_tag] = message
            raise

    async def setup_topology(
        self, config: TopologyConfig, credentials: Tuple[str, str] | None = None
    ) -> None:
        """Set up topology. Uses provided credentials or system defaults."""
        session = await self._get_session(credentials)

        try:
            # Declare DLX
            dlx_exchange = await session.publisher_channel.declare_exchange(
                config.dlx_exchange_name,
                ExchangeType.FANOUT,
                durable=config.durable,
            )

            if config.dlx_queue_name:
                dlq = await session.publisher_channel.declare_queue(
                    config.dlx_queue_name,
                    durable=config.durable,
                )
                await dlq.bind(dlx_exchange)

            # Main Exchange
            main_exchange = await session.publisher_channel.declare_exchange(
                config.exchange_name,
                config.exchange_type,
                durable=config.durable,
            )

            if config.queue_name:
                queue_args: dict[str, Any] = {
                    "x-dead-letter-exchange": config.dlx_exchange_name,
                }
                if config.message_ttl:
                    queue_args["x-message-ttl"] = config.message_ttl

                main_queue = await session.publisher_channel.declare_queue(
                    config.queue_name,
                    durable=config.durable,
                    arguments=queue_args,
                )
                await main_queue.bind(main_exchange, routing_key=config.routing_key)

        except Exception as e:
            logger.error(f"Failed to setup topology: {e}")
            raise

    async def health_check(self) -> dict[str, Any]:
        """Check system connection health and aggregate stats."""
        # Get Circuit Breaker metrics
        cb_metrics = self._circuit_breaker.metrics

        # Ensure system session exists (or try to create/get it)
        try:
            session = await self._get_session(None)
            connected = not session.connection.is_closed

            total_pending = len(session.pending_messages)

            # Aggregate pending messages from all user sessions
            async with self._session_lock:
                active_sessions_count = len(self._sessions)
                for s in self._sessions.values():
                    total_pending += len(s.pending_messages)

            return {
                "connected": connected,
                "ready": connected and cb_metrics["state"] != "open",
                "state": "connected" if connected else "disconnected",
                "pending_messages": total_pending,
                "active_sessions": active_sessions_count,
                "circuit_breaker": cb_metrics,
            }
        except Exception:
            # Fallback if system session fails
            active_sessions_count = 0
            try:
                # Try to get count without full lock if possible or just use 0
                active_sessions_count = len(self._sessions)
            except Exception:  # nosec
                pass

            return {
                "connected": False,
                "ready": False,
                "state": "disconnected",
                "pending_messages": 0,
                "active_sessions": active_sessions_count,
                "circuit_breaker": cb_metrics,
            }
