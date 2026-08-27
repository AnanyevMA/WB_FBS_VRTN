# True API ГИС МТ: Вывод КИЗ из Оборота (Дистанционная Продажа)

> **Категория**: `chestny_znak_api` | **Документ ID**: `cz_03_true_api_withdrawal`  
> **Спецификация**: True API ГИС МТ / СУЗ 3.0.38 Секция 4.4.9 (Документ `LP_SHIP_GOODS`)

---

## 1. Назначение и Регламент Выбытия

При торговле по схеме Wildberries FBS продавец осуществляет дистанционную продажу конечному физическому лицу.
Согласно правилам маркировки (ФЗ № 488, Постановление Правительства РФ № 1956):
- Продавец обязан сформировать и отправить в ГИС МТ документ вывода из оборота по причине **«Дистанционная продажа»**.
- Срок подачи сведений — не позднее 3 рабочих дней с момента передачи товара в доставку маркетплейсу.

---

## 2. Создание Документа Вывода из Оборота

### Эндпоинт: `POST https://ismp.crpt.ru/api/v3/lk/documents/create?pg=lp`
Запрос передает подписанный JSON-документ `LK_RECEIPT` (вывод из оборота при дистанционной продаже).

#### Структура конверта запроса:
```json
{
  "document_format": "MANUAL",
  "type": "LK_RECEIPT",
  "product_document": "<base64_encoded_inner_json>",
  "signature": "<base64_cms_detached_signature>"
}
```

#### Внутренний JSON (`product_document`):
```json
{
  "inn": "190207495060",
  "action_date": "2026-08-26",
  "action": "DISTANCE",
  "fias_id": "1f06b72d-5b8d-4f0c-a3ee-e0479498b901",
  "products": [
    {
      "cis": "0104630199251318215QTSRh>4sVc+.",
      "product_cost": 308200,
      "primary_document_type": "RECEIPT",
      "primary_document_number": "131749",
      "primary_document_date": "2026-08-26"
    }
  ]
}
```

> **Важные поля тела документа (Эталон True API / ISMP):**
> - `inn`: ИНН продавца.
> - `action`: `"DISTANCE"` (Дистанционная продажа).
> - `action_date`: Дата выбытия (`YYYY-MM-DD`).
> - `fias_id`: Идентификатор ФИАС места осуществления деятельности (МОД/склада продавца).
> - `kpp`: КПП организации (опционально, для юридических лиц).
> - `products[i].cis`: Чистый код маркировки товара (SGTIN без скобок и криптохвостов).
> - `products[i].product_cost`: Цена за единицу товара в **копейках** (целое число, `3082.00 руб.` -> `308200`).
> - `products[i].primary_document_type`: `"RECEIPT"` (при наличии кассового чека) или `"OTHER"`.
> - `products[i].primary_document_number`: Номер кассового чека из отчета маркетплейса (или номер заказа).
> - `products[i].primary_document_date`: Дата кассового чека (`YYYY-MM-DD`).
> - `products[i].primary_document_custom_name`: Передается только при типе документа `"OTHER"` («Продажа через Wildberries FBS»). При типе `"RECEIPT"` не передается.

---

## 3. Асинхронный Мониторинг Обработки Документа

### Эндпоинт: `GET /api/v3/true-api/doc/{docId}/info`

После отправки документ обрабатывается очередью ГИС МТ асинхронно.

```http
GET /api/v3/true-api/doc/4b68e4bf-98f2-49aa-b51c-76e9efc991e2/info HTTP/1.1
Host: markirovka.crpt.ru
Authorization: Bearer <CZ_TOKEN>
```

#### Возможные статусы:
- `IN_PROGRESS`: Документ находится на проверке форматно-логического контроля.
- `PROCESSED`: Документ успешно принят. Все КИЗ переведены в статус **`RETIRED` (Выведен из оборота)**.
- `REJECTED`: Отклонен с ошибкой. В поле `errors` возвращается массив причин (например, КИЗ не принадлежит ИНН или КИЗ не находится в обороте).

---

## 4. Реализация в Celery Агенте `cz_withdrawal`

Агент [`app/agents/cz_withdrawal.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agents/cz_withdrawal.py):
1. Отбирает заказы в статусе `DELIVERED`, где КИЗ имеет статус `ATTACHED` или `VALIDATED`.
2. Формирует тело `LP_SHIP_GOODS`.
3. Подписывает тело УКЭП через [`app/services/crypto_service.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/services/crypto_service.py).
4. Отправляет в True API и сохраняет `doc_id` в таблице `kiz_logs`.
5. Обновляет статус заказа `kiz_status = 'WITHDRAWN'`.
