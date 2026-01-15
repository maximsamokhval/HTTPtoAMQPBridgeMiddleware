# План устранения ошибок Docker после перезапуска

## Контекст
После слияния ветки `dev` в `main` с фиксами для обработки ошибок Content-Length, при перезапуске Docker-контейнеров возникли проблемы с подключением к RabbitMQ. Логи показывают повторяющиеся ошибки `[Errno 111] Connection refused`.

## Корневые причины ошибок

### 1. RabbitMQ недоступен при старте приложения
- **Симптом**: Приложение пытается подключиться к RabbitMQ сразу после запуска, но брокер ещё не готов.
- **Причина**: Отсутствие health check для RabbitMQ и зависимости `depends_on` без условия готовности.
- **Влияние**: Приложение запускается с ошибкой подключения, readiness probe возвращает `amqp_connected: false`.

### 2. Проблемы с vhost `edi`
- **Симптом**: Возможная ошибка аутентификации (не показана в логах, но возможна).
- **Причина**: Vhost `edi` может отсутствовать в RabbitMQ или у пользователя `guest` нет прав.
- **Влияние**: Даже при успешном TCP-подключении аутентификация будет失败.

### 3. Неоптимальные настройки повторных попыток
- **Симптом**: Приложение делает только 5 попыток с экспоненциальной задержкой (1, 2, 4, 8 секунд).
- **Причина**: Настройки `RETRY_ATTEMPTS: 5` и `RETRY_BASE_DELAY: 1.0` могут быть недостаточными для медленно запускающегося RabbitMQ.
- **Влияние**: Приложение прекращает попытки подключения через 15 секунд, хотя RabbitMQ может запускаться дольше.

## Приоритизация ошибок

| Приоритет | Ошибка | Критичность | Влияние |
|-----------|--------|-------------|---------|
| P0 | RabbitMQ недоступен при старте | Критическая | Приложение не может обрабатывать сообщения |
| P1 | Отсутствие vhost `edi` | Высокая | Аутентификация失败 даже при доступном брокере |
| P2 | Неоптимальные настройки повторных попыток | Средняя | Длительное время восстановления при сбоях |
| P3 | Логирование в JSON формате | Низкая | Усложнение диагностики |

## Команды для диагностики

### Проверка состояния контейнеров
```bash
docker ps -a
docker-compose ps
```

### Анализ логов RabbitMQ
```bash
docker logs rabbitmq
docker-compose logs rabbitmq
```

### Проверка сетевой связности
```bash
docker network ls
docker network inspect rmq-middleware_rmq-network
docker exec -it rmq-middleware-app ping rabbitmq
```

### Проверка доступности порта
```bash
docker exec -it rmq-middleware-app nc -zv rabbitmq 5672
# или с использованием curl (если установлен)
docker exec -it rmq-middleware-app curl -s rabbitmq:15672
```

### Проверка vhost и пользователей
```bash
docker exec -it rabbitmq rabbitmqctl list_vhosts
docker exec -it rabbitmq rabbitmqctl list_users
docker exec -it rabbitmq rabbitmqctl list_permissions
```

### Проверка health endpoints приложения
```bash
curl http://localhost:8000/health
curl http://localhost:8000/ready
```

## Необходимые изменения в конфигурации

### 1. Обновление docker-compose.yml

Добавить health check для RabbitMQ и настроить depends_on с условием готовности:

```yaml
services:
  rabbitmq:
    build: ./rabbitmq
    ports:
      - "5672:5672"
      - "15672:15672"
    networks:
      - rmq-network
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    restart: unless-stopped

  middleware:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: rmq-middleware-app
    ports:
      - "8000:8000"
    environment:
      # ... существующие переменные ...
    depends_on:
      rabbitmq:
        condition: service_healthy
    # ... остальная конфигурация ...
```

### 2. Обновление конфигурации RabbitMQ

Проверить и обновить `rabbitmq/definitions.json`:

```json
{
  "users": [
    {
      "name": "guest",
      "password": "guest",
      "tags": "administrator"
    }
  ],
  "vhosts": [
    {
      "name": "edi"
    },
    {
      "name": "/"
    }
  ],
  "permissions": [
    {
      "user": "guest",
      "vhost": "edi",
      "configure": ".*",
      "write": ".*",
      "read": ".*"
    },
    {
      "user": "guest",
      "vhost": "/",
      "configure": ".*",
      "write": ".*",
      "read": ".*"
    }
  ]
}
```

### 3. Обновление настроек приложения

В `src/rmq_middleware/config.py` увеличить параметры повторных попыток:

```python
retry_attempts: int = Field(
    default=10,  # было 5
    ge=1,
    le=30,
    description="Maximum connection retry attempts",
)
retry_base_delay: float = Field(
    default=2.0,  # было 1.0
    ge=0.5,
    le=10.0,
    description="Base delay in seconds for exponential backoff",
)
```

### 4. Обновление переменных окружения

В `.env.example` и `.env` (если используется):

```bash
# Увеличить количество попыток и базовую задержку
RETRY_ATTEMPTS=10
RETRY_BASE_DELAY=2.0

# Для разработки использовать текстовый формат логов
LOG_FORMAT=text
```

### 5. Обновление Dockerfile (опционально)

Добавить утилиты для диагностики в контейнер приложения:

```dockerfile
# Установка сетевых утилит для health check
RUN apt-get update && apt-get install -y \
    curl \
    netcat-openbsd \
    iputils-ping \
    && rm -rf /var/lib/apt/lists/*
```

## Последовательность действий для валидации исправлений

### Этап 1: Базовая диагностика (текущее состояние)
1. Запустить инфраструктуру: `docker-compose up -d`
2. Подождать 60 секунд
3. Выполнить команды диагностики из раздела "Команды для диагностики"
4. Зафиксировать результаты в таблице:
   - Статус контейнеров
   - Логи RabbitMQ
   - Результаты health checks
   - Наличие vhost `edi`

### Этап 2: Внесение изменений
1. Обновить `docker-compose.yml` с health check для RabbitMQ
2. Обновить `rabbitmq/definitions.json` (если необходимо)
3. Обновить `src/rmq_middleware/config.py`
4. Обновить `.env` файл (или переменные окружения)
5. Пересобрать образы: `docker-compose build`

### Этап 3: Перезапуск и валидация
1. Остановить контейнеры: `docker-compose down`
2. Удалить volumes (при необходимости): `docker-compose down -v`
3. Запустить заново: `docker-compose up -d`
4. Мониторить логи: `docker-compose logs -f app`
5. Проверить последовательность событий:
   - Запуск RabbitMQ
   - Health check RabbitMQ становится healthy
   - Запуск приложения
   - Успешное подключение к RabbitMQ

### Этап 4: Функциональное тестирование
1. Проверить health endpoints:
   ```bash
   curl -s http://localhost:8000/health | jq .
   curl -s http://localhost:8000/ready | jq .
   ```
   Ожидаемый результат: `{"status": "ready", "amqp_connected": true}`

2. Запустить тесты проекта:
   ```bash
   uv run pytest tests/ -v
   ```

3. Протестировать API endpoints:
   ```bash
   # Публикация сообщения
   curl -X POST http://localhost:8000/v1/publish \
     -H "Content-Type: application/json" \
     -u "guest:guest" \
     -d '{
       "exchange": "test.exchange",
       "routing_key": "test.key",
       "payload": {"test": "message"},
       "persistence": 1
     }'
   
   # Проверка готовности
   curl -u "guest:guest" http://localhost:8000/v1/fetch \
     -H "Content-Type: application/json" \
     -d '{"queue": "test.queue", "timeout": 1}'
   ```

### Этап 5: Длительное наблюдение
1. Запустить мониторинг логов на 10 минут:
   ```bash
   timeout 600 docker-compose logs -f
   ```
2. Проверить метрики Prometheus (если включены):
   ```bash
   curl http://localhost:8000/metrics | grep rmq_middleware
   ```
3. Провести нагрузочное тестирование (опционально):
   ```bash
   # Использовать k6 или аналогичный инструмент
   k6 run --vus 10 --duration 30s stress_test.js
   ```

### Этап 6: Документирование результатов
1. Создать отчет о проведенных изменениях
2. Обновить `README.md` с информацией о health checks
3. При необходимости добавить новые тесты для сценариев:
   - Медленный запуск RabbitMQ
   - Восстановление после сбоя RabbitMQ
   - Автоматическое переподключение

## Критерии успеха

1. **Основной критерий**: Приложение успешно подключается к RabbitMQ при старте без ошибок `Connection refused`.
2. **Health checks**: Endpoint `/ready` возвращает `amqp_connected: true` в течение 60 секунд после запуска.
3. **Функциональность**: Все тесты проходят, API endpoints работают корректно.
4. **Устойчивость**: При перезапуске RabbitMQ приложение автоматически восстанавливает соединение.
5. **Логирование**: Логи содержат подтверждение успешного подключения и отсутствие ошибок Content-Length.

## Откат изменений

Если изменения приведут к ухудшению ситуации, выполнить откат:

1. Восстановить предыдущие версии файлов из git
2. Пересобрать образы: `docker-compose build`
3. Перезапустить контейнеры: `docker-compose up -d`
4. Проверить работоспособность

## Ответственные и сроки

- **Ответственный**: Команда разработки
- **Срок выполнения**: 1 рабочий день
- **Дата начала**: Немедленно после утверждения плана

---

*Документ создан на основе анализа логов от 2026-01-15. Последнее обновление: 2026-01-15.*