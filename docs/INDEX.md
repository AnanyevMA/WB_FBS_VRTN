# 🧭 База Знаний и База Решений WB FBS Manager & Честный Знак

> **Стандарт и Индекс Документации**  
> **Версия**: 1.0.0 | **Дата актуализации**: 15.08.2026  
> **Назначение**: Центральный навигатор и двухуровневый семантический роутер базы знаний для разработчиков и ИИ-агентов.

---

## ⚡ Регламент быстрого поиска для Агентов (Two-Tier Routing)

> [!IMPORTANT]
> **ПРАВИЛО ДЛЯ АГЕНТОВ**: Не загружайте в контекст все документы подряд!
> 1. Сначала обратитесь к таблице маршрутизации ниже или файлу [`docs/INDEX.json`](file:///D:/PyCharm_Projects/WB%20FBS/docs/INDEX.json).
> 2. Найдите нужный раздел по **Тегам**, **Эндпоинтам** или **Коду Ошибки**.
> 3. Загружайте **только конкретный целевой файл**.

---

## 🗂️ Матрица Маршрутизации и Документов

| Раздел / Тема | Ключевые Эндпоинты / Контекст | Теги & Ошибки | Ссылка на документ |
|---|---|---|---|
| **WB: Авторизация & Лимиты** | `https://marketplace-api.wildberries.ru` | `auth`, `token`, `rate_limit`, `401`, `429` | [`docs/wb_api/01_overview_and_auth.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/wb_api/01_overview_and_auth.md) |
| **WB: Заказы & Стикеры** | `GET /api/v3/orders/new`<br>`POST /api/v3/orders/stickers` | `orders`, `fbs`, `stickers`, `cancel`, `404` | [`docs/wb_api/02_orders_workflow.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/wb_api/02_orders_workflow.md) |
| **WB: Привязка КИЗ (SGTIN)** | `PUT /api/v3/orders/{id}/meta/sgtin`<br>`POST /api/marketplace/v3/orders/meta` | `kiz`, `sgtin`, `meta`, `409 Conflict`, `validation` | [`docs/wb_api/03_kiz_and_meta.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/wb_api/03_kiz_and_meta.md) |
| **WB: Поставки (Supplies)** | `POST /api/v3/supplies`<br>`PATCH /api/v3/supplies/{id}/deliver` | `supplies`, `deliver`, `barcode`, `qr`, `400` | [`docs/wb_api/04_supplies_and_shipment.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/wb_api/04_supplies_and_shipment.md) |
| **ЧЗ: Авторизация & УКЭП** | `GET /api/v3/true-api/auth/key`<br>`POST /api/v3/true-api/auth/simpleSignIn` | `ukep`, `cryptopro`, `gost`, `x-signature`, `cms` | [`docs/chestny_znak_api/01_auth_and_cryptography.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/01_auth_and_cryptography.md) |
| **ЧЗ: Эмиссия СУЗ 5.0** | `POST /api/v3/order`<br>`GET /api/v3/codes` | `suz`, `emission`, `gtin`, `templateId: 10`, `lp` | [`docs/chestny_znak_api/02_suz_emission_and_orders.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/02_suz_emission_and_orders.md) |
| **ЧЗ: Вывод из оборота** | `POST /api/v3/true-api/doc/create` (`LP_SHIP_GOODS`) | `withdrawal`, `remote_sale`, `kopecks`, `fias`, `kpp` | [`docs/chestny_znak_api/03_true_api_withdrawal.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/03_true_api_withdrawal.md) |
| **ЧЗ: Возврат в оборот** | `POST /api/v3/true-api/doc/create` (`LP_RETURN_GOODS`) | `return`, `reintroduce`, `pvz_return`, `reversal` | [`docs/chestny_znak_api/04_true_api_returns.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/04_true_api_returns.md) |
| **ЧЗ: DataMatrix & Сканер** | `POST /api/v3/true-api/cises/info` | `datamatrix`, `gs1`, `ascii_29_gs`, `sgtin_check` | [`docs/chestny_znak_api/05_kiz_structure_and_validation.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/05_kiz_structure_and_validation.md) |
| **ЧЗ: Справочник True API v719.0** | Карта методов, блокировки ОГВ, квитанции | `true_api`, `v719`, `ogvs`, `receipts`, `spec` | [`docs/chestny_znak_api/06_true_api_v719_reference.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/06_true_api_v719_reference.md) |
| **ЧЗ: Спецификация True API (Полная)** | Официальный стандарт ГИС МТ от 18.08.2026 | `true_api`, `v719`, `official_spec`, `gis_mt` | [`docs/True_API_GIS_MT-v719.0-18.08.2026-at-10-23-16.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/True_API_GIS_MT-v719.0-18.08.2026-at-10-23-16.md) |
| **Решения: Сквозной Пайплайн** | Полный бизнес-процесс FBS | `pipeline`, `end_to_end`, `blueprint`, `flowchart` | [`docs/solutions_and_recipes/01_end_to_end_fbs_cz_pipeline.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/solutions_and_recipes/01_end_to_end_fbs_cz_pipeline.md) |
| **Решения: Мультиагенты & PoLP** | `agents_config.json`, Celery, Redis | `celery`, `redis_lock`, `polp`, `arbitration` | [`docs/solutions_and_recipes/02_multiagent_coordination_and_polp.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/solutions_and_recipes/02_multiagent_coordination_and_polp.md) |
| **Решения: Деплой & КриптоПро** | Docker, CSP 5.0, ГОСТ-сертификаты | `deploy`, `docker`, `cryptopro`, `cprocsp`, `pycades` | [`docs/solutions_and_recipes/03_deployment_and_crypto_setup.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/solutions_and_recipes/03_deployment_and_crypto_setup.md) |
| **Troubleshooting: Каталог Ошибок** | Матрица всех ошибок и решений | `409_conflict`, `429_limit`, `invalid_signature`, `rejected` | [`docs/troubleshooting/01_error_catalog_and_resolutions.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/troubleshooting/01_error_catalog_and_resolutions.md) |

---

## 🔍 Быстрый Поиск по Сценариям

### Сценарий 1: Как привязать КИЗ к заказу WB и избежать ошибки 409?
👉 Читать: [`docs/wb_api/03_kiz_and_meta.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/wb_api/03_kiz_and_meta.md) и [`docs/troubleshooting/01_error_catalog_and_resolutions.md#ошибка-409-conflict-metavalidationfail`](file:///D:/PyCharm_Projects/WB%20FBS/docs/troubleshooting/01_error_catalog_and_resolutions.md).

### Сценарий 2: Как подписать запрос в СУЗ / Честный Знак открепленной подписью?
👉 Читать: [`docs/chestny_znak_api/01_auth_and_cryptography.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/01_auth_and_cryptography.md).

### Сценарий 3: Как списать проданный товар в ГИС МТ (дистанционная торговля)?
👉 Читать: [`docs/chestny_znak_api/03_true_api_withdrawal.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/03_true_api_withdrawal.md).

### Сценарий 4: Как работает мультиагентная система и арбитраж очередей?
👉 Читать: [`docs/solutions_and_recipes/02_multiagent_coordination_and_polp.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/solutions_and_recipes/02_multiagent_coordination_and_polp.md) и [`docs/ARCHITECTURE_PLAYBOOK.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/ARCHITECTURE_PLAYBOOK.md).

### Сценарий 5: Где найти полную спецификацию True API ГИС МТ (v719.0) и карту методов?
👉 Читать: [`docs/chestny_znak_api/06_true_api_v719_reference.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/chestny_znak_api/06_true_api_v719_reference.md) и первоисточник [`docs/True_API_GIS_MT-v719.0-18.08.2026-at-10-23-16.md`](file:///D:/PyCharm_Projects/WB%20FBS/docs/True_API_GIS_MT-v719.0-18.08.2026-at-10-23-16.md).

---

## 🤖 Сервисный Агент `kb_sync_agent`
Система автоматически поддерживает актуальность документации и индексов с помощью Celery-агента `kb_sync_agent`. Он периодически проверяет целостность ссылок, контракты внешних API и перестраивает поисковые индексы.
