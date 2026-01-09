# HTTP-to-AMQP Bridge Middleware

Надійне, готове до продакшену middleware для інтеграції RabbitMQ (AMQP) з HTTP сервісами (наприклад, 1C:Enterprise).

## Огляд

Це middleware діє як надійний міст між HTTP клієнтами та RabbitMQ, забезпечуючи:
- **Публікацію (Publishing)**: HTTP POST -> RabbitMQ Exchange (Надійно)
- **Споживання (Consuming)**: HTTP POST (Long-polling) <- RabbitMQ Queue

## Можливості
- **Бриджинг протоколів**: Конвертує HTTP REST запити в AMQP повідомлення.
- **Надійність**: Використовує RabbitMQ Publisher Confirms та ручні підтвердження (acknowledgments) для гарантії доставки "At-Least-Once".
- **Управління топологією**: Автоматично налаштовує Exchanges, Queues та Dead Letter Exchanges (DLX).
- **Безпека**:
  - Аутентифікація через API Key (`X-API-Key`)
  - Обмеження частоти запитів (Rate Limiting)
  - Валідація та санітизація вхідних даних (відповідно до OWASP)
- **Спостережуваність (Observability)**: Структуроване JSON логування з Correlation IDs.
- **Валідація схеми**: Строгі Pydantic V2 моделі для надійної обробки даних.

## Встановлення та локальна розробка

### Передумови
- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (рекомендовано) або pip
- Docker та Docker Compose

### Налаштування
1. Клонуйте репозиторій:
   ```bash
   git clone <repo-url>
   cd rmq_middleware
   ```

2. Встановіть залежності:
   ```bash
   uv sync
   ```

3. Запустіть юніт-тести:
   ```bash
   uv run pytest
   ```

## Розгортання (Deployment)

### Docker Compose (Рекомендовано)

1. Створіть файл `.env` (див. приклад у `.env.example`).
2. Запустіть сервіси:
   ```bash
   docker-compose up -d
   ```
3. Перевірте статус (Health check):
   ```bash
   curl http://localhost:8000/health
   ```

## Архітектура

### Діаграма класів (Class Diagram)

```mermaid
classDiagram
    class AMQPClient {
        +get_instance() AMQPClient
        +connect()
        +publish(exchange, routing_key, payload)
        +consume_one(queue, timeout)
        +acknowledge(delivery_tag)
    }
    
    class PublishRequest {
        +exchange: str
        +routing_key: str
        +payload: Dict
        +mandatory: bool
        +persistence: int
    }
    
    class FetchRequest {
        +queue: str
        +timeout: int
        +auto_ack: bool
    }

    class Middleware {
        +add_security_headers()
        +add_request_id()
    }

    AMQPClient ..> PublishRequest : uses
    AMQPClient ..> FetchRequest : uses
```

### Діаграма послідовності: Публікація (Sequence Diagram)

```mermaid
sequenceDiagram
    participant Client
    participant API as Middleware API
    participant AMQP as RabbitMQ
    
    Client->>API: POST /v1/publish
    API->>API: Валідація запиту та Auth
    API->>AMQP: Basic.Publish (Confirm Mode)
    alt Успіх
        AMQP-->>API: Ack
        API-->>Client: 202 Accepted
    else Помилка/Тайм-аут
        AMQP-->>API: Nack / No Response
        API-->>Client: 503 Service Unavailable
    end
```

## Приклади використання (Python)

### 1. Публікація повідомлення

Використовуйте строгі типи для прапорів `persistent` та `mandatory`.

```python
import requests

url = "http://localhost:8000/v1/publish"
headers = {"X-API-Key": "your-secret-key"}

payload = {
    "exchange": "enterprise.core",
    "routing_key": "order.created",
    "payload": {
        "order_id": 12345,
        "amount": 99.99
    },
    # Нові можливості Phase 2:
    "persistent": True,   # Delivery Mode 2 (збереження на диску)
    "mandatory": True     # Повернення помилки, якщо маршрут не знайдено
}

response = requests.post(url, json=payload, headers=headers)
print(response.status_code) # 202
print(response.json())
```

### 2. Отримання повідомлення (Long-Polling)

Використання нового ендпоінту `POST /v1/fetch` дозволяє задавати кастомні тайм-аути.

```python
import requests

url = "http://localhost:8000/v1/fetch"
headers = {"X-API-Key": "your-secret-key"}

# Запит повідомлення з 'orders.queue' з очікуванням 10 секунд
payload = {
    "queue": "orders.queue",
    "timeout": 10,
    "auto_ack": False 
}

response = requests.post(url, json=payload, headers=headers)

if response.status_code == 200:
    msg = response.json()
    print("Отримано:", msg["body"])
    
    # Обробка повідомлення...
    
    # Підтвердження обробки (Acknowledgment)
    ack_url = f"http://localhost:8000/v1/ack/{msg['delivery_tag']}"
    requests.post(ack_url, headers=headers)
    
elif response.status_code == 204:
    print("Черга порожня")
```

## Довідник API (API Reference)

| Ендпоінт | Метод | Опис |
|----------|-------|------|
| `/v1/publish` | POST | Публікація повідомлення (Строга схема) |
| `/v1/fetch` | POST | Отримання повідомлення (Long-polling) |
| `/v1/ack/{tag}` | POST | Підтвердження повідомлення (Ack) |
| `/health` | GET | Перевірка працездатності (Liveness) |
| `/ready` | GET | Перевірка готовності (Readiness) |

## Налаштування (Configuration)

Дивіться `.env.example` для детальної конфігурації. Основні змінні:
- `RABBITMQ_URL`: Рядок підключення AMQP.
- `API_KEY`: Секретний ключ для аутентифікації.
- `RABBITMQ_PREFETCH_COUNT`: Налаштування QoS (кількість повідомлень).

## Вирішення проблем (Troubleshooting)

Для детальних інструкцій з експлуатації, моніторингу та обробки збоїв, дивіться [Керівництво Оператора](docs/operators_guide.md).
