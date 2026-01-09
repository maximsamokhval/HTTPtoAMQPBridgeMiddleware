"""Unit tests for Pydantic data models."""

import pytest
from pydantic import ValidationError

from rmq_middleware.models import (
    DeliveryMode,
    FetchRequest,
    PublishRequest,
)


class TestPublishRequest:
    """Tests for PublishRequest model."""

    def test_valid_minimal_request(self):
        """Test minimal valid request."""
        data = {
            "exchange": "test.ex",
            "routing_key": "key",
            "payload": {"data": 1},
        }
        model = PublishRequest(**data)
        assert model.exchange == "test.ex"
        assert model.routing_key == "key"
        assert model.payload == {"data": 1}
        assert model.mandatory is True
        assert model.persistence == DeliveryMode.PERSISTENT
        assert model.correlation_id is None

    def test_full_request(self):
        """Test request with all fields."""
        data = {
            "exchange": "test.ex",
            "routing_key": "key",
            "payload": "string payload",
            "mandatory": False,
            "persistence": 1,  # TRANSIENT
            "correlation_id": "cid-123",
            "message_id": "mid-456",
            "headers": {"x-custom": "value"},
        }
        model = PublishRequest(**data)
        assert model.payload == "string payload"
        assert model.persistence == DeliveryMode.TRANSIENT

    def test_invalid_persistence_enum(self):
        """Test invalid delivery mode."""
        data = {
            "exchange": "ex",
            "routing_key": "rk",
            "payload": {},
            "persistence": 3,
        }
        with pytest.raises(ValidationError):
            PublishRequest(**data)

    def test_missing_required_fields(self):
        """Test missing fields."""
        with pytest.raises(ValidationError) as exc:
            PublishRequest(exchange="ex")
        assert "routing_key" in str(exc.value)
        assert "payload" in str(exc.value)

    def test_extra_fields_forbidden(self):
        """Test that extra fields are forbidden."""
        data = {
            "exchange": "ex",
            "routing_key": "rk",
            "payload": {},
            "unexpected_field": "error",
        }
        with pytest.raises(ValidationError):
            PublishRequest(**data)


class TestFetchRequest:
    """Tests for FetchRequest model."""

    def test_valid_defaults(self):
        """Test valid request with defaults."""
        model = FetchRequest(queue="my.queue")
        assert model.queue == "my.queue"
        assert model.timeout == 30
        assert model.auto_ack is False

    @pytest.mark.parametrize("timeout", [-1, 301])
    def test_timeout_bounds(self, timeout):
        """Test timeout boundaries (0-300)."""
        with pytest.raises(ValidationError):
            FetchRequest(queue="q", timeout=timeout)