# HTTP-to-AMQP Bridge Middleware

[![CI](https://github.com/maximsamokhval/HTTPtoAMQPBridgeMiddleware/actions/workflows/ci.yml/badge.svg)](https://github.com/maximsamokhval/HTTPtoAMQPBridgeMiddleware/actions/workflows/ci.yml)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)

Надійне, готове до продакшену middleware для інтеграції RabbitMQ (AMQP) з HTTP сервісами (наприклад, 1C:Enterprise).

[English Version](README.md)

## Огляд

Це middleware діє як надійний міст між HTTP клієнтами та RabbitMQ, забезпечуючи:
- **Публікацію (Publishing)**: HTTP POST -> RabbitMQ Exchange (Надійно)
- **Споживання (Consuming)**: HTTP POST (Long-polling) <- RabbitMQ Queue

## Можливості
- **Бриджинг протоколів**: Конвертує HTTP REST запити в AMQP повідомлення.
- **Надійність**: Використовує RabbitMQ Publisher Confirms та ручні підтвердження (acknowledgments) для гарантії доставки "At-Least-Once".
- **Управління топологією**: Автоматично налаштовує Exchanges, Queues та Dead Letter Exchanges (DLX).
- **Безпека**:
  - **HTTP Basic Authentication**: Використовує стандартний механізм аутентифікації. Middleware виступає проксі, передаючи облікові дані (username/password) безпосередньо в RabbitMQ.
  - Управління підключеннями (Connection Pooling): Автоматично створює та кешує з'єднання для кожного користувача.
  - Обмеження частоти запитів (Rate Limiting)
  - Валідація та санітизація вхідних даних (відповідно до OWASP)
- **Спостережуваність (Observability)**: Структуроване JSON логування з Correlation IDs та Prometheus метрики.
- **Валідація схеми**: Строгі Pydantic V2 моделі для надійної обробки даних.

## ⚠️ Важливе зауваження з безпеки

Цей сервіс використовує **HTTP Basic Authentication**.

> **КРИТИЧНО**: НЕ публікуйте цей сервіс в інтернет напряму. Ви **ЗОБОВ'ЯЗАНІ** розмістити його за Reverse Proxy (Nginx, Traefik, AWS ALB), налаштованим з **HTTPS (TLS)**.
> Передача облікових даних (логін/пароль) через незахищений HTTP є небезпечною.

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

## Поведінка системи та Життєвий цикл повідомлень

### Серіалізація Payload
Middleware намагається розумно обробляти тіло повідомлень:
1.  **JSON**: Якщо тіло є валідним JSON, воно десеріалізується в об'єкт/масив.
2.  **String**: Якщо це не JSON, спроба декодувати як UTF-8 рядок.
3.  **Hex**: Якщо це бінарні дані, повертається Hexadecimal рядок.

### Обробка повідомлень та Timeout

1.  **Отримання повідомлення (`Fetch`)**:
    *   Параметр `timeout` у запиті `/v1/fetch` визначає час очікування **надходження** повідомлення в чергу. Це не час на обробку.
    *   Якщо повідомлення отримано з `auto_ack: false`, воно отримує статус `Unacked` в RabbitMQ.
    *   Повідомлення залишається "заблокованим" за поточною сесією користувача і невидимим для інших споживачів.

2.  **Повернення повідомлення в чергу (Redelivery)**:
    *   Повідомлення повернеться в чергу і стане доступним іншим споживачам лише у двох випадках:
        1.  **Явна відмова**: Виклик `/v1/reject/{tag}` з `requeue: true`.
        2.  **Таймаут сесії**: Middleware автоматично закриває неактивні з'єднання через **5 хвилин** (idle timeout). В цей момент RabbitMQ детектить розрив з'єднання і повертає всі `Unacked` повідомлення в чергу.

**Рекомендації для клієнтів:**
*   **Успішна обробка**: Завжди надсилайте `/v1/ack/{tag}` після обробки.
*   **Помилка обробки**: Надсилайте `/v1/reject/{tag}`.

## Приклади використання (Python)

### 1. Публікація повідомлення

Middleware використовує **HTTP Basic Auth**. Ви повинні надати ім'я користувача та пароль, які зареєстровані в RabbitMQ. Middleware підключиться від імені цього користувача.

```python
import requests
from requests.auth import HTTPBasicAuth

url = "http://localhost:8000/v1/publish"
auth = HTTPBasicAuth('my_rmq_user', 'my_secret_pass')

payload = {
    "exchange": "enterprise.core",
    "routing_key": "order.created",
    "payload": {
        "order_id": 12345,
        "amount": 99.99
    },
    "persistent": True,
    "mandatory": True
}

response = requests.post(url, json=payload, auth=auth)
print(response.status_code) # 202
print(response.json())
```

### 2. Отримання повідомлення (Long-Polling)

```python
import requests
from requests.auth import HTTPBasicAuth

url = "http://localhost:8000/v1/fetch"
auth = HTTPBasicAuth('my_rmq_user', 'my_secret_pass')

payload = {
    "queue": "orders.queue",
    "timeout": 10,
    "auto_ack": False 
}

response = requests.post(url, json=payload, auth=auth)

if response.status_code == 200:
    msg = response.json()
    print("Отримано:", msg["body"])
    
    # Підтвердження обробки (Acknowledgment)
    ack_url = f"http://localhost:8000/v1/ack/{msg['delivery_tag']}"
    requests.post(ack_url, auth=auth)
    
elif response.status_code == 204:
    print("Черга порожня")
```

## Довідник API (API Reference)

| Ендпоінт | Метод | Опис | Auth |
|----------|-------|------|------|
| `/v1/publish` | POST | Публікація повідомлення | Basic |
| `/v1/fetch` | POST | Отримання повідомлення (Long-polling) | Basic |
| `/v1/ack/{tag}` | POST | Підтвердження повідомлення (Ack) | Basic |
| `/v1/reject/{tag}` | POST | Відхилення повідомлення (Reject) | Basic |
| `/health` | GET | Перевірка працездатності (Liveness) | None |
| `/ready` | GET | Перевірка готовності (Readiness) | None |

## Ліцензія

Цей проект ліцензовано під ліцензією MIT - дивіться файл [LICENSE](LICENSE) для деталей.