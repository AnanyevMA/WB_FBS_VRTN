# WB FBS Manager — Makefile
.PHONY: up down build logs migrate test prod-up prod-down prod-logs prod-build secrets backup memory help

help:
	@echo "WB FBS Manager — Доступные команды:"
	@echo "  make up          — Запуск в режиме разработки (docker-compose.yml)"
	@echo "  make down        — Остановка локальных сервисов"
	@echo "  make logs        — Просмотр логов локальных сервисов"
	@echo "  make prod-up     — Запуск в продакшн режиме (docker-compose.prod.yml)"
	@echo "  make prod-down   — Остановка продакшн сервисов"
	@echo "  make prod-logs   — Просмотр логов продакшн сервисов"
	@echo "  make prod-build  — Пересборка продакшн контейнеров"
	@echo "  make secrets     — Генерация криптографических ключей для .env"
	@echo "  make migrate     — Применение миграций Alembic"
	@echo "  make backup      — Создание резервной копии базы данных"
	@echo "  make memory      — Проверка потребления RAM и Swap на сервере"
	@echo "  make test        — Запуск набора тестов pytest"

# ─── Разработка ───────────────────────────────────────────────────────────────
up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f

logs-api:
	docker compose logs -f api

logs-worker:
	docker compose logs -f worker

logs-bot:
	docker compose logs -f bot

# ─── Продакшн (1 GB RAM VPS) ──────────────────────────────────────────────────
prod-up:
	docker compose -f docker-compose.prod.yml up -d

prod-down:
	docker compose -f docker-compose.prod.yml down

prod-build:
	docker compose -f docker-compose.prod.yml build

prod-logs:
	docker compose -f docker-compose.prod.yml logs -f

# ─── База данных и миграции ───────────────────────────────────────────────────
migrate:
	docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head

makemigrations:
	docker compose -f docker-compose.prod.yml exec -T api alembic revision --autogenerate -m "$(msg)"

backup:
	@bash scripts/backup_db.sh

# ─── Утилиты ──────────────────────────────────────────────────────────────────
secrets:
	@python3 scripts/generate_secrets.py || python scripts/generate_secrets.py

memory:
	@bash scripts/check_memory.sh

test:
	pytest tests/ -v
clean-logs:
	@bash scripts/cleanup_logs.sh
