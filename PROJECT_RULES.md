# 📜 WB FBS Manager — Главный Регламент Разработки, Архитектуры и Правил Проекта (PROJECT_RULES.md)

> **Назначение документа**: Единый источник истины и операционный регламент для разработчиков и ИИ-агентов (LLM).  
> **Главная цель**: Обеспечить максимальную точность, целостность и безопасность любых доработок системы **при минимальном расходе контекстных токенов LLM**.  
> **Правило первого шага**: Перед выполнением любой задачи в проекте LLM **обязан** руководствоваться данным документом и справочником [`CODEBASE_MAP.md`](file:///d:/PyCharm_Projects/WB%20FBS/CODEBASE_MAP.md).

---

## 1. ⚡ Токеноэффективная Навигация по Проекту (Zero Token Waste)

Проект насчитывает более 130 файлов. Чтобы не расходовать десятки тысяч токенов на слепое чтение исходников:

### 🚫 Золотое правило чтения кода:
1. **НИКОГДА не читать файлы целиком вслепую** (`view_file` на сотни строк для поиска нужного фрагмента).
2. **Используйте двухслойный индекс**:
   - [`CODEBASE_MAP.md`](file:///d:/PyCharm_Projects/WB%20FBS/CODEBASE_MAP.md) — компактная архитектурная карта символов (классы, функции, эндпоинты, таски).
   - [`codebase_index.json`](file:///d:/PyCharm_Projects/WB%20FBS/codebase_index.json) — детальный структурированный AST-индекс.
   - Поиск символов: `.venv\Scripts\python -c "from app.services.codebase_indexer import CodebaseIndexer; print(CodebaseIndexer().query(symbol='ИМЯ_СИМВОЛА'))"`
3. **Открывайте только целевой файл и только нужный диапазон строк** (`StartLine` / `EndLine`).

### 🎯 Экспресс-маршрутизатор (Куда смотреть сразу):

| Домен задачи | Ключевые файлы (Backend) | Ключевые файлы (Frontend / Config) |
|---|---|---|
| **Авторизация / JWT / Админ / Пользователи** | [`app/api/auth.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/auth.py), [`app/services/auth_service.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/auth_service.py), [`app/models/user.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/models/user.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (`showLogin`, `handleLogin`) |
| **Заказы / Сборочные задания / Стикеры** | [`app/api/orders.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/orders.py), [`app/agents/order_poller.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/order_poller.py), [`app/models/order.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/models/order.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (таблица заказов, карточка) |
| **Маркировка КИЗ / SGTIN / Нормализация** | [`app/services/kiz_service.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/kiz_service.py), [`app/api/kiz/attach.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/kiz/attach.py), [`app/models/kiz.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/models/kiz.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (сканер КИЗ, модалка) |
| **Честный Знак / True API / Вывод и Возврат** | [`app/services/cz_client.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/cz_client.py), [`app/agents/cz_withdrawal.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/cz_withdrawal.py), [`app/agents/cz_return.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/cz_return.py) | [`app/api/kiz/documents.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/kiz/documents.py) |
| **Пакетное подписание КИЗ (Signature Batches)** | [`app/api/kiz/signature_batches.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/kiz/signature_batches.py), [`app/services/auto_kiz_queue_service.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/auto_kiz_queue_service.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (очередь на подпись, УКЭП) |
| **Архив WB (.xlsx) / Сверка продаж** | [`app/services/archive_service.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/archive_service.py), [`app/agents/archive_processor.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/archive_processor.py), [`app/api/kiz/archive.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/kiz/archive.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (загрузка архива WB) |
| **Поставки WB FBS / Штрихкоды** | [`app/api/supplies.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/supplies.py), [`app/agents/supply_agent.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/supply_agent.py), [`app/models/supply.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/models/supply.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (поставки, ШК поставки) |
| **Продавцы / Мультиселлер / Токены** | [`app/api/sellers.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/api/sellers.py), [`app/schemas/seller.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/schemas/seller.py), [`app/models/seller.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/models/seller.py) | [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html) (форма добавления/правки) |
| **Telegram-Бот и Уведомления** | [`app/bot/`](file:///d:/PyCharm_Projects/WB%20FBS/app/bot/), [`app/services/telegram_service.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/telegram_service.py), [`app/agents/notifier.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/notifier.py) | [`run_bot.py`](file:///d:/PyCharm_Projects/WB%20FBS/run_bot.py), [`app/agents/morning_digest.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/agents/morning_digest.py) |
| **Национальный Каталог (НКТ)** | [`app/national_catalog/router.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/national_catalog/router.py), [`app/national_catalog/client.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/national_catalog/client.py) | [`app/national_catalog/models.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/national_catalog/models.py) |
| **Celery Очереди / Расписание Beat** | [`app/celery_app.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/celery_app.py), [`agents_config.json`](file:///d:/PyCharm_Projects/WB%20FBS/agents_config.json) | [`docker-compose.prod.yml`](file:///d:/PyCharm_Projects/WB%20FBS/docker-compose.prod.yml) |
| **Деплой / Nginx / Сервер VPS** | [`docker-compose.prod.yml`](file:///d:/PyCharm_Projects/WB%20FBS/docker-compose.prod.yml), [`scripts/deploy.sh`](file:///d:/PyCharm_Projects/WB%20FBS/scripts/deploy.sh), [`nginx/`](file:///d:/PyCharm_Projects/WB%20FBS/nginx/) | [`DEPLOY_VPS_GUIDE.md`](file:///d:/PyCharm_Projects/WB%20FBS/DEPLOY_VPS_GUIDE.md) |

---

## 2. 🗺️ Архитектура Системы и Структура Каталогов

Система построена по слоистой микросервисно-модульной архитектуре:

```
WB FBS Manager
├── app/
│   ├── main.py              # Точка входа FastAPI, инициализация роутеров, CORS, healthcheck
│   ├── config.py            # Pydantic Settings, переменные окружения из .env
│   ├── database.py          # SQLAlchemy 2.0 Async/Sync engine, SessionLocal, Base
│   ├── celery_app.py        # Инициализация Celery, маршрутизация очередей, Beat Schedule
│   ├── agent_manifest.py    # Парсер agents_config.json и PoLPEnforcer (контроль доступа)
│   ├── api/                 # Контроллеры FastAPI (REST API v1)
│   ├── models/              # Декларативные ORM-модели (PostgreSQL/SQLite)
│   ├── schemas/             # Контракты Pydantic v2 (валидация запросов/ответов)
│   ├── services/            # Бизнес-логика, API-клиенты WB, ЧЗ True API, КриптоПро, шифрование
│   ├── agents/              # Задачи Celery (мультиагентный слой фоновых процессов)
│   ├── bot/                 # Telegram-бот на aiogram 3.x (команды, колбэки, документы)
│   └── national_catalog/    # Модуль работы с карточками НКТ (Национальный Каталог ЧЗ)
├── frontend/
│   └── index.html           # Одностраничное SPA-приложение (Vanilla JS, темная тема, дашборд)
├── nginx/                   # Reverse Proxy с динамическим DNS-резолвером и SSL
├── scripts/                 # CLI-утилиты обслуживания (деплой, бэкапы, сброс паролей)
├── tests/                   # Набор модульных, интеграционных и стресс-тестов pytest
├── docker-compose.prod.yml  # Продакшн-оркестрация с жесткими лимитами памяти (1GB RAM)
├── agents_config.json       # Единый конфигурационный манифест агентов и матрица PoLP
├── CODEBASE_MAP.md          # Актуальная AST-карта символов и слоев
└── codebase_index.json      # Машиночитаемый AST-индекс для быстрого поиска
```

---

## 3. 📋 Сквозной Чеклист Доработки Функционала (Holistic Protocol)

При добавлении нового функционала или изменении существующего LLM **обязан** выполнить все шаги чеклиста. Неполная реализация (например, добавление поля в бэкенд без фронтенда) считается браком.

### 📌 Чеклист разработчика (10 шагов):
- [ ] **1. Предварительный анализ воздействия (Holistic Impact Scan)**:  
  Определить, какие именно слои затрагивает изменение: Модели (`app/models/`) → Схемы (`app/schemas/`) → Эндпоинты (`app/api/`) → Агенты (`app/agents/`) → Фронтенд (`frontend/index.html`) → Тесты (`tests/`).
- [ ] **2. ЖЕСТКОЕ ПРАВИЛО ФРОНТЕНДА (Frontend Coverage Required)**:  
  Если добавлено/изменено любое пользовательское поле или настройка (в модели продавца, заказов или настройках очередей), в **этом же самом задании** должны быть обновлены:
  1. HTML-разметка формы ввода в [`frontend/index.html`](file:///d:/PyCharm_Projects/WB%20FBS/frontend/index.html).
  2. JavaScript заполнения формы при редактировании (`editSeller` или аналог).
  3. JavaScript сохранения данных (`saveSeller` или аналог).
  4. Отображение в таблице или списке (колонка/бейдж).
- [ ] **3. Сохранение данных и неразрушающие миграции**:  
  Изменения схемы БД должны быть обратно совместимы с существующими данными. Запрещено удалять колонки без миграции данных.
- [ ] **4. Регистрация задач и очередей Celery**:  
  При создании новой фоновой задачи:
  1. Зарегистрировать модуль в `include` файла [`app/celery_app.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/celery_app.py).
  2. Настроить очередь в `task_routes` (очереди: `orders`, `supplies`, `cz_operations`, `archive`, `notifications`, `maintenance`, `qa_testing`).
  3. Добавить запись агента в [`agents_config.json`](file:///d:/PyCharm_Projects/WB%20FBS/agents_config.json) с матрицей PoLP.
- [ ] **5. Обязательный аудит-след (`AuditLog`)**:  
  Все ключевые операции агентов и сервисов должны логироваться в таблицу `audit_logs` через единую модель: `seller_id`, `agent`, `action`, `entity_type`, `entity_id`, `payload`, `error`, `trace_id`.
- [ ] **6. Написание автотестов (`tests/test_<feature>.py`)**:  
  Каждая новая фича или исправление бага должны сопровождаться тестами в директории `tests/`: позитивный сценарий, пограничные случаи, валидация.
- [ ] **7. Локальный прогон тестов**:  
  Запустить тесты **ТОЛЬКО ЛОКАЛЬНО**: `.venv\Scripts\pytest tests/test_ваша_фича.py`. Убедиться в отсутствии регрессий.
- [ ] **8. Обновление индекса кодовой базы**:  
  Выполнить `.venv\Scripts\python app/services/codebase_indexer.py` для синхронизации [`codebase_index.json`](file:///d:/PyCharm_Projects/WB%20FBS/codebase_index.json) и [`CODEBASE_MAP.md`](file:///d:/PyCharm_Projects/WB%20FBS/CODEBASE_MAP.md).
- [ ] **9. Фиксация в Git**:  
  Закоммитить изменения с понятным сообщением (Conventional Commits: `feat:`, `fix:`, `docs:`) и запушить в ветку `main` удаленного репозитория GitHub.
- [ ] **10. Инструкция по обновлению VPS в ответе пользователю**:  
  В финальном ответе всегда выдать точные команды деплоя на сервере.

---

## 4. 🛡️ Ограничения Окружения и Production Guardrails (VPS 1 GB RAM / 2 GB Swap)

Продакшн-сервер работает в условиях жестких ресурсных ограничений: **1 vCPU / 1 GB RAM / 2 GB Swap (Ubuntu 22.04/24.04)**.

### ⛔ СТРОЖАЙШИЙ ЗАПРЕТ: НИКАКИХ ТЕСТОВ НА VPS (PROD)!
* **КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО** запускать `pytest` или скрипты автотестов на боевом сервере VPS.
* **Причина**: Тесты создают временные сущности, загрязняют боевую базу данных PostgreSQL фиктивными заказами и селлерами, и вызывают резкий всплеск потребления памяти, приводя к срабатыванию Linux OOM Killer.
* **Где тестировать**: Все тесты запускаются **ТОЛЬКО ЛОКАЛЬНО** на машине разработчика в изолированном окружении SQLite!

### 📊 Бюджет памяти и cgroup-лимиты контейнеров (`docker-compose.prod.yml`):
| Сервис | Назначение | Лимит cgroup | Фактическое потребление / Особенности |
|---|---|---|---|
| `worker` | Celery Worker | **290M** | `--concurrency=1`, `--max-tasks-per-child=200`, `--max-memory-per-child=250000` (250 МБ) |
| `scheduler` | Celery Beat | **НЕ МЕНЕЕ 165M** | Базовое потребление ~140M. Лимит ниже 165M приводит к OOM! |
| `bot` | Telegram Bot (aiogram) | **НЕ МЕНЕЕ 135M** | Пиковое потребление ~125M. Лимит ниже 135M приводит к OOM! |
| `api` | FastAPI Backend | **200M** | Рабочий диапазон ~40-70M, запас под параллельные запросы |
| `postgres` | PostgreSQL 16 | **180M** | `shared_buffers=64MB`, `max_connections=30` |
| `redis` | Redis 7 Broker & Cache | **64M** | `maxmemory 64mb`, политика `allkeys-lru` |
| `nginx` | Reverse Proxy & SSL | **48M** | Легковесный Nginx alpine |
| `certbot` | Автопродление Let's Encrypt | **32M** | Запускается только для проверки сертификатов |

> ⚠️ **Суммарный бюджет памяти контейнеров**: ~1100 МБ (с учетом активного Swap 2 ГБ и `swappiness=10`). Запрещено снижать лимиты памяти `bot` (<135M) и `scheduler` (<165M)!

### 🌪️ Защита от Fork Storm в Celery:
* Базовое окружение Python воркера со всеми моделями весит ~186 МБ.
* Если установить `--max-memory-per-child=100000` (100 МБ), воркер будет перезапускать дочерний процесс после **каждой задачи**, вызывая бесконечный цикл форков (Fork Storm), дисковый I/O оверлоад (до 3000+ IOPS) и свопинг.
* **Обязательные параметры воркера**:
  ```yaml
  command: celery -A app.celery_app.celery_app worker -l info -c 1 -Q orders,supplies,cz_operations,archive,notifications,maintenance,default --max-tasks-per-child=200 --max-memory-per-child=250000
  ```
* **Интервалы Celery Beat**:
  - Высокочастотный опрос (60 сек) разрешен **только** для `poll-new-orders`.
  - Все фоновые проверки расписаний, очередей и дайджестов (`morning-digest-check`, `scheduled-orders-digest-check`, `check-archive-reminders`) должны запускаться с интервалом **не чаще одного раза в 300 секунд (5 минут)**.
  - Регламентные задачи (архив, очистка логов, синхронизация базы знаний) запускаются ночью.
  - **Запрещено ставить автотесты в расписание Beat на проде!**

### 🌐 Сеть, Nginx и Zero-Downtime:
1. В `docker-compose.prod.yml` монтируются **оба** файла конфигурации:
   - `./nginx/nginx.conf:/etc/nginx/nginx.conf:ro` (зоны rate-limiting: `api_limit`, `login_limit`)
   - `./nginx/conf.d/app.conf:/etc/nginx/conf.d/app.conf:ro` (виртуальные хосты)
2. В `app.conf` **обязательно** используется встроенный Docker DNS-резолвер с динамической переменной:
   ```nginx
   resolver 127.0.0.11 valid=10s ipv6=off;
   set $upstream_api http://api:8000;
   proxy_pass $upstream_api;
   ```
   Это предотвращает падение Nginx в ошибку `502 Bad Gateway` при пересоздании API-контейнера.
3. Фронтенд обязан четко разделять `HTTP 401` (неверный пароль) и `HTTP 502/503` (сервер перезапускается) и не путать пользователя.

### 💾 Сохранение данных (Data Persistence):
* **КАТЕГОРИЧЕСКИ ЗАПРЕЩЕНО** использовать команду `docker compose down -v` (ключ `-v` безвозвратно уничтожает тома PostgreSQL).
* Стандартное обновление выполняется через:
  ```bash
  git pull origin main && bash scripts/deploy.sh
  ```
* Запрещено перезаписывать существующий файл `.env` шаблонами. Скрипт `scripts/generate_secrets.py` защищен от перезаписи существующего `.env`.

### 🧰 Хостовые CLI-скрипты (Zero Host Dependencies):
Скрипты, запускаемые непосредственно в хостовой ОС (`scripts/set_admin_password.py`, `scripts/backup_db.sh`, `scripts/deploy.sh`), **не должны иметь внешних Python-зависимостей** (никаких pydantic, sqlalchemy, fastapi на верхнем уровне). Все тяжелые операции должны пробрасываться внутрь Docker (`try_docker_forward()`).

---

## 5. 🔑 Безопасность, Авторизация и Самоисцеление (Self-Healing Auth)

В проекте реализована двухуровневая отказоустойчивая аутентификация администратора ([`app/services/auth_service.py`](file:///d:/PyCharm_Projects/WB%20FBS/app/services/auth_service.py)):

1. **Стратегия А (Основная)**: Проверка пароля по bcrypt-хэшу в таблице `users` базы данных.
2. **Стратегия Б (Мастер-аварийная)**: Сверка введенного пароля с переменной `ADMIN_PASSWORD` из `.env`.
   - Если хэш в БД разошелся (например, после восстановления бэкапа или перезапуска), вход по `ADMIN_PASSWORD` из `.env` проходит успешно, а bcrypt-хэш в БД **автоматически обновляется**.
   - Если учетная запись админа была отключена (`is_active=False`), при успешном входе она **автоматически активируется** (`is_active=True`, `is_superuser=True`).
3. **Синхронизация пароля**:
   - При смене пароля через дашборд или утилиту [`scripts/set_admin_password.py`](file:///d:/PyCharm_Projects/WB%20FBS/scripts/set_admin_password.py) значение обновляется **одновременно и в PostgreSQL, и в файле `.env`**.
4. **Защита от пробелов**: Все формы логина и смены пароля в UI и API выполняют `trim()`/`strip()` вводимых данных для защиты от скрытых пробелов при копировании.

---

## 6. 🏷️ Маркировка Честный Знак и True API Инварианты

При работе с кодами маркировки КИЗ / SGTIN / DataMatrix:

1. **Нормализация КИЗ (`normalize_kiz_light_industry`)**:
   - Код для легпрома (ТГ `lp`) имеет длину **31 символ** (01 + GTIN 14 знаков + 21 + серийный номер 13 знаков).
   - Криптохвост (ключ проверки `91` и подпись `92`) отделяется спецсимволами (`\x1d`, пробел или `>`) и не должен ошибочно обрезать серийный номер, если в самом серийном номере случайно встречаются цифры `91` или `92`.
2. **Изоляция документов вывода из оборота**:
   - Поле `cz_withdrawal_doc_id` в таблице `orders` изолировано конкретным заказом. При перепродаже возвращенного товара новый заказ должен иметь возможность сформировать собственный новый документ вывода.
3. **Пакетная очередь на подпись (Signature Batches)**:
   - Уведомления со ссылкой на подписание пакетов КИЗ отправляются **строго в личные чаты ответственных менеджеров** (`is_private_chat`), исключая публичные и групповые чаты для защиты конфиденциальности УКЭП.
4. **Проверка статуса в True API v4**:
   - Поллинг статуса документа (`/api/v4/true-api/doc/{doc_id}/info`) ожидает статус `CHECKED_OK`.
   - При возникновении временных ошибок ГИС МТ (500, 502, 504, 429) задача делает повтор (`retry`) с экспоненциальной задержкой.

---

## 7. ❌ Анти-Паттерны (Чего делать КАТЕГОРИЧЕСКИ нельзя)

| ❌ Анти-паттерн | 💣 Последствия | ✅ Как делать правильно |
|---|---|---|
| Читать файлы всего проекта целиком | Сжигает 30 000+ токенов за 1 запрос | Использовать [`CODEBASE_MAP.md`](file:///d:/PyCharm_Projects/WB%20FBS/CODEBASE_MAP.md) и точечный `view_file` |
| Добавить поле в БД, забыв про UI в `frontend/index.html` | Пользователь не видит и не может настроить поле | Всегда обновлять HTML-форму, JS load, JS save и таблицу |
| Запустить `pytest` на VPS | Падение сервера по OOM, мусор в боевой БД | Тестировать **только локально** на SQLite |
| Использовать `datetime.utcnow()` | Предупреждения и ошибки в Python 3.12+ | Использовать `datetime.now(timezone.utc)` |
| Запустить воркер Celery с лимитом памяти 100M | Fork storm, 3000+ IOPS, отказ сервера | Лимит `--max-memory-per-child=250000` (250 МБ) |
| Выполнить `docker compose down -v` | Полное уничтожение базы данных и токенов | Использовать только `git pull` и `scripts/deploy.sh` |
| Менять пароль админа только в БД | Сброс пароля при следующем перезапуске | Синхронизировать смену в БД и в `.env` |
| Поставить опрос задач в Beat на 5–10 секунд | Высокая нагрузка на CPU и блокировка по API | Интервал проверок от 300 секунд (5 минут) |

---

## 8. 💻 Памятка Команд (Developer & Ops Cheatsheet)

### 🖥️ Локальная разработка и тестирование:
```bash
# Активация виртуального окружения
.venv\Scripts\activate

# Запуск полного набора тестов
.venv\Scripts\pytest

# Запуск конкретного тестового модуля
.venv\Scripts\pytest tests/test_ваша_фича.py -v

# Обновление индекса кодовой базы (CODEBASE_MAP.md и codebase_index.json)
.venv\Scripts\python app/services/codebase_indexer.py

# Быстрый поиск символа в AST-индексе
.venv\Scripts\python -c "from app.services.codebase_indexer import CodebaseIndexer; print(CodebaseIndexer().query(symbol='WBClient'))"
```

### 🚀 Деплой на боевой сервер VPS (команда для ответа пользователю):
```bash
# 1. Подключиться к серверу
ssh root@ВАШ_VPS_IP

# 2. Перейти в каталог проекта и применить обновление
cd /PROJECTS/WB_FBS_VRTN/wb-fbs
git pull origin main
bash scripts/deploy.sh

# 3. Проверить статус контейнеров и здоровье
docker compose -f docker-compose.prod.yml ps
curl -fsS http://127.0.0.1:8000/health
```

---

> 🔒 **Резюме для LLM**: Соблюдение этого регламента гарантирует 100% стабильность сервиса, защиту от OOM-падений на VPS 1 GB RAM, целостность пользовательских данных и экономию до 90% токенов за счет точечной локализации изменений.
