# HTTP-to-AMQP Bridge Middleware

[![CI](https://github.com/maximsamokhval/HTTPtoAMQPBridgeMiddleware/actions/workflows/ci.yml/badge.svg)](https://github.com/maximsamokhval/HTTPtoAMQPBridgeMiddleware/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
[![codecov](https://codecov.io/github/maximsamokhval/HTTPtoAMQPBridgeMiddleware/branch/main/graph/badge.svg?token=MX7YSVVM6W)](https://codecov.io/github/maximsamokhval/HTTPtoAMQPBridgeMiddleware)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)

A robust, production-ready middleware for integrating RabbitMQ (AMQP) with HTTP services (e.g., 1C:Enterprise, ERPs, legacy systems).

[Українська версія (Ukrainian Version)](README.ua.md)

## Overview

This middleware acts as a reliable bridge between HTTP clients and RabbitMQ, ensuring:
- **Publishing**: HTTP POST -> RabbitMQ Exchange (Reliable, At-Least-Once)
- **Consuming**: HTTP POST (Long-polling) <- RabbitMQ Queue

## Key Features

- **Protocol Bridging**: Converts HTTP REST requests into AMQP messages.
- **Reliability**: Uses RabbitMQ Publisher Confirms and manual Acknowledgments to guarantee "At-Least-Once" delivery.
- **Fault Tolerance**: Circuit Breaker pattern protects against cascade failures when RabbitMQ is unavailable.
- **Topology Management**: Automatically sets up Exchanges, Queues, and Dead Letter Exchanges (DLX).
- **Security**:
  - **HTTP Basic Authentication**: Proxies credentials directly to RabbitMQ.
  - **Connection Pooling**: Manages efficient user sessions with auto-cleanup.
  - **Rate Limiting** & **Input Validation** (OWASP standards).
- **Observability**: Structured JSON logging with Correlation IDs and Prometheus metrics.
- **Strict Schema**: Pydantic V2 models for reliable data handling.

## Circuit Breaker (Fault Tolerance)

The middleware implements the Circuit Breaker pattern to protect against cascade failures:

| State | Behavior |
|-------|----------|
| **CLOSED** | Normal operation, requests pass through |
| **OPEN** | Requests fail immediately with HTTP 503, protecting the broker |
| **HALF_OPEN** | Testing if service has recovered |

**Configuration** (via environment variables):
- `CB_FAILURE_THRESHOLD`: Failures before opening (default: 5)
- `CB_FAILURE_WINDOW_SECONDS`: Sliding window for counting (default: 10)
- `CB_RECOVERY_TIMEOUT`: Time before half-open test (default: 30)
- `CB_HALF_OPEN_REQUESTS`: Test requests in half-open (default: 1)

The `/ready` endpoint includes `circuit_breaker_state` field.

## ⚠️ Security Notice (Production Deployment)

This service uses **HTTP Basic Authentication**.

> **CRITICAL**: Do NOT expose this service directly to the internet. You **MUST** deploy it behind a Reverse Proxy (Nginx, Traefik, AWS ALB) configured with **HTTPS (TLS)**.
> Sending credentials (username/password) over plain HTTP is insecure.

## Installation & Local Development

### Prerequisites
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip
- Docker & Docker Compose

### Setup
1. Clone the repository:
   ```bash
   git clone <repo-url>
   cd rmq_middleware
   ```

2. Install dependencies:
   ```bash
   uv sync
   ```

3. Run tests (including integration tests with Testcontainers):
   ```bash
   uv run pytest
   ```

## Deployment

### Docker Compose (Recommended)

1. Create a `.env` file (see `.env.example`).
2. Start services:
   ```bash
   docker-compose up -d
   ```
3. Check health status:
   ```bash
   curl http://localhost:8000/health
   ```

## Message Lifecycle & Behavior

### Payload Serialization
The middleware attempts to handle message bodies intelligently:
1.  **JSON**: If the message content-type is `application/json` or the body is valid JSON, it is deserialized into a Dictionary/List.
2.  **String**: If JSON parsing fails, it attempts to decode as a UTF-8 string.
3.  **Hex**: If UTF-8 decoding fails (binary data), the body is returned as a Hexadecimal string.

### Unacked Messages & Timeouts

1.  **Fetch Timeout**: The `timeout` parameter in `/v1/fetch` defines how long to wait for a message to *arrive*. It is NOT the processing time.
2.  **Unacked State**: When a message is fetched with `auto_ack: false`, it enters the `Unacked` state in RabbitMQ and is locked to the current user session.
3.  **Session Cleanup**: If a client crashes without sending an Ack/Reject, the middleware will automatically close the idle connection after **5 minutes**. Only then will RabbitMQ return the message to the queue for other consumers.

**Client Recommendations:**
*   **Success**: Always send `POST /v1/ack/{tag}`.
*   **Failure**: Send `POST /v1/reject/{tag}` (use `requeue: true` to retry, or `false` for DLX).

## API Reference

| Endpoint | Method | Description | Auth |
|----------|-------|------|------|
| `/v1/publish` | POST | Publish message (Strict Schema) | Basic |
| `/v1/fetch` | POST | Consume message (Long-polling) | Basic |
| `/v1/ack/{tag}` | POST | Acknowledge message | Basic |
| `/v1/reject/{tag}` | POST | Reject message | Basic |
| `/health` | GET | Liveness probe | None |
| `/ready` | GET | Readiness probe | None |

## Python Usage Example

### Publishing

```python
import requests
from requests.auth import HTTPBasicAuth

url = "http://localhost:8000/v1/publish"
auth = HTTPBasicAuth('my_rmq_user', 'my_secret_pass')

payload = {
    "exchange": "enterprise.core",
    "routing_key": "order.created",
    "payload": {"id": 123},
    "persistent": True,
    "mandatory": True
}

resp = requests.post(url, json=payload, auth=auth)
print(resp.status_code) # 202
```

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
