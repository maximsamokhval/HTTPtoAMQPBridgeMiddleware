# RMQ Middleware - Operator's Guide

## Overview

The RMQ Middleware is an HTTP-to-AMQP bridge designed for reliable message delivery between HTTP clients (such as 1C:Enterprise) and RabbitMQ. This guide explains operational aspects including deployment, configuration, and failure handling.

---

## Deployment Prerequisites

### System Requirements
- Docker 24.0+ with Docker Compose v2
- Network access to RabbitMQ (port 5672)
- Minimum 256MB RAM (512MB recommended)

### Quick Start

```bash
# Clone and start
cd rmq_middleware
docker-compose up -d

# Check status
docker-compose ps
docker-compose logs -f middleware
```

---

## Configuration Reference

All configuration is done via environment variables. See `.env.example` for a complete template.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `RABBITMQ_URL` | Yes | - | Full AMQP connection string |
| `RABBITMQ_PREFETCH_COUNT` | No | 10 | Max unacked messages per consumer |
| `RETRY_ATTEMPTS` | No | 5 | Connection retry attempts |
| `RETRY_BASE_DELAY` | No | 1.0 | Base delay for exponential backoff (seconds) |
| `PUBLISH_TIMEOUT` | No | 30.0 | Publisher confirm timeout (seconds) |
| `CONSUME_TIMEOUT` | No | 30.0 | Long-polling timeout (seconds) |
| `LOG_LEVEL` | No | INFO | Logging level (DEBUG/INFO/WARNING/ERROR) |
| `LOG_FORMAT` | No | json | Log format (json/text) |

### Security Notes
- `RABBITMQ_URL` passwords are automatically masked in logs
- Run the container as non-root (UID 1000)
- No build tools in the production image

---

## Health Monitoring

### Endpoints

| Endpoint | Purpose | Success | Failure |
|----------|---------|---------|---------|
| `GET /health` | Liveness probe | 200 OK | Application crashed |
| `GET /ready` | Readiness probe | 200 OK | 503 Service Unavailable |

### Kubernetes/Docker Health Checks

```yaml
# Kubernetes probe configuration
livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 5
```

### Monitoring Metrics

The `/ready` endpoint returns detailed status:

```json
{
  "status": "ready",
  "service": "rmq-middleware",
  "amqp_connected": true,
  "amqp_ready": true,
  "amqp_state": "connected",
  "pending_messages": 0
}
```

---

## RabbitMQ Restart Handling

### Automatic Reconnection Behavior

When RabbitMQ becomes unavailable (restart, network partition, etc.), the middleware handles it as follows:

#### 1. During RabbitMQ Downtime
- New publish requests immediately receive **HTTP 503 Service Unavailable**
- No messages are buffered in memory (prevents OOM)
- Fetch/consume requests return 503
- The `/ready` endpoint returns 503

#### 2. Reconnection Process
- The middleware detects connection loss via aio-pika callbacks
- A background task initiates reconnection with exponential backoff:
  - Attempt 1: Wait 1 second
  - Attempt 2: Wait 2 seconds
  - Attempt 3: Wait 4 seconds
  - Attempt 4: Wait 8 seconds
  - Attempt 5: Wait 16 seconds
- Total reconnection window: ~31 seconds (with default settings)

#### 3. After RabbitMQ Recovery
- Connection is automatically re-established
- Channels are recreated with configured prefetch
- The `/ready` endpoint returns 200
- Normal message processing resumes

### Client Retry Strategy

Since the middleware returns 503 during outages, clients (1C) should implement retry logic:

```
Recommended Client Behavior:
1. On 503 response, wait 1-5 seconds
2. Retry the request
3. Use exponential backoff for repeated failures
4. Log the failure for investigation
```

### Message Safety Guarantees

| Scenario | Behavior |
|----------|----------|
| RabbitMQ down before publish | 503 returned, message not lost (client has it) |
| RabbitMQ down during publish | Timeout triggers 504, client should retry |
| RabbitMQ down after ack | Message confirmed, safe |
| Message consumed but not acked, RabbitMQ restarts | Message redelivered by RabbitMQ |

---

## Graceful Shutdown

When the middleware receives SIGTERM or SIGINT:

1. **Stop accepting new requests** (FastAPI shutdown)
2. **Wait for in-flight requests** (500ms default)
3. **Close AMQP channels** (pending operations complete)
4. **Close AMQP connection** (clean disconnect)
5. **Log shutdown complete**

### Docker Stop Behavior

```bash
# Graceful stop (sends SIGTERM, waits 10s)
docker-compose stop middleware

# Force kill (not recommended)
docker-compose kill middleware
```

### Kubernetes Termination

Configure `terminationGracePeriodSeconds` in your deployment:

```yaml
spec:
  terminationGracePeriodSeconds: 30
```

---

## Troubleshooting

### Common Issues

#### 1. "Connection refused" on startup
**Cause**: RabbitMQ not ready when middleware starts

**Solution**: Use Docker Compose health-based dependency:
```yaml
depends_on:
  rabbitmq:
    condition: service_healthy
```

#### 2. Constant 503 errors
**Cause**: RabbitMQ connection failing repeatedly

**Diagnosis**:
```bash
# Check middleware logs
docker-compose logs middleware | grep -i "connect"

# Check RabbitMQ status
docker-compose exec rabbitmq rabbitmq-diagnostics status
```

**Solutions**:
- Verify `RABBITMQ_URL` is correct
- Check network connectivity between containers
- Increase `RETRY_ATTEMPTS` for unstable networks

#### 3. Messages not persisting after RabbitMQ restart
**Cause**: Messages published with `persistent=false`

**Solution**: Always use `persistent=true` (default) for critical messages

#### 4. High memory usage
**Cause**: Many pending (unacknowledged) messages

**Diagnosis**: Check `/ready` endpoint for `pending_messages` count

**Solutions**:
- Ensure clients call `/v1/ack/{delivery_tag}` after processing
- Reduce `RABBITMQ_PREFETCH_COUNT`
- Implement client-side timeout for ack operations

---

## API Quick Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness probe |
| `/ready` | GET | Readiness probe |
| `/v1/publish/{exchange}/{routing_key}` | POST | Publish message |
| `/v1/fetch/{queue}` | GET | Consume single message |
| `/v1/ack/{delivery_tag}` | POST | Acknowledge message |
| `/v1/reject/{delivery_tag}` | POST | Reject message |

### Example: Publish Message

```bash
curl -X POST http://localhost:8000/v1/publish/orders/new \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: abc-123" \
  -d '{"payload": {"order_id": 12345, "customer": "ACME"}}'
```

### Example: Fetch and Acknowledge

```bash
# Fetch
RESPONSE=$(curl -s http://localhost:8000/v1/fetch/orders.queue)
DELIVERY_TAG=$(echo $RESPONSE | jq -r '.delivery_tag')

# Process message...

# Acknowledge
curl -X POST http://localhost:8000/v1/ack/$DELIVERY_TAG
```

---

## Log Analysis

### JSON Log Format (Production)

```json
{
  "timestamp": "2024-01-15T10:30:45.123456+00:00",
  "level": "INFO",
  "message": "Message published",
  "request_id": "abc-123-def-456",
  "exchange": "orders",
  "routing_key": "new",
  "correlation_id": "abc-123-def-456"
}
```

### Key Log Events

| Message | Level | Meaning |
|---------|-------|---------|
| "Starting RMQ Middleware" | INFO | Application starting |
| "Connected to RabbitMQ" | INFO | Connection established |
| "Connection closed unexpectedly" | WARNING | Connection lost |
| "Retrying connection" | WARNING | Reconnection attempt |
| "Message published" | INFO | Successful publish |
| "Publish failed" | ERROR | Publish operation failed |
| "Shutdown complete" | INFO | Graceful shutdown finished |

---

## Version Information

- Application Version: 1.0.0
- Python: 3.11+
- FastAPI: 0.109+
- aio-pika: 9.4+
