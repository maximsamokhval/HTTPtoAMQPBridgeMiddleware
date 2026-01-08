# RMQ Middleware

Production-ready HTTP-to-AMQP bridge for ERP integration (1C:Enterprise).

## Features

- **Reliable Message Delivery**: Publisher confirms ensure message persistence
- **Long-Polling Consume**: Fetch messages with timeout
- **Full Message Lifecycle**: Acknowledge or reject with DLX support
- **Deep Observability**: Request-ID tracing through logs and AMQP headers
- **Resilient Connections**: Exponential backoff reconnection
- **Production-Ready**: Docker multi-stage build, non-root user, health probes

## Quick Start

```bash
# Start with Docker Compose
docker-compose up -d

# Check health
curl http://localhost:8000/health
curl http://localhost:8000/ready

# Publish a message
curl -X POST http://localhost:8000/v1/publish/my-exchange/my-key \
  -H "Content-Type: application/json" \
  -d '{"payload": {"message": "Hello, World!"}}'
```

## Configuration

Copy `.env.example` to `.env` and configure:

```bash
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
LOG_LEVEL=INFO
LOG_FORMAT=json
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Liveness probe |
| `/ready` | GET | Readiness probe (checks RabbitMQ) |
| `/v1/publish/{exchange}/{routing_key}` | POST | Publish message |
| `/v1/fetch/{queue}` | GET | Fetch single message |
| `/v1/ack/{delivery_tag}` | POST | Acknowledge message |
| `/v1/reject/{delivery_tag}` | POST | Reject message |

## Development

```bash
# Install dependencies
uv sync

# Run locally
uv run uvicorn rmq_middleware.main:app --reload

# API docs
open http://localhost:8000/docs
```

## Documentation

See [Operator's Guide](docs/operators_guide.md) for deployment and operational details.

## License

MIT
