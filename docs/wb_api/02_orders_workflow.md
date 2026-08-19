# Wildberries Marketplace API v3: Жизненный цикл сборочных заданий (Orders)

> **Категория**: `wb_api` | **Документ ID**: `wb_02_orders_workflow`  
> **Базовый URL**: `https://marketplace-api.wildberries.ru`

---

## 1. Получение Новых Сборочных Заданий

### Эндпоинт: `GET /api/v3/orders/new`
Возвращает список всех новых заказов, ожидающих подтверждения и сборки продавцом.

```http
GET /api/v3/orders/new HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```

#### Пример ответа WB API:
```json
{
  "orders": [
    {
      "id": 12345678,
      "rid": "987654321098",
      "createdAt": "2026-08-15T10:30:00Z",
      "warehouseId": 15432,
      "article": "TSHIRT-BLK-L",
      "nmId": 9876543,
      "chrtId": 456789,
      "price": 250000,
      "convertedPrice": 250000,
      "currencyCode": 643,
      "deliveryType": "fbs",
      "cargoType": 1
    }
  ]
}
```

> **Поля:**
> - `id`: Уникальный ID сборочного задания на WB.
> - `rid`: Идентификатор клиентской корзины.
> - `price`: Цена товара в копейках (250000 = 2500.00 руб).
> - `deliveryType`: Схема доставки (`fbs`).
> - `cargoType`: `1` — обычный товар, `2` — крупногабаритный (СГТ), `3` — сверхкрупногабаритный (КГТ).

---

## 2. Получение Стикеров Заказов (Маркировка Грузомест)

### Эндпоинт: `POST /api/v3/orders/stickers`
Позволяет получить этикетки со штрихкодом сборочного задания для наклейки на индивидуальную упаковку.

#### Тело запроса:
```json
{
  "orders": [12345678],
  "type": "svg",
  "width": 58,
  "height": 40
}
```
*Поддерживаемые форматы `type`: `svg`, `zpl`, `png`.*

#### Пример ответа:
```json
{
  "stickers": [
    {
      "orderId": 12345678,
      "partA": 1234,
      "partB": 5678,
      "barcode": "123456789012",
      "file": "PD94bWwgdmVyc2lvbj0iMS4wIiBlbmNvZGluZz0idXRmLTgiPz4..."
    }
  ]
}
```
- `partA` и `partB`: Верхняя и нижняя части цифрового кода задания для визуального контроля сборщиком.
- `barcode`: Штрихкод Code128 сборочного задания.
- `file`: Base64-encoded контент файла стикера (сохраняется в `storage/stickers/`).

---

## 3. Выгрузка Истории Заказов

### Эндпоинт: `GET /api/v3/orders`
Используется агентом сверки [`app/agents/archive_processor.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agents/archive_processor.py) для синхронизации статусов выполненных или отмененных заказов.

#### Параметры запроса (Query Params):
- `dateFrom`: Начало периода (Unix timestamp, секунды).
- `dateTo`: Конец периода (Unix timestamp, секунды).
- `limit`: Максимальное количество записей (максимум `1000`).
- `next`: Курсор смещения для постраничной пагинации.

---

## 4. Отмена Сборочного Задания

### Эндпоинт: `PATCH /api/v3/orders/{orderId}/cancel`
Используется при обнаружении брака, отсутствии товара на складе или отмене менеджером.

```http
PATCH /api/v3/orders/12345678/cancel HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```
*Возвращает HTTP 204 No Content или 200 OK.*
