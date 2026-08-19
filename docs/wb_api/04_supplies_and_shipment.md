# Wildberries Marketplace API v3: Управление поставками (Supplies) и Отгрузка

> **Категория**: `wb_api` | **Документ ID**: `wb_04_supplies_and_shipment`  
> **Базовый URL**: `https://marketplace-api.wildberries.ru`

---

## 1. Концепция Поставки (Supply) в WB FBS

В схеме FBS сборочные задания объединяются в логическую группу — **Поставку (Supply)**.
- При создании поставки WB присваивает ей номер вида `WB-GI-12345678`.
- Поставка маркируется общим транспортным штрихкодом (ШК поставки).
- По прибытии на СЦ / ПВЗ водитель или курьер предъявляет ШК поставки для приемки.

---

## 2. Эндпоинты Управления Поставками

### 2.1 Создание Новой Поставки
```http
POST /api/v3/supplies HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
Content-Type: application/json

{
  "name": "Поставка FBS от 15.08.2026 Утро"
}
```
**Ответ:**
```json
{
  "id": "WB-GI-12345678"
}
```

---

### 2.2 Добавление Заказа в Поставку
```http
PUT /api/v3/supplies/WB-GI-12345678/orders/12345678 HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```
*Заказ переводится в статус «В сборке / В поставке».*

---

### 2.3 Получение Заказов в Поставке
```http
GET /api/v3/supplies/WB-GI-12345678/orders HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```
**Ответ:**
```json
{
  "orders": [
    {
      "id": 12345678,
      "article": "TSHIRT-BLK-L",
      "createdAt": "2026-08-15T10:30:00Z"
    }
  ]
}
```

---

### 2.4 Получение Штрихкода Поставки
```http
GET /api/v3/supplies/WB-GI-12345678/barcode?type=svg HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```
**Ответ:**
```json
{
  "barcode": "WB-GI-12345678",
  "file": "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmci..."
}
```

---

### 2.5 Закрытие Поставки и Передача в Доставку
```http
PATCH /api/v3/supplies/WB-GI-12345678/deliver HTTP/1.1
Host: marketplace-api.wildberries.ru
Authorization: Bearer <WB_TOKEN>
```

> [!WARNING]
> Если в поставке есть хотя бы один товар, требующий маркировки «Честный Знак», к которому **не привязан** валидный SGTIN, запрос вернет **HTTP 409 Conflict**.
> Агент [`app/agents/supply_agent.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agents/supply_agent.py) перед закрытием поставки проверяет флаги всех входящих заказов.
