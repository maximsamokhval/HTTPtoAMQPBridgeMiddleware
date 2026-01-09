"""Pydantic V2 models for RMQ Middleware."""

from enum import IntEnum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, ConfigDict


class DeliveryMode(IntEnum):
    """AMQP Delivery Mode."""
    TRANSIENT = 1
    PERSISTENT = 2


class PublishRequest(BaseModel):
    """Request mode for message publishing."""
    
    model_config = ConfigDict(extra="forbid")

    exchange: str = Field(..., description="Target Exchange name")
    routing_key: str = Field(..., description="Routing key for message distribution")
    payload: Dict[str, Any] = Field(..., description="JSON-serializable message body")
    
    # Reliability Parameters
    mandatory: bool = Field(default=True, description="Return message if unroutable")
    persistence: DeliveryMode = Field(default=DeliveryMode.PERSISTENT, description="1=Transient, 2=Persistent")
    
    # Metadata for Message Tracing (Idempotency)
    correlation_id: Optional[str] = Field(None, description="Request-Response identifier")
    message_id: Optional[str] = Field(None, description="Unique message ID for deduplication")
    headers: Optional[Dict[str, str]] = Field(None, description="Custom AMQP headers")


class FetchRequest(BaseModel):
    """Request model for message fetching (pull)."""
    
    model_config = ConfigDict(extra="forbid")

    queue: str = Field(..., description="Queue name to consume from")
    prefetch_count: int = Field(default=10, ge=1, le=100, description="Max messages to prefetch")
    timeout: int = Field(default=30, ge=0, le=300, description="Seconds to wait for data")
    auto_ack: bool = Field(default=False, description="Explicit ACK is required for EDI")
