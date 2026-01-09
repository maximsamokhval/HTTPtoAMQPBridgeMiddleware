"""Integration tests using Testcontainers."""

import asyncio
import os
import pytest
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

@pytest.fixture(scope="module")
def rabbitmq_container():
    """Spin up a RabbitMQ container using generic DockerContainer.
    
    We use generic DockerContainer instead of RabbitMqContainer to avoid 
    dependency on 'pika' and potential protocol conflicts during health checks.
    """
    # rabbitmq:3.11-management-alpine is a good lightweight default
    with DockerContainer("rabbitmq:3.11-management-alpine") as rabbitmq:
        # Expose standard AMQP port
        rabbitmq.with_exposed_ports(5672)
        
        # Start the container
        rabbitmq.start()
        
        # Wait for RabbitMQ to be fully ready by checking logs
        # This is more robust than TCP checks in CI environments
        wait_for_logs(rabbitmq, "Server startup complete", timeout=60)
        
        yield rabbitmq

@pytest.fixture
def integration_app(rabbitmq_container, monkeypatch):
    """Create a FastAPI app instance connected to the container."""
    
    # Manually construct URL
    host = rabbitmq_container.get_container_host_ip()
    port = rabbitmq_container.get_exposed_port(5672)
    amqp_url = f"amqp://guest:guest@{host}:{port}/"
    
    # Override environment settings
    monkeypatch.setenv("RABBITMQ_URL", amqp_url)
    monkeypatch.setenv("API_KEY_ENABLED", "false")
    
    # Import here to avoid early config loading before monkeypatch
    from rmq_middleware.config import get_settings
    get_settings.cache_clear()
    
    from rmq_middleware.main import app
    from rmq_middleware.amqp_wrapper import AMQPClient
    
    # Reset singleton to ensure fresh connection to container
    asyncio.run(AMQPClient.reset_instance())
    
    return app

@pytest.mark.asyncio
async def test_full_integration_flow(integration_app, rabbitmq_container):
    """Test full cycle: Publish -> Fetch -> Ack."""
    
    client = TestClient(integration_app)
    
    # Default credentials for the image are guest:guest
    username, password = "guest", "guest"
    
    import base64
    auth_str = f"{username}:{password}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {b64_auth}"}

    # 1. Setup Topology
    from rmq_middleware.amqp_wrapper import AMQPClient, TopologyConfig
    amqp_client = await AMQPClient.get_instance()
    await amqp_client.connect()
    
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