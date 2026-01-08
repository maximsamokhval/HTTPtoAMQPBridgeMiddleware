# RMQ Middleware

Високопродуктивний HTTP-to-AMQP bridge для інтеграції з ERP-системами (1C:Enterprise).

## Особливості

- **Надійна доставка повідомлень**: Publisher confirms гарантують збереження повідомлень у брокері.
- **Long-Polling Consume**: Отримання повідомлень з черги з підтримкою таймаутів.
- **Повний життєвий цикл повідомлень**: Підтримка Acknowledge та Reject з використанням Dead Letter Exchange (DLX).
- **Наскрізна обсервабільність**: Трасування за допомогою Request-ID через логи та AMQP заголовки.
- **Стійкість з'єднання**: Автоматичне перепідключення з експоненціальною затримкою (backoff).
- **Готовність до Production**: Multi-stage Docker build, запуск від non-root користувача, health probes.

## Швидкий старт

```bash
# Запуск за допомогою Docker Compose
docker-compose up rabbitmq -d
# Зачекайте 30 секунд до готовності RabbitMQ, потім:
docker exec rmq-middleware-rabbitmq rabbitmqctl add_vhost edi
docker exec rmq-middleware-rabbitmq rabbitmqctl set_permissions -p edi guest ".*" ".*" ".*"
docker-compose up middleware -d

# Ініціалізація топології EDI
uv run python scripts/init_topology.py

# Перевірка стану
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Конфігурація

Скопіюйте `.env.example` у `.env` та налаштуйте параметри:

```bash
RABBITMQ_URL=amqp://guest:guest@localhost:5672/edi
LOG_LEVEL=INFO
LOG_FORMAT=json

# Налаштування безпеки (Security)
API_KEY_ENABLED=true
API_KEY=ваш-секретний-ключ
RATE_LIMIT_ENABLED=true
```

## Безпека (Security)

Проект відповідає стандартам **OWASP Top 10** та включає наступні механізми захисту:

### 1. Авторизація (API Key)
Всі запити до `/v1/*` повинні містити заголовок `X-API-Key`.
- **Налаштування**: `API_KEY_ENABLED=true`.
- **Заголовок**: `X-API-Key: <your_key>`.

### 2. Обмеження запитів (Rate Limiting)
Захист від DoS-атак та brute-force.
- **Налаштування**: `RATE_LIMIT_ENABLED=true`.
- **Ліміт**: 100 запитів на 60 секунд (налаштовується).

### 3. Валідація вводу (Input Validation)
Сувора перевірка назв черг та exchange для запобігання ін'єкціям.
- **Дозволені символи**: `a-z`, `A-Z`, `0-9`, `.`, `-`, `_`.
- **Заборонено**: `..`, `//`, префікс `amq.`.
- **Макс. довжина**: 255 символів.

### 4. Security Headers
Middleware автоматично додає захисні заголовки:
- `X-Frame-Options: DENY`
- `Content-Security-Policy: default-src 'none'`
- `Strict-Transport-Security` (HSTS)
- `X-Content-Type-Options: nosniff`

## API Ендпоінти

> [!IMPORTANT]
> Для всіх ендпоінтів `/v1/` обов'язкова передача `X-API-Key` (якщо активовано в налаштуваннях).

| Ендпоінт | Метод | Опис |
|----------|--------|-------------|
| `/health` | GET | Liveness probe (без AUTH) |
| `/ready` | GET | Readiness probe (без AUTH) |
| `/v1/publish/{ex}/{key}` | POST | Публікація повідомлення |
| `/v1/fetch/{q}?timeout=30` | GET | Отримання повідомлення |
| `/v1/ack/{delivery_tag}` | POST | Підтвердження (ACK) |
| `/v1/reject/{tag}` | POST | Відхилення (Reject/DLX) |

---

## Посібник з отримання повідомлень

### Огляд процесу

Повідомлення отримуються за патерном **long-polling** з **ручним підтвердженням**:

1. **Fetch**: Отримання повідомлення з черги.
2. **Process**: Обробка даних у вашій системі.
3. **Acknowledge** (успіх) або **Reject** (помилка).

> ⚠️ **Важливо**: Повідомлення ПОВИННІ бути або підтверджені (ack), або відхилені (reject). Непідтверджені повідомлення залишаються у статусі "in-flight" і будуть повторно доставлені при розриві з'єднання.

### Крок 1: Отримання повідомлення (Fetch)

```http
GET /v1/fetch/{queue}?timeout=30
```

**Параметри:**
| Параметр | Тип | За замовчуванням | Опис |
|-----------|------|---------|-------------|
| `queue` | path | обов'язковий | Назва черги (напр., `q.erp_central.inbox`) |
| `timeout` | query | 30.0 | Таймаут очікування в секундах (1-300) |

**Відповідь (200 OK):**
```json
{
  "delivery_tag": 1,
  "body": {"order_id": 12345, "customer": "ACME"},
  "routing_key": "erp_central.wh_lviv.logistics.order.created.v1",
  "exchange": "edi.internal.topic",
  "correlation_id": "abc-123-def-456",
  "headers": {"x-retry-count": 0},
  "redelivered": false
}
```

**Відповідь (204 No Content):**
Повідомлень немає в черзі протягом вказаного таймауту.

**Приклад (curl):**
```bash
curl -s http://localhost:8000/v1/fetch/q.erp_central.inbox?timeout=10
```

**Приклад (1C:Enterprise):**
```bsl
HTTPЗапрос = Новый HTTPЗапрос("/v1/fetch/q.erp_central.inbox?timeout=30");
HTTPОтвет = HTTPСоединение.Получить(HTTPЗапрос);

Если HTTPОтвет.КодСостояния = 200 Тогда
    Сообщение = ПрочитатьJSON(HTTPОтвет.ПолучитьТелоКакСтроку());
    DeliveryTag = Сообщение.delivery_tag;
    ТелоСообщения = Сообщение.body;
ИначеЕсли HTTPОтвет.КодСостояния = 204 Тогда
    // Черга порожня
КонецЕсли;
```

### Що буде, якщо не надіслати ACK?

Якщо ваша система отримала повідомлення через `/v1/fetch/`, але **не викликала** `/v1/ack/` або `/v1/reject/`:

1.  **На стороні RabbitMQ**: Повідомлення переходить у статус `Unacked`. Воно залишається зарезервованим за вашою сесією (каналом) і не буде доставлене іншим споживачам.
2.  **На стороні Middleware**: Об'єкт повідомлення зберігається в пам'яті в словнику `_pending_messages` ([див. amqp_wrapper.py:L452](file:///c:/dev/rmq_middleware/src/rmq_middleware/amqp_wrapper.py#L452)).
3.  **При розриві з'єднання**: Якщо Middleware перезавантажиться або з'єднання з брокером розірветься, всі непідтверджені повідомлення **автоматично повернуться в чергу** (status `Ready`) і будуть доступні для повторного отримання.

> [!WARNING]
> Велика кількість непідтверджених повідомлень призводить до "зависання" черги та споживання пам'яті Middleware. Завжди завершуйте цикл обробки викликом ACK або Reject.

**Логіка в коді:**
*   [Вичитування з `no_ack=False`](file:///c:/dev/rmq_middleware/src/rmq_middleware/amqp_wrapper.py#L441) — змушує брокер чекати на підтвердження.
*   [Збереження в `_pending_messages`](file:///c:/dev/rmq_middleware/src/rmq_middleware/amqp_wrapper.py#L452) — дозволяє Middleware знайти повідомлення за його `delivery_tag` для наступного підтвердження.

---

### Крок 2: Обробка повідомлення

Виконайте необхідні дії у вашій бізнес-логіці. `delivery_tag` знадобиться для наступного кроку.

### Крок 3a: Підтвердження успіху (Acknowledge)

Після успішної обробки підтвердіть повідомлення:

```http
POST /v1/ack/{delivery_tag}
```

**Відповідь (200 OK):**
```json
{
  "status": "acknowledged",
  "delivery_tag": 1
}
```

**Приклад (1C:Enterprise):**
```bsl
HTTPЗапрос = Новый HTTPЗапрос("/v1/ack/" + Формат(DeliveryTag, "ЧГ=0"));
HTTPЗапрос.Заголовки.Вставить("Content-Type", "application/json");
HTTPОтвет = HTTPСоединение.ОтправитьДляОбработки(HTTPЗапрос);

Если HTTPОтвет.КодСостояния = 200 Тогда
    // Повідомлення успішно видалено з черги брокера
КонецЕсли;
```

### Крок 3b: Відхилення (Reject)

Якщо обробка неможлива, відхиліть повідомлення:

```http
POST /v1/reject/{delivery_tag}
Content-Type: application/json

{"requeue": false}
```

**Параметри:**
| Параметр | Тип | За замовчуванням | Опис |
|-----------|------|---------|-------------|
| `requeue` | body | false | `true` = повернути в чергу, `false` = відправити в DLX |

**Відповідь (200 OK):**
```json
{
  "status": "rejected",
  "delivery_tag": 1
}
```

### Повний цикл (Bash приклад)

```bash
# 1. Отримання
RESPONSE=$(curl -s http://localhost:8000/v1/fetch/q.erp_central.inbox?timeout=10)

# 2. Парсинг тега (через jq)
DELIVERY_TAG=$(echo $RESPONSE | jq -r '.delivery_tag')

# 3. Підтвердження
curl -X POST http://localhost:8000/v1/ack/$DELIVERY_TAG
```

### Обробка помилок

| Код стану | Значення | Дія |
|-------------|---------|--------|
| `200` | Success | Повідомлення оброблено/підтверджено |
| `204` | No message | Черга порожня, повторіть пізніше |
| `400` | Invalid delivery_tag | Тег застарів або вже був оброблений |
| `503` | RabbitMQ unavailable | Помилка з'єднання, повторіть з затримкою |

### Кращі практики

1. **Завжди ACK/REJECT**: Непідтверджені повідомлення блокують пам'ять брокера.
2. **Correlation ID**: Використовуйте для трасування ланцюжка HTTP -> AMQP.
3. **Redelivered flag**: Якщо `true`, повідомлення вже намагалися доставити раніше.
4. **Таймаути**: Обирайте таймаут згідно з навантаженням (зазвичай 30-60с).
5. **Retry logic**: При кодах 503 реалізуйте повторні спроби на стороні клієнта.

---

## Публікація повідомлень

```bash
curl -X POST http://localhost:8000/v1/publish/edi.internal.topic/erp_central.wh_lviv.logistics.order.created.v1 \
  -H "Content-Type: application/json" \
  -H "X-Request-ID: correlation-id-123" \
  -d '{
    "payload": {"order_id": 12345, "customer": "ACME"},
    "headers": {"x-source-system": "erp"},
    "persistent": true
  }'
```

## Для розробників

### Архітектура системи

Сервіс побудований на базі **FastAPI** та **aio-pika** (асинхронний драйвер RabbitMQ).

```mermaid
graph TD
    Client[HTTP Client / 1C] --> MW[Security Middleware]
    MW --> Auth{Auth OK?}
    Auth -- No --> E401[401 Unauthorized]
    Auth -- Yes --> Rate{Rate Limit?}
    Rate -- Exceeded --> E429[429 Too Many Requests]
    Rate -- OK --> Route[FastAPI Router]
    Route --> AMQP[AMQP Client Singleton]
    AMQP --> RMQ[RabbitMQ Broker]
```

### Структура проекту
- `src/rmq_middleware/security.py`: Логіка аутентифікації, rate limiting та валідації.
- `src/rmq_middleware/amqp_wrapper.py`: Обгортка над RabbitMQ з механізмом повторних підключень.
- `src/rmq_middleware/middleware.py`: JSON-логування та Request-ID трасування.

### Розробка та тестування

```bash
# Встановлення залежностей (uv)
uv sync

# Запуск linter (ruff)
uv run ruff check .

# Локальний запуск для розробки
uv run uvicorn rmq_middleware.main:app --reload --port 8000
```

### Важливі примітки
- **Singleton AMQPClient**: Використовує одне спільне підключення для всього додатку.
- **Graceful Shutdown**: При зупинці сервіс чекає завершення активних запитів та коректно закриває AMQP-канали.

---

## Додаткова документація

- [Operator's Guide](docs/operators_guide.md) - Розгортання та адміністрування.
- [Walkthrough](C:\Users\samokhval\.gemini\antigravity\brain\7564beff-b027-4652-bea0-be674b60125b\walkthrough.md) - Детальний опис реалізації.

## Ліцензія

MIT

