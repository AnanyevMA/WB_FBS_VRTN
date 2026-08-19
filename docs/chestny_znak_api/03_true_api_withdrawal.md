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
  "action": "DISTANCE",
  "action_date": "2026-08-18",
  "document_type": "OTHER",
  "document_number": "5389201923",
  "document_date": "2026-08-18",
  "primary_document_custom_name": "Продажа через Wildberries FBS",
  "fias_id": "1f06b72d-5b8d-4f0c-a3ee-e0479498b901",
  "products": [
    {
      "cis": "0104630199254371215LgcIxnWSgssC",
      "product_cost": 247500
    }
  ]
}
```

> **Важные поля тела документа:**
> - `inn`: ИНН продавца.
> - `action`: `"DISTANCE"` (Дистанционная продажа).
> - `action_date`: Дата выбытия (`YYYY-MM-DD`).
> - `cis`: Код идентификации товара.
> - `product_cost`: Цена за единицу товара в **копейках** (целое число).
> - `primary_document_custom_name`: Наименование первичного документа («Продажа через Wildberries FBS»).
> - `document_number`: Номер сборочного задания WB.
> - `fias_id`: Идентификатор ФИАС места осуществления деятельности (МОД).

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
