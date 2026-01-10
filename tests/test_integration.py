"""Integration tests using Testcontainers or external RabbitMQ."""

import asyncio
import socket
import logging
import pytest
from fastapi.testclient import TestClient
from testcontainers.core.container import DockerContainer
from testcontainers.core.waiting_utils import wait_for_logs
import aio_pika

logger = logging.getLogger(__name__)

def is_port_open(host: str, port: int) -> bool:
    """Check if a TCP port is open."""
    logger.info(f"Checking if {host}:{port} is open...")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        result = s.connect_ex((host, port)) == 0
        logger.info(f"Port {port} open: {result}")
        return result

@pytest.fixture(scope="module")
def rabbitmq_container():
    """
    Provide RabbitMQ context.
    """
    if is_port_open("localhost", 5672):
        logger.info("Using existing RabbitMQ instance on localhost:5672")
        yield None
        return

    logger.info("Falling back to Testcontainers (DockerContainer)...")
    with DockerContainer("rabbitmq:4.0-management") as rabbitmq:
        rabbitmq.with_exposed_ports(5672)
        rabbitmq.start()
        logger.info("Waiting for RabbitMQ to start (looking for 'Server startup complete')...")
        wait_for_logs(rabbitmq, "Server startup complete", timeout=60)
        logger.info("RabbitMQ container is ready.")
        yield rabbitmq

@pytest.fixture
def integration_app(rabbitmq_container, monkeypatch):
    """Create a FastAPI app instance connected to RabbitMQ."""
    
    if rabbitmq_container:
        host = rabbitmq_container.get_container_host_ip()
        port = rabbitmq_container.get_exposed_port(5672)
        amqp_url = f"amqp://guest:guest@{host}:{port}/"
    else:
        amqp_url = "amqp://guest:guest@localhost:5672/"
    
    logger.info(f"Connecting integration app to: {amqp_url}")
    monkeypatch.setenv("RABBITMQ_URL", amqp_url)
    monkeypatch.setenv("API_KEY_ENABLED", "false")
    
    from rmq_middleware.config import get_settings
    get_settings.cache_clear()
    
    from rmq_middleware.main import app
    from rmq_middleware.amqp_wrapper import AMQPClient
    
    logger.info("Resetting AMQPClient instance for integration tests...")
    # We must reset this because unit tests might have mocked it or left it dirty
    asyncio.run(AMQPClient.reset_instance())
    
    return app

@pytest.fixture
def amqp_url(rabbitmq_container):
    """Return the AMQP URL string."""
    if rabbitmq_container:
        host = rabbitmq_container.get_container_host_ip()
        port = rabbitmq_container.get_exposed_port(5672)
        return f"amqp://guest:guest@{host}:{port}/"
    return "amqp://guest:guest@localhost:5672/"

@pytest.mark.asyncio
async def test_full_integration_flow(integration_app, amqp_url):
    """Test full cycle: Publish -> Fetch -> Ack."""
    
    # 1. Setup Topology using a separate, independent connection
    # This avoids "Event Loop" conflicts between TestClient and the App's internal singleton
    logger.info("Step 1: Setting up topology (external)...")
    
    import uuid
    unique_suffix = str(uuid.uuid4())[:8]
    ex_name = f"test.integration.ex.{unique_suffix}"
    q_name = f"test.integration.q.{unique_suffix}"
    r_key = "test.key"
    
    dlx_name = f"{ex_name}.dlx"
    
    connection = await aio_pika.connect_robust(amqp_url)
    async with connection:
        channel = await connection.channel()
        
        # Declare DLX
        await channel.declare_exchange(dlx_name, aio_pika.ExchangeType.FANOUT, durable=True)
        
        # Declare Main Exchange
        exchange = await channel.declare_exchange(ex_name, aio_pika.ExchangeType.TOPIC, durable=True)
        
        # Declare Queue with DLX
        queue = await channel.declare_queue(
            q_name,
            durable=True,
            arguments={"x-dead-letter-exchange": dlx_name}
        )
        await queue.bind(exchange, routing_key=r_key)
        logger.info(f"Topology set: Exchange={ex_name}, Queue={q_name}")

    # 2. Use TestClient to interact with the API
    with TestClient(integration_app) as client:
        logger.info("TestClient initialized (Lifespan started).")
        
        username, password = "guest", "guest"
        import base64
        auth_str = f"{username}:{password}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        headers = {"Authorization": f"Basic {b64_auth}"}

        # 2. Publish Message
        logger.info("Step 2: Publishing message...")
        payload = {
            "exchange": ex_name,
            "routing_key": r_key,
            "payload": {"status": "integration_test"},
            "mandatory": True
        }
        
        response = client.post("/v1/publish", json=payload, headers=headers)
        logger.info(f"Publish response: {response.status_code}")
        assert response.status_code == 202
        
        # 3. Fetch Message
        logger.info("Step 3: Fetching message...")
        fetch_payload = {
            "queue": q_name,
            "timeout": 5,
            "auto_ack": False
        }
        
        response = client.post("/v1/fetch", json=fetch_payload, headers=headers)
        logger.info(f"Fetch response: {response.status_code}")
        assert response.status_code == 200
        
        data = response.json()
        logger.info(f"Message body received: {data['body']}")
        assert data["body"] == {"status": "integration_test"}
        delivery_tag = data["delivery_tag"]
        
        # 4. Ack Message
        logger.info(f"Step 4: Acknowledging message (tag={delivery_tag})...")
        ack_response = client.post(f"/v1/ack/{delivery_tag}", headers=headers)
        logger.info(f"Ack response: {ack_response.status_code}")
        assert ack_response.status_code == 200
        
        # 5. Verify Empty
        logger.info("Step 5: Verifying queue is empty...")
        response_empty = client.post("/v1/fetch", json=fetch_payload, headers=headers)
        logger.info(f"Final fetch response: {response_empty.status_code}")
        assert response_empty.status_code == 204
        logger.info("Integration test PASSED.")