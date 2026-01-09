"""Unit tests for API routes."""

from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient

from rmq_middleware.main import app
from rmq_middleware.amqp_wrapper import AMQPClient, ConsumedMessage

client = TestClient(app)

# Mock headers for Basic Auth (user:pass) -> base64 "dXNlcjpwYXNz"
AUTH_HEADERS = {"Authorization": "Basic dXNlcjpwYXNz"}
CREDENTIALS_TUPLE = ("user", "pass")

@pytest.fixture
def mock_amqp():
    """Mock AMQPClient singleton."""
    with patch("rmq_middleware.routes.AMQPClient.get_instance", new_callable=AsyncMock) as mock_get:
        mock_client = AsyncMock(spec=AMQPClient)
        mock_get.return_value = mock_client
        yield mock_client

@pytest.fixture
def override_settings(monkeypatch):
    """Override settings for testing."""
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")

class TestPublishRoute:
    """Tests for POST /v1/publish."""

    def test_publish_success(self, mock_amqp, override_settings):
        """Test successful publish."""
        mock_amqp.publish.return_value = None  # publish returns None on success

        payload = {
            "exchange": "test.ex",
            "routing_key": "key",
            "payload": {"msg": "hello"},
            "headers": {"x-test": "1"},
        }
        response = client.post("/v1/publish", json=payload, headers=AUTH_HEADERS)
        
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "accepted"
        assert data["exchange"] == "test.ex"
        
        # Verify AMQP call
        mock_amqp.publish.assert_awaited_once()
        call_args = mock_amqp.publish.call_args[1]
        assert call_args["exchange"] == "test.ex"
        assert call_args["payload"] == {"msg": "hello"}
        assert call_args["credentials"] == CREDENTIALS_TUPLE
        assert call_args["persistent"] is True  # Default
        assert call_args["priority"] == 0  # Default

    def test_publish_no_auth(self, mock_amqp, override_settings):
        """Test publish without credentials."""
        payload = {
            "exchange": "test.ex",
            "routing_key": "key",
            "payload": {"msg": "hello"},
        }
        response = client.post("/v1/publish", json=payload)
        assert response.status_code == 401

    def test_publish_validation_error(self, mock_amqp, override_settings):
        """Test validation error (missing field)."""
        payload = {
            "exchange": "test.ex",
            # Missing routing_key
            "payload": {},
        }
        response = client.post("/v1/publish", json=payload, headers=AUTH_HEADERS)
        assert response.status_code == 422


class TestHealthRoutes:
    """Tests for health and readiness probes."""

    def test_health_check(self):
        """Test GET /health."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "service": "rmq-middleware"}

    def test_readiness_check_success(self, mock_amqp):
        """Test GET /ready success."""
        mock_amqp.health_check.return_value = {
            "connected": True,
            "ready": True,
            "state": "connected",
            "pending_messages": 0,
            "active_sessions": 1
        }
        
        response = client.get("/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["amqp_ready"] is True
        assert data["active_sessions"] == 1

    def test_readiness_check_failure(self, mock_amqp):
        """Test GET /ready failure when AMQP not ready."""
        mock_amqp.health_check.return_value = {
            "connected": False,
            "ready": False,
            "state": "disconnected",
            "pending_messages": 0,
            "active_sessions": 0
        }
        
        response = client.get("/ready")
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "not_ready"

    def test_publish_business_validation(self, mock_amqp, override_settings):
        """Test business logic validation (invalid characters)."""
        payload = {
            "exchange": "bad..exchange",
            "routing_key": "key",
            "payload": {},
        }
        response = client.post("/v1/publish", json=payload, headers=AUTH_HEADERS)
        assert response.status_code == 400
        assert "validation_error" in response.json()["detail"]["error"]


class TestFetchRoute:
    """Tests for POST /v1/fetch."""

    def test_fetch_success(self, mock_amqp, override_settings):
        """Test successful fetch."""
        mock_msg = ConsumedMessage(
            delivery_tag=1,
            body={"data": "test"},
            routing_key="rk",
            exchange="ex",
            correlation_id="cid",
            headers={},
            redelivered=False
        )
        mock_amqp.consume_one.return_value = mock_msg

        payload = {"queue": "q", "timeout": 5}
        response = client.post("/v1/fetch", json=payload, headers=AUTH_HEADERS)
        
        assert response.status_code == 200
        data = response.json()
        assert data["delivery_tag"] == 1
        assert data["body"] == {"data": "test"}
        
        # Verify credentials passed
        mock_amqp.consume_one.assert_awaited_once()
        assert mock_amqp.consume_one.call_args[1]["credentials"] == CREDENTIALS_TUPLE

    def test_fetch_timeout_no_content(self, mock_amqp, override_settings):
        """Test fetch timeout (204 No Content)."""
        mock_amqp.consume_one.return_value = None
        
        payload = {"queue": "q", "timeout": 1}
        response = client.post("/v1/fetch", json=payload, headers=AUTH_HEADERS)
        
        assert response.status_code == 204

    def test_fetch_validation(self, mock_amqp, override_settings):
        """Test fetch validation (invalid timeout)."""
        payload = {"queue": "q", "timeout": -1}
        response = client.post("/v1/fetch", json=payload, headers=AUTH_HEADERS)
        # Timeout validation happens in Pydantic model
        assert response.status_code == 422

    def test_ack_success(self, mock_amqp, override_settings):
        """Test successful ack."""
        mock_amqp.acknowledge.return_value = True
        
        response = client.post("/v1/ack/1", headers=AUTH_HEADERS)
        
        assert response.status_code == 200
        assert response.json()["status"] == "acknowledged"
        
        mock_amqp.acknowledge.assert_awaited_once_with(1, credentials=CREDENTIALS_TUPLE)
