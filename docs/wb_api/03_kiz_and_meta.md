# Wildberries Marketplace API v3: Привязка КИЗ (SGTIN) и Валидация Метаданных

> **Категория**: `wb_api` | **Документ ID**: `wb_03_kiz_and_meta`  
> **Базовый URL**: `https://marketplace-api.wildberries.ru`

---

## 1. Зачем передавать КИЗ в Wildberries?

Для товаров, подлежащих обязательной маркировке («Честный Знак»: одежда, обувь, духи, текстиль и др.), правила площадки WB требуют привязки кода идентификации (КИЗ / SGTIN / КиЗ) к сборочному заданию **ДО** закрытия поставки и передачи в сортировочный центр.

Если попытаться закрыть поставку без привязки КИЗ к маркированным заказам, WB API возвращает фатальную ошибку:
> `HTTP 409 Conflict — MetaValidationFail (KIZ not attached or validation failed)`

---

## 2. Привязка КИЗ к Заказу

### Эндпоинт: `PUT /api/v3/orders/{orderId}/meta/sgtin`

Привязывает код маркировки (SGTIN) к указанному сборочному заданию.

#### Формат запроса:
```http
PUT /api/v3/orders/12345678/meta/sgtin HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
Content-Type: application/json

{
  "shgt": [
    "0104670033010052215...<GS>91ffd0<GS>92..."
  ]
}
```

> **Важно:** Поле `"shgt"` принимает массив строк. Для схемы FBS передается один КИЗ на единицу товара в заказе.

---

## 3. Проверка Статуса Валидации КИЗ со стороны WB

### Эндпоинт: `POST /api/marketplace/v3/orders/meta`

Позволяет пакетно запросить метаданные заказов и статус проверки кодов маркировки.

#### Тело запроса:
```json
{
  "orders": [12345678, 12345679]
}
```

#### Пример ответа WB:
```json
{
  "meta": [
    {
      "orderId": 12345678,
      "sgtin": "0104670033010052215abcd",
      "sgtinStatus": "VALIDATED",
      "updatedAt": "2026-08-15T10:45:00Z"
    }
  ]
}
```

### Статусы валидации КИЗ в WB:
- `PENDING`: Код принят, находится на асинхронной верификации в ГИС МТ через шлюз WB.
- `VALIDATED`: Код успешно подтвержден. Заказ готов к отгрузке в поставку.
- `REJECTED` / `INVALID`: Код не прошел валидацию (не введен в оборот, чужой ИНН или ошибка криптохвоста).

---

## 4. Архитектурная реализация в проекте

В файле [`app/services/wb_client.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/services/wb_client.py) методы реализованы как:
- `set_order_sgtin(order_id, kiz_codes)`
- `get_orders_meta(order_ids)`

При сканировании 2D-сканером в UI или автоматической привязке агентом выполняется:
1. Валидация формата GS1 DataMatrix.
2. Проверка статуса в ГИС МТ True API (`POST /cises/info`).
3. Отправка в WB API (`PUT /meta/sgtin`).
4. Обновление статуса в локальной таблице `orders.kiz_status = 'ATTACHED'`.
