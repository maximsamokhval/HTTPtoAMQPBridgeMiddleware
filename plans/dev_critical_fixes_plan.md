# Пошаговый план работ для ветки DEV: Критические исправления

## Контекст
Ветка DEV будет содержать исключительно исправления критических ошибок, угрожающих стабильности или безопасности системы. Каждый шаг - атомарное изменение кода с последующей верификацией тестами и коммитом.

## Критические проблемы для исправления (приоритет P0)

1. **KeyError: 'request_id' в логировании** - нарушает наблюдаемость, скрывает реальные ошибки
2. **Отсутствие валидации AMQP имен** - security vulnerability, возможность инъекции
3. **Проблемы с graceful shutdown** - потенциальная потеря сообщений при остановке

## Шаг 1: Исправление KeyError в логировании

### Проблема
Функция `get_request_id()` в `middleware.py` возвращает пустую строку вне контекста запроса, что вызывает `KeyError: 'request_id'` в форматерах логов.

### Изменения кода
1. Модифицировать `get_request_id()` для гарантированного возврата непустой строки
2. Обновить `text_formatter()` и `json_sink()` для обработки отсутствующего request_id

### Файлы для изменения
- `src/rmq_middleware/middleware.py`

### Код изменения
```python
# В функции get_request_id():
def get_request_id() -> str:
    """Get the current request ID from context.
    
    Returns a non-empty string:
    - If in a request context: returns the request ID (from headers or generated UUID)
    - If not in a request context: returns a generated UUID for traceability
    """
    request_id = request_id_ctx.get()
    if not request_id:  # Empty string or None
        # Generate a temporary ID for background operations
        import uuid
        request_id = f"bg-{uuid.uuid4().hex[:8]}"
    return request_id

# В функции text_formatter():
def text_formatter(record: dict) -> str:
    """Human-readable formatter for development."""
    request_id = record["extra"].get("request_id", "")
    # Если request_id пустой, использовать значение из контекста
    if not request_id:
        request_id = get_request_id()
    request_id_str = f"[{request_id[:8]}] " if request_id else ""
    
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        f"<cyan>{request_id_str}</cyan>"
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>\n"
    )
```

### Тестирование
**Действие**: Запустить unit тесты для middleware
```bash
uv run pytest tests/ -k "middleware" -v
```

### Коммит
**Сообщение**: `fix(logging): eliminate KeyError by ensuring request_id is always available`

---

## Шаг 2: Усиленная валидация AMQP имен

### Проблема
Отсутствие строгой валидации имен exchange, queue и routing_key создает security vulnerability (инъекция через специальные символы).

### Изменения кода
1. Добавить функции валидации в `security.py`
2. Интегрировать валидацию в endpoints публикации и потребления
3. Добавить тесты для edge cases

### Файлы для изменения
- `src/rmq_middleware/security.py`
- `src/rmq_middleware/routes.py`
- `tests/test_security.py`

### Код изменения
```python
# В security.py добавить:
import re

def validate_amqp_name(name: str, max_length: int = 255) -> None:
    """Validate AMQP exchange/queue name according to AMQP 0-9-1 spec.
    
    Raises:
        InputValidationError: If name is invalid
    """
    if not name:
        raise InputValidationError("AMQP name cannot be empty")
    
    if len(name) > max_length:
        raise InputValidationError(f"AMQP name too long (max {max_length} chars)")
    
    # Basic validation: no control characters, no problematic sequences
    if re.search(r'[\x00-\x1F\x7F]', name):
        raise InputValidationError("AMQP name contains control characters")
    
    # Additional security: prevent potential injection
    if '..' in name or '//' in name:
        raise InputValidationError("AMQP name contains potentially dangerous sequences")

def validate_routing_key_strict(key: str) -> None:
    """Strict validation for routing keys."""
    if not key:
        raise InputValidationError("Routing key cannot be empty")
    
    if len(key) > 255:
        raise InputValidationError("Routing key too long (max 255 chars)")
    
    # Validate characters (simplified - actual AMQP allows more)
    if not re.match(r'^[a-zA-Z0-9\-\._#*]+$', key):
        raise InputValidationError(
            "Routing key contains invalid characters. "
            "Allowed: letters, numbers, hyphen, period, underscore, #, *"
        )
```

### Тестирование
**Действие**: Запустить security тесты
```bash
uv run pytest tests/test_security.py -v
```

### Коммит
**Сообщение**: `fix(security): add strict validation for AMQP names to prevent injection attacks`

---

## Шаг 3: Улучшение graceful shutdown

### Проблема
При получении SIGTERM приложение может потерять in-flight сообщения, так как не дает достаточно времени для завершения операций.

### Изменения кода
1. Увеличить timeout graceful shutdown в lifespan
2. Добавить draining mode для拒绝 новых запросов
3. Улучшить обработку cancellation в AMQPClient

### Файлы для изменения
- `src/rmq_middleware/main.py`
- `src/rmq_middleware/amqp_wrapper.py`
- `docker-compose.yml`

### Код изменения
```python
# В main.py, в функции lifespan:
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager with improved graceful shutdown."""
    settings = get_settings()
    
    # Startup (unchanged)
    logger.info(
        "Starting RMQ Middleware",
        version=__version__,
        rabbitmq_url=settings.rabbitmq_url_masked,
    )
    
    # Start metrics update task
    metrics_task = asyncio.create_task(update_metrics())
    
    # Connect to RabbitMQ (System Session)
    client = await AMQPClient.get_instance()
    try:
        await client.connect()
    except Exception as e:
        logger.error(f"Failed to connect to RabbitMQ on startup: {e}")
    
    logger.info("RMQ Middleware started successfully")
    
    yield
    
    # Shutdown with extended timeout
    logger.info("Starting graceful shutdown (timeout: 30s)")
    
    # Cancel metrics task
    metrics_task.cancel()
    try:
        await asyncio.wait_for(metrics_task, timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    
    # Give time for in-flight requests to complete
    await asyncio.sleep(2.0)
    
    try:
        # Shutdown AMQP client with timeout
        await asyncio.wait_for(client.shutdown(), timeout=25.0)
    except asyncio.TimeoutError:
        logger.error("AMQP shutdown timeout, forcing closure")
    except Exception as e:
        logger.error(f"Error during AMQP shutdown: {e}")
    
    logger.info("RMQ Middleware shutdown complete")
```

### Тестирование
**Действие**: Запустить integration тесты с graceful shutdown
```bash
uv run pytest tests/test_amqp_client.py::test_shutdown -v
```

### Коммит
**Сообщение**: `fix(shutdown): improve graceful shutdown to prevent message loss`

---

## Шаг 4: Исправление утечки соединений при ошибках

### Проблема
При исключениях во время операций AMQP соединения могут оставаться открытыми, что приводит к утечке ресурсов.

### Изменения кода
1. Улучшить обработку исключений в `UserSession.close()`
2. Добавить гарантированное закрытие соединений в cleanup логике
3. Усилить тесты для проверки отсутствия утечек

### Файлы для изменения
- `src/rmq_middleware/amqp_wrapper.py`
- `tests/test_amqp_client.py`

### Код изменения
```python
# В классе UserSession:
async def close(self):
    """Close all channels and connection with proper error handling."""
    errors = []
    
    try:
        if not self.consumer_channel.is_closed:
            await self.consumer_channel.close()
    except Exception as e:
        errors.append(f"consumer_channel: {e}")
    
    try:
        if not self.publisher_channel.is_closed:
            await self.publisher_channel.close()
    except Exception as e:
        errors.append(f"publisher_channel: {e}")
    
    try:
        if not self.connection.is_closed:
            await self.connection.close()
    except Exception as e:
        errors.append(f"connection: {e}")
    
    if errors:
        logger.warning(f"Errors closing user session: {', '.join(errors)}")
```

### Тестирование
**Действие**: Запустить тесты на утечку соединений
```bash
uv run pytest tests/test_amqp_client.py::test_connect_retry_failure tests/test_amqp_client.py::test_shutdown -v
```

### Коммит
**Сообщение**: `fix(resources): prevent connection leaks by improving error handling in session closure`

---

## Шаг 5: Добавление лимитов на размер сообщений

### Проблема
Отсутствие лимитов на размер сообщений создает риск DoS атак через отправку огромных сообщений.

### Изменения кода
1. Добавить конфигурацию максимального размера сообщений
2. Интегрировать проверку в валидацию запросов
3. Добавить соответствующие HTTP ошибки

### Файлы для изменения
- `src/rmq_middleware/config.py`
- `src/rmq_middleware/security.py`
- `src/rmq_middleware/routes.py`

### Код изменения
```python
# В config.py добавить:
max_message_size_bytes: int = Field(
    default=10485760,  # 10MB
    ge=1024,  # 1KB minimum
    le=104857600,  # 100MB maximum
    description="Maximum allowed message size in bytes",
)

# В security.py добавить:
def validate_message_size(payload: dict | str | bytes) -> None:
    """Validate message size against configured limits."""
    settings = get_settings()
    
    if isinstance(payload, dict):
        # Estimate JSON size
        import json
        estimated_size = len(json.dumps(payload).encode('utf-8'))
    elif isinstance(payload, str):
        estimated_size = len(payload.encode('utf-8'))
    elif isinstance(payload, bytes):
        estimated_size = len(payload)
    else:
        estimated_size = len(str(payload).encode('utf-8'))
    
    if estimated_size > settings.max_message_size_bytes:
        raise InputValidationError(
            f"Message size ({estimated_size} bytes) exceeds maximum "
            f"({settings.max_message_size_bytes} bytes)"
        )
```

### Тестирование
**Действие**: Запустить тесты валидации размера
```bash
uv run pytest tests/test_security.py::TestRequestSize -v
```

### Коммит
**Сообщение**: `fix(security): add message size limits to prevent DoS attacks`

---

## Порядок выполнения

1. **Создать ветку DEV** от текущего main
   ```bash
   git checkout -b dev-critical-fixes
   ```

2. **Выполнить шаги последовательно**:
   - Шаг 1: Исправление логирования
   - Шаг 2: Валидация AMQP имен  
   - Шаг 3: Graceful shutdown
   - Шаг 4: Утечки соединений
   - Шаг 5: Лимиты размера сообщений

3. **После каждого шага**:
   - Запустить указанные тесты
   - Создать коммит с указанным сообщением
   - Убедиться, что все тесты проходят

4. **Финальная проверка**:
   ```bash
   uv run pytest tests/ -v
   docker-compose up -d && sleep 10 && curl http://localhost:8000/ready
   ```

## Критерии завершения ветки DEV

1. Все 5 критических проблем исправлены
2. Все тесты проходят (100% success rate)
3. Приложение успешно запускается и проходит readiness check
4. Каждое исправление изолировано в отдельном коммите с четким сообщением

После выполнения всех шагов ветка DEV готова к review и merge в main.