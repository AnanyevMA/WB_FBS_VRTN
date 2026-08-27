# 🗺️ Карта Архитектуры и Символов Проекта (Codebase Map)

> **Автоматически сгенерированный индекс кодовой базы**  
> **Дата актуализации**: 2026-08-27 13:49:07 UTC | **Файлов проиндексировано**: 75  
> **Правило для ИИ-Агентов**: Перед открытием файлов используйте этот справочник или `codebase_index.json` для точечной локализации кода и экономии контекстных токенов.

---

## 1. Архитектурные Слои и Модули

### 🗄️ База Данных & ORM Модели (`app/models/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`app/models/__init__.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/__init__.py) | — | — | Модуль кодовой базы |
| [`app/models/audit.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/audit.py) | `AuditLog` | — | Модуль кодовой базы |
| [`app/models/kiz.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/kiz.py) | `KizOperationType`, `KizOperation`, `KizProductInfo` | — | Модуль кодовой базы |
| [`app/models/order.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/order.py) | `OrderStatus`, `KizStatus`, `Order` | — | Модуль кодовой базы |
| [`app/models/seller.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/seller.py) | `Seller` | — | Модуль кодовой базы |
| [`app/models/supply.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/supply.py) | `SupplyStatus`, `Supply` | — | Модуль кодовой базы |
| [`app/models/user.py`](file:///D:/PyCharm_Projects/WB FBS/app/models/user.py) | `UserRole`, `User` | — | Модуль кодовой базы |

### 📐 Pydantic Схемы & Контракты (`app/schemas/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`app/schemas/__init__.py`](file:///D:/PyCharm_Projects/WB FBS/app/schemas/__init__.py) | — | — | Модуль кодовой базы |
| [`app/schemas/auth.py`](file:///D:/PyCharm_Projects/WB FBS/app/schemas/auth.py) | `LoginRequest`, `TokenPayload`, `UserResponse`, `Token`, `UserCreate`, `UserUpdate`, `PasswordChangeRequest` | — | Pydantic Schemas for Authentication & User Management |
| [`app/schemas/order.py`](file:///D:/PyCharm_Projects/WB FBS/app/schemas/order.py) | `OrderBase`, `OrderResponse`, `OrderListItem`, `KIZAttachRequest`, `KIZValidationResponse` | — | Модуль кодовой базы |
| [`app/schemas/seller.py`](file:///D:/PyCharm_Projects/WB FBS/app/schemas/seller.py) | `DigestSettings`, `SellerBase`, `SellerCreate`, `SellerUpdate`, `SellerResponse`, `SellerListItem` | — | Модуль кодовой базы |

### 🌐 FastAPI Роутеры & Эндпоинты (`app/api/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`app/api/__init__.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/__init__.py) | — | — | Модуль кодовой базы |
| [`app/api/audit.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/audit.py) | — | `GET /sellers/{seller_id}/audit` → `list_audit_logs`<br>`GET /audit` → `list_audit_logs` | Модуль кодовой базы |
| [`app/api/auth.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/auth.py) | — | `POST /login` → `login`<br>`GET /me` → `get_current_user_profile`<br>`POST /change-password` → `change_password`<br>`GET /users` → `list_users`<br>`POST /users` → `create_user_by_admin` | Authentication Router & Dependencies — JWT Login, Current User, and User Management |
| [`app/api/debug.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/debug.py) | — | `GET /status` → `get_debug_status`<br>`POST /seed-mock-data` → `seed_mock_data`<br>`POST /simulate-order-flow` → `simulate_order_flow` | Debug & Testing Router — Отладочный модуль для симуляции и генерации тестовых данных |
| [`app/api/kiz.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/kiz.py) | — | `POST /orders/{order_id}/kiz` → `attach_kiz`<br>`POST /kiz/attach` → `attach_kiz`<br>`POST /kiz/lookup` → `lookup_kiz`<br>`DELETE /orders/{order_id}/kiz` → `detach_kiz`<br>`GET /orders/{order_id}/kiz/validate` → `validate_kiz`<br>`POST /kiz/withdraw` → `withdraw_kiz`<br>`POST /kiz/return` → `return_kiz`<br>`POST /kiz/prepare-document` → `prepare_kiz_document`<br>`POST /kiz/submit-signed-document` → `submit_signed_kiz_document`<br>`GET /kiz/operations` → `list_kiz_operations`<br>`POST /archive/preview` → `preview_wb_archive`<br>`POST /archive/sync-cz` → `sync_archive_kiz_with_cz`<br>`POST /archive/process` → `process_wb_archive` | Модуль кодовой базы |
| [`app/api/orders.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/orders.py) | — | `GET /stats` → `get_dashboard_stats`<br>`GET ` → `list_orders`<br>`GET /{order_id}` → `get_order`<br>`POST /{order_id}/kiz-check` → `check_order_kiz_status`<br>`POST /{order_id}/cancel` → `cancel_order`<br>`POST /{order_id}/mark-assembling` → `mark_assembling`<br>`GET /{order_id}/sticker` → `get_sticker`<br>`POST /sync` → `refresh_orders`<br>`POST /refresh` → `refresh_orders` | Модуль кодовой базы |
| [`app/api/qa.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/qa.py) | — | `POST /run-tests` → `run_qa_tests` | QA Router — Эндпоинты запуска автоматического тестировщика |
| [`app/api/sellers.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/sellers.py) | — | `POST ` → `create_seller`<br>`GET ` → `list_sellers`<br>`GET /{seller_id}` → `get_seller`<br>`PATCH /{seller_id}` → `update_seller`<br>`DELETE /{seller_id}` → `deactivate_seller`<br>`POST /{seller_id}/test-connection` → `test_connection`<br>`POST /{seller_id}/toggle-polling` → `toggle_polling`<br>`GET /{seller_id}/time` → `get_seller_time`<br>`GET /{seller_id}/pending-summary` → `get_pending_summary`<br>`GET /{seller_id}/cz-challenge` → `get_cz_auth_challenge`<br>`POST /{seller_id}/cz-signin` → `cz_signin_with_signature` | Модуль кодовой базы |
| [`app/api/supplies.py`](file:///D:/PyCharm_Projects/WB FBS/app/api/supplies.py) | — | `GET ` → `list_supplies`<br>`POST /sync` → `sync_supplies`<br>`POST /refresh` → `sync_supplies`<br>`POST ` → `create_supply`<br>`GET /{supply_id}` → `get_supply`<br>`GET /{supply_id}/barcode` → `get_supply_barcode`<br>`POST /create-from-pending` → `create_supply_from_pending` | Модуль кодовой базы |

### ⚙️ Бизнес-Логика & Клиенты API (`app/services/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`app/services/__init__.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/__init__.py) | — | — | WB FBS Manager — README |
| [`app/services/archive_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/archive_service.py) | — | `_parse_date_str`, `parse_wb_archive_excel`, `analyze_archive_data` | Archive Service — парсинг и обработка выгрузок архива Wildberries (.xlsx). |
| [`app/services/auth_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/auth_service.py) | — | `hash_password`, `verify_password`, `create_access_token`, `decode_access_token`, +еще 5 | Authentication Service — Password hashing, JWT token handling, and user authentication |
| [`app/services/codebase_indexer.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/codebase_indexer.py) | `CodebaseIndexer` | — | Codebase Semantic & Symbol Indexer — WB FBS Manager |
| [`app/services/crypto_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/crypto_service.py) | `CryptoSignatureError` | `_find_cryptopro_bin`, `sign_document`, `_mock_signature`, `is_cryptopro_available` | КриптоПро Digital Signature Service |
| [`app/services/cz_client.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/cz_client.py) | `CZAPIError`, `CZUnauthorizedError`, `CZDocumentError`, `CZClient` | — | True API & СУЗ 5.0 Client — интеграция с ГИС МТ / СУЗ-Облако 3.0.38 (Честный Знак) |
| [`app/services/encryption.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/encryption.py) | `EncryptionService` | `_get_fernet`, `encrypt`, `decrypt` | Encryption Service — шифрование чувствительных данных (токены API, credentials) |
| [`app/services/kb_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/kb_service.py) | `KBService` | — | Knowledge Base Service & Fast Two-Tier Search Engine — WB FBS Manager |
| [`app/services/kiz_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/kiz_service.py) | — | `is_kiz_withdrawn`, `extract_cz_item_info`, `parse_kiz_code`, `resolve_kiz_product_info`, +еще 2 | Модуль кодовой базы |
| [`app/services/telegram_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/telegram_service.py) | `TelegramService` | `get_telegram_service` | Telegram Notification Service — отправка Push-уведомлений менеджерам |
| [`app/services/time_service.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/time_service.py) | — | `resolve_timezone`, `get_server_time_info`, `get_now_in_timezone`, `get_seller_local_time`, +еще 2 | Time & Timezone Management Service — WB FBS Manager |
| [`app/services/wb_client.py`](file:///D:/PyCharm_Projects/WB FBS/app/services/wb_client.py) | `WBAPIError`, `WBUnauthorizedError`, `WBRateLimitError`, `WBMetaValidationError`, `WBClient` | `is_kiz_required` | Wildberries Marketplace API Client. |

### 🤖 Мультиагентный Слой Celery (`app/agents/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`app/agents/__init__.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/__init__.py) | — | — | Agents package for WB FBS Manager Celery tasks. |
| [`app/agents/archive_processor.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/archive_processor.py) | — | ⚙️ `app.agents.archive_processor.process_all_archives`<br>⚙️ `app.agents.archive_processor.process_seller_archive`<br>⚙️ `app.agents.archive_processor.sync_order_statuses` | Archive Processor Agent — пакетная обработка архива WB для вывода КИЗ из оборота |
| [`app/agents/cleanup.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/cleanup.py) | — | ⚙️ `app.agents.cleanup.cleanup_old_audit_logs` | Cleanup Agent — WB FBS Manager |
| [`app/agents/cz_return.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/cz_return.py) | — | ⚙️ `app.agents.cz_return.return_order_kiz` | CZ Return Agent — Возврат КИЗ в оборот при возврате товара |
| [`app/agents/cz_token_refresher.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/cz_token_refresher.py) | — | ⚙️ `app.agents.cz_token_refresher.refresh_all_tokens` | Chestny Znak Token Refresher Agent — WB FBS Manager |
| [`app/agents/cz_withdrawal.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/cz_withdrawal.py) | — | ⚙️ `app.agents.cz_withdrawal.withdraw_order_kiz`<br>⚙️ `app.agents.cz_withdrawal.process_seller_archive` | CZ Withdrawal Celery Agent — Вывод КИЗ из оборота |
| [`app/agents/kb_sync_agent.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/kb_sync_agent.py) | — | ⚙️ `app.agents.kb_sync_agent.sync_knowledge_base` | Knowledge Base & Codebase Synchronization Agent — WB FBS Manager |
| [`app/agents/morning_digest.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/morning_digest.py) | — | ⚙️ `app.agents.morning_digest.send_morning_digest` | Morning Digest Agent — WB FBS Manager |
| [`app/agents/notifier.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/notifier.py) | — | ⚙️ `app.agents.notifier.notify_new_order`<br>⚙️ `app.agents.notifier.notify_batch_orders`<br>⚙️ `app.agents.notifier.send_cz_status_notification`<br>⚙️ `app.agents.notifier.send_supply_notification`<br>⚙️ `app.agents.notifier.send_alert` | Notifier Agent — отправка уведомлений через Telegram |
| [`app/agents/order_poller.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/order_poller.py) | — | ⚙️ `app.agents.order_poller.get_order_sticker`<br>⚙️ `app.agents.order_poller.poll_all_sellers` | Order Polling Agent — WB FBS Manager |
| [`app/agents/qa_test_agent.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/qa_test_agent.py) | `QATestingError` | ⚙️ `app.agents.qa_test_agent.run_system_regression_tests` | QA Testing Agent — Автоматический агент-тестировщик системы |
| [`app/agents/security_audit_agent.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/security_audit_agent.py) | — | ⚙️ `app.agents.security_audit_agent.run_security_audit` | Security Audit Agent — WB FBS Manager |
| [`app/agents/supply_agent.py`](file:///D:/PyCharm_Projects/WB FBS/app/agents/supply_agent.py) | — | ⚙️ `app.agents.supply_agent.create_supply_for_seller` | Supply Manager Agent — создание и управление поставками WB FBS |

### 🧠 Ядро Системы & Конфигурация (`app/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`app/__init__.py`](file:///D:/PyCharm_Projects/WB FBS/app/__init__.py) | — | — | WB FBS Manager Application |
| [`app/agent_manifest.py`](file:///D:/PyCharm_Projects/WB FBS/app/agent_manifest.py) | `AgentPoLPMatrix`, `AgentConfig`, `GlobalPoLPPolicy`, `ArbitrationRules`, `AgentsManifest`, `PoLPEnforcer` | `load_manifest` | Agent Configuration & PoLP Manifest Module — WB FBS Manager |
| [`app/bot.py`](file:///D:/PyCharm_Projects/WB FBS/app/bot.py) | — | `_get_active_seller`, `get_main_reply_keyboard`, `create_bot_router` | Telegram Bot Handler & Dispatcher — WB FBS Manager |
| [`app/celery_app.py`](file:///D:/PyCharm_Projects/WB FBS/app/celery_app.py) | — | — | Celery Application Configuration — WB FBS Manager |
| [`app/config.py`](file:///D:/PyCharm_Projects/WB FBS/app/config.py) | `Settings` | `get_settings` | Application Configuration — WB FBS Manager |
| [`app/database.py`](file:///D:/PyCharm_Projects/WB FBS/app/database.py) | `Base` | `get_db`, `init_db` | Database setup — async SQLAlchemy engine + session factory |
| [`app/main.py`](file:///D:/PyCharm_Projects/WB FBS/app/main.py) | — | `GET /` → `root`<br>`GET /health` → `health_check` | Модуль кодовой базы |

### 🖥️ Пользовательский Интерфейс (`frontend/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`frontend/index.html`](file:///D:/PyCharm_Projects/WB FBS/frontend/index.html) | — | — | Single Page Application Dashboard (HTML/CSS/JS) |

### 🧪 Набор Автотестов (`tests/`)

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`tests/test_agent_delegation_sync.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_agent_delegation_sync.py) | — | `test_agent_task_registration_and_queues`, `test_celery_beat_schedule_synchronization`, `test_qa_agent_execution_and_audit_logging` | Integration test for Agent Task Delegation, Audit Logging, and Workflow Synchronization. |
| [`tests/test_agent_manifest.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_agent_manifest.py) | — | `test_load_manifest`, `test_development_rules_holistic_and_test_policies`, `test_polp_enforcer_global_forbidden`, `test_polp_enforcer_agent_permissions` | Unit tests for agents_config.json and app.agent_manifest PoLPEnforcer. |
| [`tests/test_auth.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_auth.py) | — | `test_password_hashing_and_verification`, `test_jwt_token_generation_and_decode`, `test_admin_bootstrap_and_login_flow`, `test_protected_routes_require_authentication`, +еще 5 | Tests for Authentication, JWT, User Management, and Endpoint Security Protection |
| [`tests/test_codebase_indexer.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_codebase_indexer.py) | — | `test_codebase_indexer_scan_and_save`, `test_codebase_indexer_fast_symbol_query`, `test_lookup_code_symbol_helper`, `test_codebase_indexing_rule_in_manifest`, +еще 2 | Unit & Integration Tests for Codebase Symbol Indexer and Token-Efficient Search Rule. |
| [`tests/test_cz_client_and_queues.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_cz_client_and_queues.py) | — | `test_encryption_service_compatibility`, `test_cz_client_authenticate_flow`, `test_cz_client_suz_endpoints_and_cises_info`, `test_agent_task_queue_decorators_match_manifest`, +еще 3 | Unit & Integration tests for CZClient, SUZ 3.0.38 endpoints, Task Queues, and EncryptionService. |
| [`tests/test_kb_agent.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_kb_agent.py) | — | `test_kb_service_index_loading_and_structure`, `test_kb_two_tier_fast_search`, `test_kb_get_document_content`, `test_kb_integrity_validation`, +еще 3 | Unit & Integration Tests for Knowledge Base Service and KB Sync Agent. |
| [`tests/test_kiz_service.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_kiz_service.py) | — | `test_parse_kiz_code_standard`, `test_parse_kiz_code_with_parentheses`, `test_parse_kiz_code_with_crypto_tail`, `test_kiz_product_info_model_and_validation`, +еще 8 | Модуль кодовой базы |
| [`tests/test_kiz_signing_endpoints.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_kiz_signing_endpoints.py) | — | `test_prepare_and_submit_kiz_document_endpoints` | Модуль кодовой базы |
| [`tests/test_kiz_withdrawal_return.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_kiz_withdrawal_return.py) | — | `test_kiz_structure_validation`, `test_withdrawal_document_building_with_fias`, `test_golden_schema_withdrawal_and_return_with_receipts`, `test_return_document_building`, +еще 3 | Test suite for KIZ Withdrawal (LP_SHIP_GOODS) and Return (LP_RETURN_GOODS) |
| [`tests/test_morning_digest.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_morning_digest.py) | `TestSellerDigestDue`, `TestMorningDigestTelegramContent`, `TestManifestMorningDigestRegistered`, `TestCeleryBeatMorningDigest`, `TestMorningDigestFailureHandling` | `_stub_aiogram`, `_make_telegram_svc` | Tests: morning_digest agent — timezone-aware fire logic, Telegram message content, |
| [`tests/test_polling_and_digest_schema.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_polling_and_digest_schema.py) | `TestDigestSettings`, `TestSellerCreatePollingInterval`, `TestSellerUpdateDigest`, `TestSellerResponseComputedInterval` | — | Tests: polling interval + digest settings — seller schema validation. |
| [`tests/test_security_agent.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_security_agent.py) | — | `test_security_audit_agent_execution`, `test_security_audit_celery_task` | Tests for Security Audit Agent & Posture Inspection |
| [`tests/test_time_service.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_time_service.py) | `TestTimeServiceResolution`, `TestServerTimeInfo`, `TestSellerTimeFormatting`, `TestIsSellerDigestDue` | — | Unit tests for app.services.time_service. |
| [`tests/test_wb_order_status.py`](file:///D:/PyCharm_Projects/WB FBS/tests/test_wb_order_status.py) | — | `test_wb_client_get_orders_status_endpoint`, `test_refresh_orders_syncs_wb_status_and_supplier_status` | Модуль кодовой базы |

### 📄 Системные Конфигурации & Скрипты

| Файл | Классы / Модели | Функции / Эндпоинты / Таски | Назначение |
|---|---|---|---|
| [`run_bot.py`](file:///D:/PyCharm_Projects/WB FBS/run_bot.py) | — | `get_active_sellers_with_tokens`, `check_bot`, `send_test_notification`, `run_polling`, +еще 1 | Run Telegram Bot — WB FBS Manager |
| [`scripts/generate_secrets.py`](file:///D:/PyCharm_Projects/WB FBS/scripts/generate_secrets.py) | — | `generate_fernet_key`, `generate_random_token`, `generate_password`, `main` | Generate Secure Keys and Passwords for WB FBS Manager |
| [`scripts/set_admin_password.py`](file:///D:/PyCharm_Projects/WB FBS/scripts/set_admin_password.py) | — | `sync_local_env_file`, `is_running_in_docker`, `try_docker_forward`, `set_admin_password_direct`, +еще 1 | Set or Reset Admin Password for WB FBS Manager |
