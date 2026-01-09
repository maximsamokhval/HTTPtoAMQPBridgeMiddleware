"""Integration tests using Testcontainers."""

import asyncio
import os
import pytest
from fastapi.testclient import TestClient
from testcontainers.rabbitmq import RabbitMqContainer
import time

# Use a separate scope for integration tests if needed, but standard pytest is fine.
# We need to make sure we don't depend on the 'mock_amqp' fixture from other tests.

@pytest.fixture(scope="module")
def rabbitmq_container():
    """Spin up a RabbitMQ container."""
    # rabbitmq:3.11-management-alpine is a good lightweight default
    with RabbitMqContainer("rabbitmq:3.11-management-alpine") as rabbitmq:
        yield rabbitmq

@pytest.fixture
def integration_app(rabbitmq_container, monkeypatch):
    """Create a FastAPI app instance connected to the container."""
    
    # Manually construct URL since get_connection_url might be missing/broken
    host = rabbitmq_container.get_container_host_ip()
    port = rabbitmq_container.get_exposed_port(5672)
    amqp_url = f"amqp://guest:guest@{host}:{port}/"
    
    # Override environment settings
    monkeypatch.setenv("RABBITMQ_URL", amqp_url)
    # Ensure other settings are defaults
    monkeypatch.setenv("API_KEY_ENABLED", "false") # Basic Auth is default now
    
    # Import here to avoid early config loading before monkeypatch
    # We need to reload settings or create a fresh app
    from rmq_middleware.config import get_settings
    get_settings.cache_clear()
    
    from rmq_middleware.main import app
    from rmq_middleware.amqp_wrapper import AMQPClient
    
    # Reset singleton to ensure fresh connection to container
    # (Since other unit tests might have mocked it)
    asyncio.run(AMQPClient.reset_instance())
    
    return app

@pytest.mark.asyncio
async def test_full_integration_flow(integration_app, rabbitmq_container):
    """Test full cycle: Publish -> Fetch -> Ack."""
    
    client = TestClient(integration_app)
    
    # Construct credentials manually
    username, password = "guest", "guest"
    
    import base64
    auth_str = f"{username}:{password}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {b64_auth}"}

    # 1. Setup Topology (using publish to auto-declare or implicit)
    # But wait, our Publish endpoint validates exchange existence? 
    # AMQPClient.publish uses the channel to 'get_exchange'. 
    # If we use a default exchange like "amq.topic", it should exist.
    # Or we rely on the fact that our code creates the exchange?
    # Actually, the code expects the exchange to exist usually, OR we declare it.
    # Looking at amqp_wrapper.py -> publish -> get_exchange(exchange)
    # If the exchange doesn't exist, aio-pika's get_exchange might fail if we don't ensure it exists.
    # Let's use the AMQPClient to setup topology first via a "hack" or just rely on amq.topic
    
    # Better: Use the internal AMQPClient to setup a test queue/exchange first
    from rmq_middleware.amqp_wrapper import AMQPClient, TopologyConfig
    amqp_client = await AMQPClient.get_instance()
    await amqp_client.connect() # Connects with system/default creds from env
    
    # Create topology
    await amqp_client.setup_topology(
        TopologyConfig(
            exchange_name="test.integration.ex",
            queue_name="test.integration.q",
            routing_key="test.key"
        )
    )

    # 2. Publish Message via API
    payload = {
        "exchange": "test.integration.ex",
        "routing_key": "test.key",
        "payload": {"status": "integration_test"},
        "mandatory": True
    }
    
    response = client.post("/v1/publish", json=payload, headers=headers)
    assert response.status_code == 202, f"Publish failed: {response.text}"
    
    # 3. Fetch Message via API
    fetch_payload = {
        "queue": "test.integration.q",
        "timeout": 5,
        "auto_ack": False
    }
    
    response = client.post("/v1/fetch", json=fetch_payload, headers=headers)
    assert response.status_code == 200, f"Fetch failed: {response.text}"
    
    data = response.json()
    assert data["body"] == {"status": "integration_test"}
    delivery_tag = data["delivery_tag"]
    
    # 4. Ack Message
    ack_response = client.post(f"/v1/ack/{delivery_tag}", headers=headers)
    assert ack_response.status_code == 200
    assert ack_response.json()["status"] == "acknowledged"
    
    # 5. Verify Queue is Empty
    response_empty = client.post("/v1/fetch", json=fetch_payload, headers=headers)
    assert response_empty.status_code == 204

    # Cleanup handled by container shutdown
