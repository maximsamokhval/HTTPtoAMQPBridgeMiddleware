"""Unit tests for AMQPClient."""

import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from rmq_middleware.amqp_wrapper import AMQPClient, AMQPConnectionError, ConnectionState, TopologyConfig

@pytest.fixture
def mock_aio_pika():
    with patch("rmq_middleware.amqp_wrapper.aio_pika") as mock:
        yield mock

@pytest.mark.asyncio
async def test_connect_retry_failure(mock_aio_pika):
    """Test connection retry logic failing after max attempts."""
    # Mock connect_robust to always fail
    mock_aio_pika.connect_robust = AsyncMock(side_effect=Exception("Connection refused"))
    
    client = AMQPClient()
    # Speed up retry by patching sleep
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        # Override settings to fail fast
        client._settings.retry_attempts = 2
        client._settings.retry_base_delay = 0.1
        
        with pytest.raises(AMQPConnectionError) as exc:
            await client.connect()
        
        assert "Failed to connect after 2 attempts" in str(exc.value)
        assert mock_aio_pika.connect_robust.call_count == 2

@pytest.mark.asyncio
async def test_get_session_reconnect(mock_aio_pika):
    """Test session reconnection if closed."""
    client = AMQPClient()
    
    # First connection successful
    mock_conn = AsyncMock()
    mock_conn.is_closed = False
    mock_aio_pika.connect_robust = AsyncMock(return_value=mock_conn)
    
    creds = ("user", "pass")
    
    # Get session first time
    session1 = await client._get_session(creds)
    assert session1.connection == mock_conn
    
    # Simulate closed connection
    mock_conn.is_closed = True
    
    # Get session again - should reconnect
    mock_conn2 = AsyncMock()
    mock_conn2.is_closed = False
    mock_aio_pika.connect_robust = AsyncMock(return_value=mock_conn2)
    
    session2 = await client._get_session(creds)
    assert session2.connection == mock_conn2
    assert session2 != session1

@pytest.mark.asyncio
async def test_shutdown(mock_aio_pika):
    """Test shutdown closes all sessions."""
    client = AMQPClient()
    
    # Create a dummy session manually
    mock_session = AsyncMock()
    client._sessions["key"] = mock_session
    mock_system_session = AsyncMock()
    client._system_session = mock_system_session
    
    await client.shutdown()
    
    mock_session.close.assert_awaited_once()
    mock_system_session.close.assert_awaited_once()
    assert len(client._sessions) == 0
    assert client._system_session is None

@pytest.mark.asyncio
async def test_create_session_channels(mock_aio_pika):
    """Test channel creation during session init."""
    mock_conn = AsyncMock()
    mock_aio_pika.connect_robust = AsyncMock(return_value=mock_conn)
    
    client = AMQPClient()
    await client._create_session("amqp://url")
    
    # Should create 2 channels (pub + sub)
    assert mock_conn.channel.call_count == 2

def test_topology_config_defaults():
    """Test TopologyConfig post-init defaults."""
    config = TopologyConfig(exchange_name="ex")
    assert config.dlx_exchange_name == "ex.dlx"
    assert config.dlx_queue_name == ""
    
    config2 = TopologyConfig(exchange_name="ex", queue_name="q")
    assert config2.dlx_queue_name == "q.dlq"

@pytest.mark.asyncio
async def test_acknowledge_missing_tag():
    """Test acknowledge returns False for unknown tag."""
    client = AMQPClient()
    mock_session = AsyncMock()
    mock_session.pending_messages = {}
    
    # Manually inject session
    client._sessions["user:pass"] = mock_session
    
    # Since _get_session logic is complex/locked, we can mock it 
    # OR we can just rely on the fact that if we call acknowledge with creds, 
    # it calls _get_session.
    # Let's mock _get_session to return our mock session
    with patch.object(client, "_get_session", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_session
        
        result = await client.acknowledge(123, ("user", "pass"))
        assert result is False

@pytest.mark.asyncio
async def test_reject_missing_tag():
    """Test reject returns False for unknown tag."""
    client = AMQPClient()
    mock_session = AsyncMock()
    mock_session.pending_messages = {}
    
    with patch.object(client, "_get_session", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_session
        
        result = await client.reject(123, ("user", "pass"))
        assert result is False

@pytest.mark.asyncio
async def test_health_check_system_failure(mock_aio_pika):
    """Test health check when system connection fails."""
    client = AMQPClient()
    
    # Mock _get_session to raise error for system session
    with patch.object(client, "_get_session", side_effect=Exception("Conn error")):
        health = await client.health_check()
        assert health["connected"] is False
        assert health["state"] == "disconnected"