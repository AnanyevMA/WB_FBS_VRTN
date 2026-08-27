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
  "products_list": [
    {
      "ki": "0104630199251332215ZEdKVTnFahrt",
      "primary_document_type": "RECEIPT",
      "primary_document_number": "217837",
      "primary_document_date": "27.08.2026",
      "certificate_type": "CONFORMITY_DECLARATION",
      "certificate_number": "ЕАЭС N RU Д-RU.РА05.В.88154/22",
      "certificate_date": "29.08.2022"
    }
  ]
}
```

> **Поля документа (Эталон True API / ISMP):**
> - `trade_participant_inn`: ИНН продавца.
> - `return_type`: `"REMOTE_SALE_RETURN"` (Возврат при дистанционной продаже).
> - `paid`: `true` (признак оплаты/возврата).
> - `products_list[i].ki`: Чистый код маркировки товара (`ki` / `cis`).
> - `products_list[i].primary_document_type`: `"RECEIPT"` (при наличии чека возврата) или `"OTHER"`.
> - `products_list[i].primary_document_number`: Номер чека возврата или номер сборочного задания.
> - `products_list[i].primary_document_date`: Дата первичного документа / чека (`YYYY-MM-DD` или `dd.MM.yyyy`).
> - `products_list[i].certificate_type`: Тип разрешительного документа (например, `"CONFORMITY_DECLARATION"`).
> - `products_list[i].certificate_number`: Номер декларации/сертификата соответствия.
> - `products_list[i].certificate_date`: Дата регистрации декларации.
>
> **Правило обработки возвратов при сверке архива WB (`archive.xlsx`):**
> - Если в отчете WB в колонке «Тип операции» указан «Возврат» (или заказ отменен покупателем), система проверяет реальный статус КИЗ в ГИС МТ.
> - **Случай 1 (КИЗ в статусе `RETIRED` / `WITHDRAWN`)**: Товар был списан, но еще не введен обратно. Требуется подача документа `LP_RETURN`.
> - **Случай 2 (КИЗ уже в статусе `INTRODUCED` / «В обороте»)**: Продавец уже вернул товар в оборот. Повторный ввод в ГИС МТ **не требуется**; КИЗ освобождается и становится доступен для привязки к новым заказам.

---

## 3. Обработка Агентом `cz_return`

Агент [`app/agents/cz_return.py`](file:///D:/PyCharm_Projects/WB%20FBS/app/agents/cz_return.py):
- Вызывается при получении события возврата от WB API или через ручной ввод в UI / API `POST /api/v1/sellers/{id}/kiz/return`.
- Подписывает документ `LP_RETURN` с помощью сертификата продавца.
- Отправляет документ в ГИС МТ (`https://ismp.crpt.ru/api/v3/lk/documents/create?pg=lp`).
- Переводит статус в БД: `kiz_status = 'RETURNED'`.
- Оповещает склад о возможности повторной продажи единицы товара.
