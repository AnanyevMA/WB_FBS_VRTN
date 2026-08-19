# Wildberries Marketplace API v3: Авторизация, Лимиты и Мультиселлеринг

> **Категория**: `wb_api` | **Документ ID**: `wb_01_overview_and_auth`  
> **Базовый URL**: `https://marketplace-api.wildberries.ru`  
> **Статус**: Production Standard

---

## 1. Схема Аутентификации

API Wildberries (Маркетплейс FBS v3) использует стандартную Bearer-аутентификацию через HTTP-заголовок `Authorization`.

### Формат заголовков запроса
```http
Authorization: Bearer <WB_API_TOKEN>
Content-Type: application/json
Accept: application/json
```

- Токен генерируется продавцом в Личном кабинете WB:  
  *Настройки → Доступ к API → Создать новый токен (с правами «Маркетплейс» и «Цены и скидки»)*.
- Токен является долгоживущим JWT-токеном, содержащим `supplier_id` и маску прав доступа.

---

## 2. Rate Limits & Политика Ретраев

### Ограничения частоты вызовов (Throttling)
- **Лимит**: до **300 запросов в минуту** (5 req/sec) на один токен продавца.
- **При превышении**: WB возвращает HTTP-статус `429 Too Many Requests`.

### Алгоритм Exponential Backoff
Клиент [`app/services/wb_client.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/services/wb_client.py) реализует ретраи через декоратор `tenacity`:
```python
@retry(
    retry=retry_if_exception_type((WBRateLimitError, httpx.RequestError, httpx.TimeoutException)),
    stop=stop_after_attempt(4),  # 1 попытка + 3 ретрая
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
```

---

## 3. Мультиселлеринг (Multi-Tenant Architecture)

Сервис `WB FBS Manager` поддерживает одновременную работу множества независимых продавцов (юридических лиц / ИП).

- Каждый продавец (`Seller`) имеет собственный зашифрованный `wb_api_token`.
- Шифрование осуществляется модулем [`app/services/encryption.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/services/encryption.py) (AES-256-GCM) с использованием мастер-ключа `ENCRYPTION_KEY`.
- Фоновые агенты опрашивают и обрабатывают заказы каждого селлера в изолированном контексте.

---

## 4. Обработка Стандартных Ошибок WB API

| HTTP Код | Тип Исключения в Проекте | Причина | Действие Системы |
|---|---|---|---|
| `401 Unauthorized` | `WBUnauthorizedError` | Неверный или отозванный токен WB | Деактивация селлера, алерт в Telegram и аудит |
| `429 Too Many Requests` | `WBRateLimitError` | Превышен лимит 300 вызовов/мин | Пауза Celery, экспоненциальный retry (2–10с) |
| `409 Conflict` | `WBMetaValidationError` | Нарушение валидации (нет КИЗ перед поставкой) | Блокировка отгрузки, уведомление оператора |
| `5xx Server Error` | `WBAPIError` | Сбой на стороне Wildberries | Автоматический ретрай через очередь задач |
