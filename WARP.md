# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

HTTP-to-AMQP Bridge Middleware for ERP integration (1C:Enterprise). Converts HTTP REST requests to AMQP messages with reliable message delivery using RabbitMQ Publisher Confirms. Written in Python 3.11+ using FastAPI and aio-pika.

**Key Features:**
- Protocol bridging between HTTP clients and RabbitMQ
- At-Least-Once delivery guarantees via Publisher Confirms
- Long-polling message consumption
- Automatic reconnection with exponential backoff
- Structured JSON logging with correlation IDs

## Development Commands

### Environment Setup
```bash
# Install dependencies using uv (recommended)
uv sync

# Alternative: Create virtual environment manually
python -m venv .venv
source .venv/bin/activate  # or `.venv/Scripts/activate` on Windows
pip install -e .
```

### Testing
```bash
# Run all tests
uv run pytest

# Run specific test file
uv run pytest tests/test_routes.py

# Run with verbose output
uv run pytest -v

# Run with coverage
uv run pytest --cov=src/rmq_middleware
```

### Docker Operations
```bash
# Start all services (RabbitMQ + middleware)
docker-compose up -d

# View logs
docker-compose logs -f middleware
docker-compose logs -f rabbitmq

# Stop services
docker-compose stop

# Rebuild and restart
docker-compose up -d --build

# Check service status
docker-compose ps
```

### RabbitMQ Topology Setup
```bash
# Initialize EDI exchanges, queues, and bindings
uv run python scripts/init_topology.py

# Note: Requires RABBITMQ_URL environment variable or uses default
```

### Local Development
```bash
# Run the application directly (without Docker)
uv run uvicorn rmq_middleware.main:app --reload --host 0.0.0.0 --port 8000

# Check health
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Code Architecture

### Core Components

**AMQPClient (src/rmq_middleware/amqp_wrapper.py)**
- Singleton pattern for single connection management across the app
- Maintains two channels: publisher channel (with confirms) and consumer channel
- Implements connection state machine: DISCONNECTED → CONNECTING → CONNECTED → DISCONNECTING
- Automatic reconnection on connection loss via background task
- Tracks pending messages in `_pending_messages` dict for manual acknowledgment
- **Critical**: All operations check `is_ready` (connection AND channel available) before proceeding

**Routes (src/rmq_middleware/routes.py)**
- `/v1/publish` - Returns 202 only after broker confirms message persistence
- `/v1/fetch` - Long-polling message retrieval with configurable timeout (0-300s)
- `/v1/ack/{delivery_tag}` - Manual acknowledgment (required when auto_ack=False)
- `/v1/reject/{delivery_tag}` - Message rejection with optional requeue
- Returns 503 Service Unavailable when RabbitMQ is down (no buffering)

**Application Lifecycle (src/rmq_middleware/main.py)**
- Uses FastAPI `lifespan` context manager for startup/shutdown
- Signal handlers for graceful shutdown (SIGTERM, SIGINT)
- Creates singleton AMQPClient instance during startup
- 500ms grace period before closing connections on shutdown

**Configuration (src/rmq_middleware/config.py)**
- Pydantic Settings v2 with AmqpDsn validation
- Loads from .env file automatically
- Sensitive values (API keys, passwords) use SecretStr or are masked in logs
- Key settings: retry_attempts, publish_timeout, consume_timeout, log_level

**Security (src/rmq_middleware/security.py)**
- Optional API key authentication via X-API-Key header (disabled by default)
- Input validation: AMQP names must match `^[a-zA-Z0-9._-]+$`, rejects "../" and "amq." prefix
- Rate limiting: Simple in-memory sliding window (not suitable for multi-instance deployment)
- Request size limits: Default 1MB, max 100MB
- Security headers middleware adds standard HTTP security headers

**Middleware (src/rmq_middleware/middleware.py)**
- RequestIDMiddleware: Extracts/generates X-Request-ID, stores in ContextVar
- Structured logging: JSON format (production) or text format (development)
- Correlation IDs propagate from HTTP request → AMQP message → logs

### Message Flow

**Publishing:**
1. HTTP POST → `/v1/publish` with JSON body
2. Validate API key (if enabled), rate limit, request size
3. Validate exchange/routing_key format
4. Get AMQPClient singleton instance
5. Serialize payload to bytes (JSON dict → UTF-8 bytes)
6. Publish with mandatory=True, persistent=True (default)
7. Wait for publisher confirm (timeout: 30s default)
8. Return 202 Accepted or 503/504 on failure

**Consuming:**
1. HTTP POST → `/v1/fetch` with queue name and timeout
2. Call `AMQPClient.consume_one(queue_name, timeout, auto_ack=False)`
3. Use `queue.get(no_ack=False)` with asyncio.wait_for
4. Store message in `_pending_messages[delivery_tag]` for later ack
5. Return 200 with message body or 204 No Content if timeout
6. Client must POST to `/v1/ack/{delivery_tag}` to confirm processing

### Connection Management

**Reconnection Behavior:**
- aio-pika's `connect_robust()` handles automatic reconnection
- Additional custom reconnection triggered by `_on_connection_close` callback
- Exponential backoff: delay = retry_base_delay * (2^attempt)
- Default: 5 attempts starting at 1s → 1s, 2s, 4s, 8s, 16s (31s total)
- During reconnection: All API calls return 503, `/ready` returns 503

**State Checks:**
- `is_connected`: Connection exists and not closed
- `is_ready`: Connection AND both channels exist and not closed (use this for operations)

## Important Patterns

### Error Handling
- AMQPConnectionError: Not connected (503)
- AMQPPublishError: Publish failed or timeout (503/504)
- AMQPConsumeError: Consumption failed (503)
- Never buffer messages in memory - fail fast with 503

### Testing Patterns
- Use `AMQPClient.reset_instance()` in test teardown to clear singleton
- Mock AMQPClient in tests by patching `AMQPClient.get_instance()`
- Tests in tests/test_routes.py use httpx.AsyncClient with FastAPI app

### Configuration Precedence
1. Environment variables
2. .env file
3. Pydantic Field defaults

### Logging Context
- Always use `logger` from loguru, not standard logging
- Request-scoped context: `logger.contextualize(request_id=request_id)`
- Use structured fields: `logger.info("msg", field1=val1, field2=val2)`

## Common Gotchas

- **Singleton lifecycle**: AMQPClient persists for application lifetime. Don't create multiple instances.
- **Delivery tags are per-channel**: Pending messages stored with consumer_channel's delivery tags.
- **Publisher confirms**: Messages not guaranteed until confirm received. Don't return success before confirmation.
- **No message buffering**: If RabbitMQ is down, return 503 immediately. Client handles retries.
- **Manual ACK required**: Default auto_ack=False for fetch. Must explicitly acknowledge via /v1/ack.
- **Security disabled by default**: API_KEY_ENABLED and RATE_LIMIT_ENABLED are false in .env.example.
- **Docker networking**: Use service name 'rabbitmq' not 'localhost' in docker-compose RABBITMQ_URL.

## EDI Topology

The init_topology.py script creates a specific message routing structure:

**Exchanges:**
- edi.internal.topic (main router)
- edi.deadletter.topic (DLX)
- edi.retry.topic (delayed retry)

**Routing Key Format:**
`{source}.{target}.{domain}.{type}.{action}.{version}`

Example: `erp_central.wh_lviv.logistics.despatch_advice.created.v1`

**Queue Naming:**
- Inbox queues: `q.{system}.inbox` (e.g., q.erp_central.inbox)
- Dead letter: `q.deadletter`
- Retry queues: `q.retry.5s`, `q.retry.30s`, `q.retry.5m`

**Broadcast Support:**
- `*.all.#` routing key broadcasts to all systems
- `*.all.finance.#` broadcasts to finance systems only
- Domain-specific subscriptions via routing key patterns

## Language Notes

README and documentation are in Ukrainian. Code, comments, and logs are in English. When adding features, maintain this separation.
