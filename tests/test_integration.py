"""Integration tests using Testcontainers or external RabbitMQ."""

import asyncio
import os
import socket
import pytest
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs

def is_port_open(host: str, port: int) -> bool:
    """Check if a TCP port is open."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0

@pytest.fixture(scope="module")
def rabbitmq_container():
    """
    Provide RabbitMQ context.
    
    Strategy:
    1. Check if RabbitMQ is already running on localhost:5672 (CI Service / Local).
    2. If yes, yield None (use existing).
    3. If no, spin up a DockerContainer (Testcontainers).
    """
    # Check for existing instance (common in CI with Service Containers)
    if is_port_open("localhost", 5672):
        yield None
        return

    # Fallback to Testcontainers
    with DockerContainer("rabbitmq:4.0-management") as rabbitmq:
        rabbitmq.with_exposed_ports(5672)
        rabbitmq.start()
        wait_for_logs(rabbitmq, "Server startup complete", timeout=60)
        yield rabbitmq

@pytest.fixture
def integration_app(rabbitmq_container, monkeypatch):
    """Create a FastAPI app instance connected to RabbitMQ."""
    
    if rabbitmq_container:
        # We are using Testcontainers
        host = rabbitmq_container.get_container_host_ip()
        port = rabbitmq_container.get_exposed_port(5672)
        amqp_url = f"amqp://guest:guest@{host}:{port}/"
    else:
        # We are using existing RabbitMQ (localhost:5672)
        # Assuming default guest:guest
        amqp_url = "amqp://guest:guest@localhost:5672/"
    
    # Override environment settings
    monkeypatch.setenv("RABBITMQ_URL", amqp_url)
    monkeypatch.setenv("API_KEY_ENABLED", "false")
    
    # Import here to avoid early config loading before monkeypatch
    from rmq_middleware.config import get_settings
    get_settings.cache_clear()
    
    from rmq_middleware.main import app
    from rmq_middleware.amqp_wrapper import AMQPClient
    
    # Reset singleton to ensure fresh connection
    asyncio.run(AMQPClient.reset_instance())
    
    return app

@pytest.mark.asyncio
async def test_full_integration_flow(integration_app):
    """Test full cycle: Publish -> Fetch -> Ack."""
    
    client = TestClient(integration_app)
    
    # Credentials (default guest:guest)
    username, password = "guest", "guest"
    
    import base64
    auth_str = f"{username}:{password}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {"Authorization": f"Basic {b64_auth}"}

    # 1. Setup Topology
    from rmq_middleware.amqp_wrapper import AMQPClient, TopologyConfig
    amqp_client = await AMQPClient.get_instance()
    await amqp_client.connect()
    
    # Use a unique exchange name to avoid collisions if reusing instance
    import uuid
    unique_suffix = str(uuid.uuid4())[:8]
    ex_name = f"test.integration.ex.{unique_suffix}"
    q_name = f"test.integration.q.{unique_suffix}"
    r_key = "test.key"

    await amqp_client.setup_topology(
        TopologyConfig(
            exchange_name=ex_name,
            queue_name=q_name,
            routing_key=r_key
        )
    )

    # 2. Publish Message via API
    payload = {
        "exchange": ex_name,
        "routing_key": r_key,
        "payload": {"status": "integration_test"},
        "mandatory": True
    }
    
    response = client.post("/v1/publish", json=payload, headers=headers)
    assert response.status_code == 202, f"Publish failed: {response.text}"
    
    # 3. Fetch Message via API
    fetch_payload = {
        "queue": q_name,
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
