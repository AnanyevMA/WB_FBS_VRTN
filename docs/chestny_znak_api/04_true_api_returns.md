# True API ГИС МТ: Возврат КИЗ в Оборот при Отменах и Возвратах

> **Категория**: `chestny_znak_api` | **Документ ID**: `cz_04_true_api_returns`  
> **Спецификация**: True API ГИС МТ / СУЗ 3.0.38 (Документ `LP_RETURN_GOODS`)

---

## 1. Бизнес-Сценарии Возврата КИЗ

Возврат товара в оборот необходим в следующих случаях:
1. **Клиентский возврат на ПВЗ**: Покупатель примерил одежду/обувь и отказался от покупки.
2. **Отмена заказа до вручения**: Заказ отменен покупателем в пути.
3. **Ошибочное списание**: Ошибка оператора при сканировании.

Повторная эмиссия кодов не требуется — ранее выведенный КИЗ возвращается в статус **`INTRODUCED` (В обороте)**.

---

## 2. Формирование Документа `LP_RETURN`

### Эндпоинт: `POST https://ismp.crpt.ru/api/v3/lk/documents/create?pg=lp`

#### Структура конверта запроса:
```json
{
  "document_format": "MANUAL",
  "type": "LP_RETURN",
  "product_document": "<base64_encoded_inner_json>",
  "signature": "<base64_cms_detached_signature>"
}
```

#### Внутренний JSON (`product_document`):
```json
{
  "trade_participant_inn": "190207495060",
  "return_type": "REMOTE_SALE_RETURN",
  "paid": true,
  "primary_document_type": "OTHER",
  "primary_document_number": "5389201923",
  "primary_document_date": "2026-08-18",
  "primary_document_custom_name": "Возврат от покупателя Wildberries FBS",
  "products_list": [
    {
      "ki": "0104630199254371215LgcIxnWSgssC"
    }
  ]
}
```

> **Поля документа:**
> - `trade_participant_inn`: ИНН продавца.
> - `return_type`: `"REMOTE_SALE_RETURN"` (Возврат при дистанционной продаже).
> - `paid`: `true` (признак оплаты/возврата).
> - `primary_document_type`: `"OTHER"` (Вид первичного документа).
> - `primary_document_custom_name`: `"Возврат от покупателя Wildberries FBS"`.
> - `primary_document_date`: Дата первичного документа (`YYYY-MM-DD`).
> - `products_list`: Массив объектов товаров с полем `ki` (Код идентификации).

---

## 3. Обработка Агентом `cz_return`

Агент [`app/agents/cz_return.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agents/cz_return.py):
- Вызывается при получении события возврата от WB API или через ручной ввод в UI / API `POST /api/v1/sellers/{id}/kiz/return`.
- Подписывает документ `LP_RETURN` с помощью сертификата продавца.
- Отправляет документ в ГИС МТ (`https://ismp.crpt.ru/api/v3/lk/documents/create?pg=lp`).
- Переводит статус в БД: `kiz_status = 'RETURNED'`.
- Оповещает склад о возможности повторной продажи единицы товара.
