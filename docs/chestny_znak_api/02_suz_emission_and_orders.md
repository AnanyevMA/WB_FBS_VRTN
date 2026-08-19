# СУЗ-Облако 5.0 (API 3.0.38): Заказ и Эмиссия Кодов Маркировки

> **Категория**: `chestny_znak_api` | **Документ ID**: `cz_02_suz_emission_and_orders`  
> **Спецификация**: СУЗ-Облако 5.0 (API 3.0.38) Секция 4.4.1, 4.4.2, 4.4.4, 4.4.11

---

## 1. Заказ на Эмиссию КМ (Секция 4.4.1)

### Эндпоинт: `POST /api/v3/order?omsId={omsId}`
Заказ генерирует коды маркировки (КМ) для указанного GTIN и товарной группы.

#### Параметры для ТГ «Лёгкая промышленность» (Одежда/Текстиль):
- `productGroup`: `"lp"`
- `templateId`: `10` (Шаблон 10 для потребительской упаковки одежды)
- `cisType`: `"UNIT"` (Единичная упаковка товара)
- `releaseMethodType`: `"PRODUCTION"` (Производство РФ) или `"IMPORT"` (Импорт)
- `serialNumberType`: `"OPERATOR"` (генерация ЦРПТ) или `"SELF_MADE"` (собственная нумерация серийников по 12 символов)

#### Пример тела запроса:
```json
{
  "productGroup": "lp",
  "products": [
    {
      "gtin": "04670033010052",
      "quantity": 500,
      "templateId": 10,
      "cisType": "UNIT",
      "serialNumberType": "OPERATOR"
    }
  ],
  "attributes": {
    "releaseMethodType": "PRODUCTION"
  }
}
```

#### Ответ СУЗ:
```json
{
  "orderId": "b3e945c7-3b2d-419b-a0ee-6d0c41a4a408",
  "expectedStartDate": 1723719600000
}
```

---

## 2. Проверка Готовности Заказа КМ (Секция 4.4.2)

### Эндпоинт: `GET /api/v3/order/status?omsId={omsId}&orderId={orderId}`

#### Возможные статусы заказа:
- `CREATED`: Заказ зарегистрирован в СУЗ.
- `PENDING`: Идет генерация криптохвостов на HSM-модулях ЦРПТ.
- `READY`: Все коды сгенерированы и готовы к выгрузке.
- `REJECTED`: Отклонен (превышен баланс лицевого счета, неверный GTIN).

---

## 3. Выгрузка Готовых Кодов Маркировки (Секция 4.4.4)

### Эндпоинт: `GET /api/v3/codes?omsId={omsId}&orderId={orderId}&gtin={gtin}&quantity={quantity}`

Возвращает массив строк КМ со спецсимволами `\u001d` (ASCII 29 GS separator):
```json
{
  "codes": [
    "0104670033010052215sY2n!t\u001d91ffd0\u001d924v8K...base64crypto..."
  ]
}
```

---

## 4. Отчет об Использовании / Нанесении КМ (Секция 4.4.11)

### Эндпоинт: `POST /api/v3/report/utilisation?omsId={omsId}`
Фиксирует факт печати и физического нанесения DataMatrix этикеток на товар. Без отчета о нанесении коды не могут быть введены в оборот.

#### Структура запроса:
```json
{
  "productGroup": "lp",
  "sntins": [
    "0104670033010052215sY2n!t"
  ],
  "usageType": "VERIFIED"
}
```
