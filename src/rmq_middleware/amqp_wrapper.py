"""Robust AMQP client wrapper with connection management and message operations.

Provides a singleton-like AMQPClient class that handles:
- Connection with exponential backoff reconnection
- Publisher confirms for reliable message delivery
- Long-polling message consumption
- Message acknowledgment and rejection
- Topology setup with Dead Letter Exchange support
"""

import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import aio_pika
from aio_pika import ExchangeType, Message, DeliveryMode
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
)
from loguru import logger

from rmq_middleware.config import Settings, get_settings
from rmq_middleware.middleware import get_request_id


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


class AMQPClient:
    """Robust AMQP client with connection management and message operations.
    
    Implements the singleton pattern to ensure a single connection is shared
    across the application. Handles automatic reconnection with exponential
    backoff and provides publisher confirms for reliable delivery.
    
    Usage:
        client = AMQPClient.get_instance()
        await client.connect()
        await client.publish("exchange", "routing.key", {"message": "hello"})
        await client.disconnect()
    """
    
    _instance: "AMQPClient | None" = None
    _lock: asyncio.Lock = asyncio.Lock()
    
    def __init__(self, settings: Settings | None = None):
        """Initialize AMQP client.
        
        Note: Use get_instance() for singleton access.
        """
        self._settings = settings or get_settings()
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._state = ConnectionState.DISCONNECTED
        self._reconnect_task: asyncio.Task | None = None
        
        # Track pending messages for acknowledgment
        self._pending_messages: dict[int, AbstractIncomingMessage] = {}
        
        # Channel for consuming messages
        self._consumer_channel: AbstractChannel | None = None
    
    @classmethod
    async def get_instance(cls, settings: Settings | None = None) -> "AMQPClient":
        """Get or create singleton instance.
        
        Thread-safe singleton implementation using asyncio.Lock.
        """
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls(settings)
            return cls._instance
    
    @classmethod
    async def reset_instance(cls) -> None:
        """Reset singleton instance (for testing)."""
        async with cls._lock:
            if cls._instance is not None:
                await cls._instance.disconnect()
                cls._instance = None
    
    @property
    def state(self) -> ConnectionState:
        """Current connection state."""
        return self._state
    
    @property
    def is_connected(self) -> bool:
        """Check if connection is established."""
        return (
            self._state == ConnectionState.CONNECTED
            and self._connection is not None
            and not self._connection.is_closed
        )
    
    @property
    def is_ready(self) -> bool:
        """Check if client is ready for operations.
        
        Used for readiness probes - returns True only when both
        connection and channel are available.
        """
        return (
            self.is_connected
            and self._channel is not None
            and not self._channel.is_closed
        )
    
    async def connect(self) -> None:
        """Establish connection to RabbitMQ with exponential backoff.
        
        Retries connection with increasing delays on failure:
        delay = base_delay * (2 ** attempt)
        
        Raises:
            AMQPConnectionError: If max retries exceeded.
        """
        if self._state == ConnectionState.CONNECTED:
            logger.debug("Already connected to RabbitMQ")
            return
        
        self._state = ConnectionState.CONNECTING
        
        settings = self._settings
        last_error: Exception | None = None
        
        for attempt in range(settings.retry_attempts):
            try:
                delay = settings.retry_base_delay * (2 ** attempt)
                
                if attempt > 0:
                    logger.warning(
                        f"Retrying connection in {delay:.1f}s",
                        attempt=attempt + 1,
                        max_attempts=settings.retry_attempts,
                    )
                    await asyncio.sleep(delay)
                
                logger.info(
                    "Connecting to RabbitMQ",
                    url=settings.rabbitmq_url_masked,
                    attempt=attempt + 1,
                )
                
                # Establish connection
                self._connection = await aio_pika.connect_robust(
                    settings.rabbitmq_url_str,
                    timeout=30.0,
                )
                
                # Set up connection close callback
                self._connection.close_callbacks.add(self._on_connection_close)
                
                # Create publisher channel with confirms
                self._channel = await self._connection.channel()
                await self._channel.set_qos(prefetch_count=settings.rabbitmq_prefetch_count)
                
                # Create consumer channel
                self._consumer_channel = await self._connection.channel()
                await self._consumer_channel.set_qos(prefetch_count=settings.rabbitmq_prefetch_count)
                
                self._state = ConnectionState.CONNECTED
                
                logger.info(
                    "Connected to RabbitMQ",
                    url=settings.rabbitmq_url_masked,
                )
                return
                
            except Exception as e:
                last_error = e
                logger.error(
                    f"Connection attempt failed: {e}",
                    attempt=attempt + 1,
                    max_attempts=settings.retry_attempts,
                )
        
        self._state = ConnectionState.DISCONNECTED
        raise AMQPConnectionError(
            f"Failed to connect after {settings.retry_attempts} attempts: {last_error}"
        )
    
    async def disconnect(self) -> None:
        """Gracefully disconnect from RabbitMQ.
        
        Closes channels first, then the connection. Cancels any
        pending reconnection tasks.
        """
        if self._state == ConnectionState.DISCONNECTED:
            return
        
        self._state = ConnectionState.DISCONNECTING
        
        logger.info("Disconnecting from RabbitMQ")
        
        # Cancel reconnection task if running
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        
        # Clear pending messages
        self._pending_messages.clear()
        
        # Close consumer channel
        if self._consumer_channel and not self._consumer_channel.is_closed:
            try:
                await self._consumer_channel.close()
            except Exception as e:
                logger.warning(f"Error closing consumer channel: {e}")
        
        # Close publisher channel
        if self._channel and not self._channel.is_closed:
            try:
                await self._channel.close()
            except Exception as e:
                logger.warning(f"Error closing publisher channel: {e}")
        
        # Close connection
        if self._connection and not self._connection.is_closed:
            try:
                await self._connection.close()
            except Exception as e:
                logger.warning(f"Error closing connection: {e}")
        
        self._connection = None
        self._channel = None
        self._consumer_channel = None
        self._state = ConnectionState.DISCONNECTED
        
        logger.info("Disconnected from RabbitMQ")
    
    def _on_connection_close(self, *args) -> None:
        """Callback when connection is unexpectedly closed.
        
        Triggers background reconnection task.
        """
        if self._state == ConnectionState.DISCONNECTING:
            return
        
        logger.warning("RabbitMQ connection closed unexpectedly")
        self._state = ConnectionState.DISCONNECTED
        
        # Start reconnection in background
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())
    
    async def _reconnect(self) -> None:
        """Background task for automatic reconnection."""
        logger.info("Starting automatic reconnection")
        try:
            await self.connect()
        except AMQPConnectionError as e:
            logger.error(f"Automatic reconnection failed: {e}")
    
    async def publish(
        self,
        exchange: str,
        routing_key: str,
        payload: dict[str, Any] | str | bytes,
        headers: dict[str, Any] | None = None,
        persistent: bool = True,
        mandatory: bool = True,
        message_id: str | None = None,
        correlation_id: str | None = None,
        priority: int = 0,
    ) -> None:
        """Publish message with publisher confirms.
        
        Args:
            exchange: Target exchange name.
            routing_key: Message routing key.
            payload: Message body (dict will be JSON serialized).
            headers: Optional message headers.
            headers: Optional message headers.
            persistent: If True, message survives broker restart.
            mandatory: If True, message is returned if unroutable.
            message_id: Optional unique message identifier.
            correlation_id: Optional request-response identifier.
            priority: Message priority (0-255).
        
        Raises:
            AMQPPublishError: If publishing fails or times out.
            AMQPConnectionError: If not connected.
        """
        if not self.is_ready:
            raise AMQPConnectionError("Not connected to RabbitMQ")
        
        # Get correlation ID from request context if not provided
        if not correlation_id:
            correlation_id = get_request_id() or None
        
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
             # Fallback for other serializable types
             body = str(payload).encode("utf-8")
             content_type = "text/plain"
        
        # Build message
        message = Message(
            body=body,
            correlation_id=correlation_id,
            message_id=message_id,
            headers=headers or {},
            delivery_mode=DeliveryMode.PERSISTENT if persistent else DeliveryMode.NOT_PERSISTENT,
            priority=priority,
        )
        
        try:
            # Get or declare exchange
            amqp_exchange = await self._channel.get_exchange(exchange)
            
            # Publish with confirmation
            await asyncio.wait_for(
                amqp_exchange.publish(
                    message,
                    routing_key=routing_key,
                    mandatory=mandatory,
                ),
                timeout=self._settings.publish_timeout,
            )
            
            logger.info(
                "Message published",
                exchange=exchange,
                routing_key=routing_key,
                correlation_id=message.correlation_id,
                message_id=message.message_id,
                persistent=persistent,
                mandatory=mandatory,
                priority=priority,
            )
            
        except asyncio.TimeoutError:
            raise AMQPPublishError(
                f"Publish confirmation timeout after {self._settings.publish_timeout}s"
            )
        except Exception as e:
            raise AMQPPublishError(f"Failed to publish message: {e}")
    
    async def consume_one(
        self,
        queue_name: str,
        timeout: float | None = None,
        auto_ack: bool = False,
    ) -> ConsumedMessage | None:
        """Consume a single message from queue (long-polling style).
        
        Args:
            queue_name: Name of the queue to consume from.
            timeout: Maximum time to wait for a message.
            auto_ack: If True, message is acknowledged immediately upon receipt.
        
        Returns:
            ConsumedMessage if message received, None if timeout.
        
        Raises:
            AMQPConsumeError: If consumption fails.
            AMQPConnectionError: If not connected.
        """
        if not self.is_ready:
            raise AMQPConnectionError("Not connected to RabbitMQ")
        
        timeout = timeout or self._settings.consume_timeout
        
        try:
            # Get queue reference
            queue: AbstractQueue = await self._consumer_channel.get_queue(queue_name)
            
            # Try to get a message with timeout
            try:
                message: AbstractIncomingMessage = await asyncio.wait_for(
                    queue.get(no_ack=False),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.debug(f"No message available in queue {queue_name}")
                return None
            
            if message is None:
                return None
            
            # Use explicit ACK if auto_ack is False (default behavior kept for backward compatibility if needed)
            if auto_ack:
                 await message.ack()
                 logger.info("Message auto-acknowledged", delivery_tag=message.delivery_tag)
            else:
                 # Store message for later acknowledgment
                 self._pending_messages[message.delivery_tag] = message
            
            # Parse body
            try:
                if message.content_type == "application/json":
                    body = json.loads(message.body.decode("utf-8"))
                else:
                    body = message.body.decode("utf-8")
            except (json.JSONDecodeError, UnicodeDecodeError):
                body = message.body.hex()
            
            consumed = ConsumedMessage(
                delivery_tag=message.delivery_tag,
                body=body,
                routing_key=message.routing_key or "",
                exchange=message.exchange or "",
                correlation_id=message.correlation_id,
                headers=dict(message.headers) if message.headers else {},
                redelivered=message.redelivered,
            )
            
            logger.info(
                "Message consumed",
                queue=queue_name,
                delivery_tag=message.delivery_tag,
                redelivered=message.redelivered,
            )
            
            return consumed
            
        except aio_pika.exceptions.QueueEmpty:
            return None
        except Exception as e:
            raise AMQPConsumeError(f"Failed to consume message: {e}")
    
    async def acknowledge(self, delivery_tag: int) -> bool:
        """Acknowledge successful message processing.
        
        Args:
            delivery_tag: The delivery tag of the message to acknowledge.
        
        Returns:
            True if acknowledged, False if delivery_tag not found.
        
        Raises:
            AMQPConnectionError: If not connected.
        """
        if not self.is_ready:
            raise AMQPConnectionError("Not connected to RabbitMQ")
        
        message = self._pending_messages.pop(delivery_tag, None)
        if message is None:
            logger.warning(f"Delivery tag {delivery_tag} not found in pending messages")
            return False
        
        try:
            await message.ack()
            logger.info("Message acknowledged", delivery_tag=delivery_tag)
            return True
        except Exception as e:
            logger.error(f"Failed to acknowledge message: {e}")
            # Put back in pending if ack failed
            self._pending_messages[delivery_tag] = message
            raise
    
    async def reject(self, delivery_tag: int, requeue: bool = False) -> bool:
        """Reject a message.
        
        Args:
            delivery_tag: The delivery tag of the message to reject.
            requeue: If True, message is requeued; if False, goes to DLX.
        
        Returns:
            True if rejected, False if delivery_tag not found.
        
        Raises:
            AMQPConnectionError: If not connected.
        """
        if not self.is_ready:
            raise AMQPConnectionError("Not connected to RabbitMQ")
        
        message = self._pending_messages.pop(delivery_tag, None)
        if message is None:
            logger.warning(f"Delivery tag {delivery_tag} not found in pending messages")
            return False
        
        try:
            await message.reject(requeue=requeue)
            logger.info(
                "Message rejected",
                delivery_tag=delivery_tag,
                requeue=requeue,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to reject message: {e}")
            self._pending_messages[delivery_tag] = message
            raise
    
    async def setup_topology(self, config: TopologyConfig) -> None:
        """Set up exchange and queue topology with DLX support.
        
        Creates exchanges and queues idempotently. If they already exist
        with matching configuration, no error is raised.
        
        Args:
            config: Topology configuration.
        
        Raises:
            AMQPConnectionError: If not connected.
        """
        if not self.is_ready:
            raise AMQPConnectionError("Not connected to RabbitMQ")
        
        logger.info(
            "Setting up topology",
            exchange=config.exchange_name,
            queue=config.queue_name,
        )
        
        try:
            # Declare DLX exchange
            dlx_exchange = await self._channel.declare_exchange(
                config.dlx_exchange_name,
                ExchangeType.FANOUT,
                durable=config.durable,
            )
            
            # Declare DLQ if queue name is provided
            if config.dlx_queue_name:
                dlq = await self._channel.declare_queue(
                    config.dlx_queue_name,
                    durable=config.durable,
                )
                await dlq.bind(dlx_exchange)
            
            # Declare main exchange
            main_exchange = await self._channel.declare_exchange(
                config.exchange_name,
                config.exchange_type,
                durable=config.durable,
            )
            
            # Declare main queue with DLX
            if config.queue_name:
                queue_args = {
                    "x-dead-letter-exchange": config.dlx_exchange_name,
                }
                if config.message_ttl:
                    queue_args["x-message-ttl"] = config.message_ttl
                
                main_queue = await self._channel.declare_queue(
                    config.queue_name,
                    durable=config.durable,
                    arguments=queue_args,
                )
                await main_queue.bind(main_exchange, routing_key=config.routing_key)
            
            logger.info(
                "Topology setup complete",
                exchange=config.exchange_name,
                queue=config.queue_name,
                dlx_exchange=config.dlx_exchange_name,
            )
            
        except Exception as e:
            logger.error(f"Failed to setup topology: {e}")
            raise
    
    async def health_check(self) -> dict[str, Any]:
        """Perform health check on AMQP connection.
        
        Returns:
            Dictionary with connection status details.
        """
        return {
            "connected": self.is_connected,
            "ready": self.is_ready,
            "state": self._state.value,
            "pending_messages": len(self._pending_messages),
        }
