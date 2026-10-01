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

---

## 5. Архив Сборочных Заданий (>3 дней)

### Эндпоинт: `GET /api/marketplace/v3/fbs/orders/archive` (`getV3FbsOrdersArchive`)
Используется для получения сборочных заданий, которые завершены более 3 дней назад (доставлены или отменены).

```http
GET /api/marketplace/v3/fbs/orders/archive?year=2026&month=10&next=0&limit=1000 HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```

#### Параметры запроса (Query Params):
- `year` (int, обязательный): Год создания заказа (например, `2026`).
- `month` (int, 1–12, обязательный): Месяц создания заказа.
- `next` (int64, обязательный): Курсор пагинации (начинается с `0`, в последующих запросах передается значение `next` из ответа).
- `limit` (int, 100–1000, обязательный): Количество заказов в порции.

#### Структура ответа WB API:
```json
{
  "orders": [
    {
      "id": 5790663669,
      "rid": "1234567890",
      "srid": "abcdef123456",
      "createdAt": "2026-09-15T10:00:00Z",
      "cargoType": 1,
      "priceInfo": {
        "price": 250000,
        "convertedPrice": 250000,
        "currencyCode": 643
      },
      "product": {
        "article": "Платье-01",
        "name": "Платье женское",
        "nmId": 12345678,
        "chrtId": 87654321,
        "skus": ["2000000000001"]
      },
      "status": {
        "supplierStatus": "complete",
        "wbStatus": "canceled_by_client"
      },
      "stickerId": "123456789",
      "warehouseId": 1234,
      "metaDetails": {
        "sgtin": "0104630199252612215*I)EeruudhW6",
        "gtin": "04630199252612",
        "uin": "",
        "imei": ""
      }
    }
  ],
  "next": 0
}
```

#### Статусы заказов в архиве:
- **`status.wbStatus`**:
  - Отмены и возвраты: `"canceled_by_client"` (отмена клиентом), `"declined_by_client"` (отказ в ПВЗ/курьеру), `"canceled"` (отмена продавцом/WB), `"defect"` (брак при доставке).
  - Доставка: `"sold"`, `"delivered"` (успешный выкуп).
- **`status.supplierStatus`**:
  - `"cancel"` — отменено продавцом;
  - `"complete"` — собрано и передано в доставку.
- **`metaDetails.sgtin`**:
  - Содержит прикрепленный к заказу **код маркировки (КИЗ)**.

#### Архитектурная роль в системе (Триада источников архива):
1. **API архива (`GET /api/marketplace/v3/fbs/orders/archive`)**:
   - Автоматический сбор сборочных заданий старше 3 дней со скользящим окном **3 месяца** (`app/services/wb_archive_service.py`).
   - Оперативно выявляет отмены и привязывает КИЗ к заказам со статусом `CANCELLED` для автоматического возврата в оборот ЧЗ.
2. **Детализированный финансовый отчет WB (`wb_finance_service.py`)**:
   - Обновляется еженедельно / раз в месяц по закрытию периодов; фиксирует факт финансовых удержаний и продаж.
3. **Excel-файл архива сборочных заданий (`archive_service.py`)**:
   - Скачивается из ЛК WB вручную; содержит реквизиты фискальных чеков продажи (№ чека, дата чека, ФН) для вывода КИЗ из оборота.

---

## 6. Пакетная Проверка Статусов Активных Заданий

### Эндпоинт: `POST /api/v3/orders/status` (`postV3OrdersStatus`)
Используется агентом фонового мониторинга [`app/agents/order_poller.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agents/order_poller.py) для отслеживания смены статусов активных заказов (в интервале 0–3 дня до ухода в архив).

```http
POST /api/v3/orders/status HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
Content-Type: application/json

{
  "orders": [12345678, 87654321]
}
```

#### Структура ответа:
```json
{
  "orders": [
    {
      "id": 12345678,
      "supplierStatus": "complete",
      "wbStatus": "sold",
      "isCancellable": false
    }
  ]
}
```
- До 1000 ID заказов в одном запросе.
- Позволяет оперативно переводить выкупленные закары в `DELIVERED` (и запускать вывод КИЗ), а отмененные — в `CANCELLED` (и запускать возврат КИЗ) до архивации.

