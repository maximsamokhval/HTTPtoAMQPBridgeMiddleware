# План исправления ошибки "Response content longer than Content-Length" в RMQ Middleware

## Анализ проблемы

**Ошибка**: `RuntimeError: Response content longer than Content-Length`

**Контекст**:
- Ошибка возникает после успешной публикации сообщения в RabbitMQ
- В логах видно "Message published", затем ошибка
- Ошибка происходит в uvicorn при отправке HTTP-ответа
- Middleware обработки ошибок пытается отправить ответ об ошибке, но возникает та же ошибка (бесконечный цикл)

**Причина**: Несоответствие между заголовком Content-Length и фактической длиной тела ответа. Это может быть вызвано:
1. Неправильным расчетом длины тела ответа FastAPI/Starlette
2. Проблемой с кодировкой (символы Unicode, которые кодируются в UTF-8 как несколько байт)
3. Проблемой с middleware, который модифицирует ответ после вычисления Content-Length
4. Исключением, возникающим после того, как ответ уже начал отправляться

## Предлагаемые исправления

### Исправление 1: Обработка ошибки "Response content longer than Content-Length" в обработчике исключений

**Файл**: `src/rmq_middleware/main.py`

**Изменения**:
```python
@app.exception_handler(Exception)
async def general_error_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id()
    
    # Special handling for "Response content longer than Content-Length" error
    # This error occurs when uvicorn detects mismatch between Content-Length header
    # and actual response body. If we try to send another response, it will fail again.
    if isinstance(exc, RuntimeError) and "Response content longer than Content-Length" in str(exc):
        logger.error(
            "Response length mismatch error - cannot send error response as body already partially sent",
            request_id=request_id,
            exc_info=True,
        )
        # Return a minimal response without body to avoid infinite error loop
        return JSONResponse(
            status_code=500,
            content=None,
            headers={"X-Error": "internal_error"},
        )
    
    logger.exception(f"Unexpected error: {exc}", request_id=request_id)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_error",
            "detail": "An unexpected error occurred",
            "request_id": request_id,
        },
    )
```

### Исправление 2: Явная сериализация ответа в эндпоинте публикации

**Файл**: `src/rmq_middleware/routes.py`

**Изменения**:
```python
from fastapi.responses import JSONResponse

# В функции publish_message заменить возврат PublishResponse на JSONResponse
return JSONResponse(
    status_code=status.HTTP_202_ACCEPTED,
    content=PublishResponse(
        request_id=request_id,
        exchange=body.exchange,
        routing_key=body.routing_key,
        message_id=body.message_id,
        correlation_id=body.correlation_id or request_id,
    ).model_dump(),
)
```

### Исправление 3: Улучшение обработки исключений в AMQPClient.publish

**Файл**: `src/rmq_middleware/amqp_wrapper.py`

**Изменения**:
```python
async def publish(...):
    # ... существующий код ...
    try:
        amqp_exchange = await session.publisher_channel.get_exchange(exchange)
        
        await asyncio.wait_for(
            amqp_exchange.publish(
                message,
                routing_key=routing_key,
                mandatory=mandatory,
            ),
            timeout=self._settings.publish_timeout,
        )
        
        logger.info(
            "Message published",
            user=credentials[0],
            exchange=exchange,
            routing_key=routing_key,
            correlation_id=message.correlation_id,
        )
        
    except asyncio.TimeoutError:
        raise AMQPPublishError(
            f"Publish confirmation timeout after {self._settings.publish_timeout}s"
        )
    except Exception as e:
        # Логируем все детали исключения
        logger.error(f"Publish failed with exception: {e}", exc_info=True)
        raise AMQPPublishError(f"Failed to publish message: {e}")
```

### Исправление 4: Гарантия, что request_id всегда является строкой

**Файл**: `src/rmq_middleware/middleware.py`

**Изменения**:
```python
def get_request_id() -> str:
    """Get the current request ID from context.
    
    Returns empty string if not in a request context.
    """
    request_id = request_id_ctx.get()
    return request_id if request_id is not None else ""
```

### Исправление 5: Временное отключение Prometheus instrumentation для тестирования

**Файл**: `src/rmq_middleware/main.py`

**Изменения** (опционально, для тестирования):
```python
# Заменить:
# Instrumentator().instrument(app).expose(app)
# На:
if not settings.disable_metrics:  # Добавить настройку
    Instrumentator().instrument(app).expose(app)
```

## Порядок реализации

1. **Сначала применить Исправление 1** - это предотвратит бесконечный цикл ошибок и позволит приложению продолжить работу даже при возникновении проблемы.
2. **Затем применить Исправление 4** - это гарантирует, что request_id всегда является строкой.
3. **Применить Исправление 2** - явная сериализация может помочь избежать проблем с вычислением Content-Length.
4. **Применить Исправление 3** - улучшенное логирование поможет диагностировать будущие проблемы.
5. **Протестировать** каждое исправление отдельно.

## Тестирование

### Шаг 1: Воспроизведение ошибки
1. Запустить RabbitMQ (если не запущен)
2. Запустить RMQ Middleware
3. Отправить запрос на публикацию сообщения:
   ```bash
   curl -X POST http://localhost:8000/v1/publish \
     -H "Content-Type: application/json" \
     -H "Authorization: Basic Z3Vlc3Q6Z3Vlc3Q=" \
     -d '{
       "exchange": "test.exchange",
       "routing_key": "test.key",
       "payload": {"message": "test"}
     }'
   ```
4. Проверить логи на наличие ошибки

### Шаг 2: Проверка исправлений
1. После применения Исправления 1, ошибка должна логироваться, но приложение не должно падать.
2. После применения Исправления 2, ответ должен отправляться корректно.
3. Проверить все эндпоинты API на корректность работы.

## Альтернативные подходы

Если вышеуказанные исправления не решают проблему, рассмотреть:

1. **Обновление зависимостей**:
   ```bash
   pip install --upgrade fastapi uvicorn starlette
   ```

2. **Использование другого ASGI сервера**:
   - Hypercorn вместо uvicorn
   - Daphne

3. **Отключение всех middleware** для диагностики:
   - Временно убрать SecurityHeadersMiddleware, RequestIDMiddleware, Instrumentator

## Риски

1. **Исправление 1** может скрыть реальную проблему, но предотвратит падение приложения.
2. **Исправление 2** может изменить поведение API (но должно быть обратно совместимо).
3. Все изменения должны быть протестированы на всех эндпоинтах.

## Сроки

Исправления могут быть реализованы и протестированы в течение 2-4 часов.

## Ответственные

Разработчики RMQ Middleware.

---

*Дата создания плана: 2026-01-15*
*Статус: Ожидает реализации*