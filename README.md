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
```

## API Ендпоінти

| Ендпоінт | Метод | Опис |
|----------|--------|-------------|
| `/health` | GET | Liveness probe (чи працює сервіс) |
| `/ready` | GET | Readiness probe (чи є з'єднання з RabbitMQ) |
| `/v1/publish/{exchange}/{routing_key}` | POST | Публікація повідомлення |
| `/v1/fetch/{queue}?timeout=30` | GET | Отримання повідомлення (long-polling) |
| `/v1/ack/{delivery_tag}` | POST | Підтвердження успішної обробки (ACK) |
| `/v1/reject/{delivery_tag}` | POST | Відхилення повідомлення (Reject/DLX) |

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

## Розробка

```bash
# Встановлення залежностей
uv sync

# Локальний запуск
uv run uvicorn rmq_middleware.main:app --reload

# API документація
відкрити http://localhost:8000/docs
```

## Додаткова документація

- [Operator's Guide](docs/operators_guide.md) - Розгортання та адміністрування.
- [OpenAPI Docs](http://localhost:8000/docs) - Інтерактивна специфікація API.

## Ліцензія

MIT

