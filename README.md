# WB FBS Manager 🚀

[![CI Test & Build](https://github.com/AnanyevMA/WB_FBS_VRTN/actions/workflows/ci.yml/badge.svg)](https://github.com/AnanyevMA/WB_FBS_VRTN/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED.svg)](https://www.docker.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-336791.svg)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Мультитенантный SaaS-сервис** для автоматизации и управления заказами Wildberries по схеме FBS (Fulfillment by Seller) с бесшовной интеграцией государственной системы маркировки «Честный Знак» (ГИС МТ / True API).

---

## 🌟 Ключевые возможности

- 🔔 **Push-уведомления** о новых сборочных заданиях в Telegram с интерактивными кнопками
- 📦 **Управление заказами и поставками** — синхронизация статусов, автоформирование поставок, передача в доставку
- 🏷️ **Маркировка «Честный Знак»** — валидация, привязка КИЗ (SGTIN), автоматический вывод из оборота при продаже и возврат в оборот
- 🔐 **КриптоПро CSP & УКЭП** — подписание документов для True API
- 👥 **Мультиселлер** — независимое изолированное управление несколькими кабинетами WB
- 📊 **Дашборд** — встроенный веб-интерфейс с темной темой для оператора склада
- ⚡ **Легковесность** — оптимизирован для работы даже на серверах **1 vCPU / 1 GB RAM / 30 GB SSD**

---

## 🛠️ Стек технологий

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Alembic
- **Task Queue & Scheduler**: Celery 5.4, Redis 7, Celery Beat
- **Database**: PostgreSQL 16
- **Notifications & Bot**: aiogram 3.x (Telegram Bot API)
- **Web Server & Reverse Proxy**: Nginx 1.27 + Certbot (Let's Encrypt SSL)
- **Crypto Engine**: Cryptography, интеграция с КриптоПро CSP
- **Frontend**: Vanilla HTML5/CSS3/JavaScript (Dark Theme, Glassmorphism)
- **CI/CD & DevOps**: GitHub Actions, Docker Compose

---

## 🚀 Быстрый старт (Локальная разработка)

### 1. Клонирование репозитория
```bash
git clone https://github.com/AnanyevMA/WB_FBS_VRTN.git
cd wb-fbs-manager
```

### 2. Настройка переменных окружения
```bash
cp .env.example .env
python3 scripts/generate_secrets.py
```

### 3. Запуск через Docker Compose (Dev режим)
```bash
docker compose up -d
```

### 4. Доступ к сервисам
- **Дашборд**: [http://localhost:8000](http://localhost:8000) (Учетные данные по умолчанию: логин `admin`, пароль `admin_password`)
- **Swagger API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Flower (Мониторинг Celery)**: [http://localhost:5555](http://localhost:5555)

> 🔒 **Безопасность**: При первом входе в дашборд смените стандартный пароль администратора через меню «Профиль» -> «Смена пароля». Все API-эндпоинты защищены JWT Bearer токенами и ограничены Rate Limiting.

---

## 🌐 Развертывание на VPS (1 Core / 1 GB RAM)

Подробное пошаговое руководство с командами для Ubuntu 22.04/24.04 доступно в файле:
👉 **[DEPLOY_VPS_GUIDE.md](DEPLOY_VPS_GUIDE.md)**

### Краткая инструкция для сервера:

```bash
# 1. Клонировать на сервер в /opt/wb-fbs
git clone https://github.com/AnanyevMA/WB_FBS_VRTN.git /opt/wb-fbs
cd /opt/wb-fbs

# 2. Запустить скрипт настройки VPS (создаст Swap 2GB, установит Docker, настроит UFW и Fail2ban)
chmod +x scripts/*.sh docker/entrypoint.sh
./scripts/setup_vps.sh

# 3. Сгенерировать криптографические ключи
python3 scripts/generate_secrets.py

# 4. Запустить проект в production-режиме
docker compose -f docker-compose.prod.yml up -d --build

# 5. Выпустить бесплатный SSL-сертификат Let's Encrypt
./scripts/init_ssl.sh yourdomain.ru admin@yourdomain.ru
```

---

## 📁 Структура проекта

```
.
├── .github/workflows/          # CI/CD автоматизация (GitHub Actions)
│   ├── ci.yml                  # Запуск тестов и валидация сборки
│   └── deploy.yml              # Автодеплой на VPS по SSH
├── alembic/                    # Миграции базы данных
│   ├── versions/               # Файлы ревизий БД
│   └── env.py
├── app/                        # Исходный код приложения
│   ├── main.py                 # Точка входа FastAPI
│   ├── config.py               # Конфигурация и переменные окружения
│   ├── database.py             # Асинхронный движок БД (SQLAlchemy)
│   ├── celery_app.py           # Инициализация Celery и расписания Beat
│   ├── bot.py                  # Роутер команд и кнопок Telegram
│   ├── api/                    # REST API эндпоинты
│   ├── models/                 # ORM-модели (Seller, Order, Supply, KIZ, Audit)
│   ├── schemas/                # Схемы валидации Pydantic
│   ├── services/               # Бизнес-логика (WB API, True API, КриптоПро, Telegram)
│   └── agents/                 # Задачи Celery (мультиагентная архитектура)
├── docker/
│   └── entrypoint.sh           # Скрипт ожидания БД и авто-миграций
├── frontend/                   # Статический дашборд оператора
├── nginx/                      # Конфигурация обратного прокси Nginx и SSL
│   ├── nginx.conf
│   └── conf.d/
├── scripts/                    # Скрипты автоматизации и администрирования
│   ├── setup_vps.sh            # Первоначальная настройка VPS
│   ├── generate_secrets.py     # Генерация ключей для .env
│   ├── init_ssl.sh             # Выпуск сертификатов Let's Encrypt
│   ├── deploy.sh               # Быстрое обновление проекта
│   ├── backup_db.sh            # Автоматический бэкап PostgreSQL
│   └── check_memory.sh         # Мониторинг RAM/Swap на 1GB сервере
├── systemd/
│   └── wb-fbs.service          # Автозапуск Docker Compose при старте ОС
├── tests/                      # Набор тестов (pytest)
├── Dockerfile                  # Оптимизированный Docker-образ
├── docker-compose.yml          # Compose-конфигурация для разработки
├── docker-compose.prod.yml     # Compose-конфигурация для Production (1 GB RAM)
├── Makefile                    # Шорткаты для удобного управления
└── requirements.txt            # Зависимости Python
```

---

## 🧪 Тестирование

Запуск полного набора тестов:

```bash
# Локально в виртуальном окружении
pytest tests/ -v

# Или через Docker
docker compose exec api pytest tests/ -v
```

---

## 📄 Лицензия

Проект распространяется под лицензией [MIT](LICENSE).