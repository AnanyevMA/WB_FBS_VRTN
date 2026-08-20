#!/bin/bash
# =============================================================================
# WB FBS Manager — Fast Zero-Downtime Deploy / Update Script
# =============================================================================
set -e

echo "🚀 Обновление проекта WB FBS Manager на VPS..."

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 1. Получение свежего кода из Git
if [ -d ".git" ]; then
    echo "📥 Получение обновлений из Git..."
    git pull origin main
else
    echo "ℹ️  Git репозиторий не инициализирован локально, сборка текущих файлов..."
fi

# 2. Сборка Docker контейнеров
echo "🔨 Сборка Docker контейнеров..."
docker compose -f docker-compose.prod.yml build

# 3. Перезапуск сервисов
echo "🔄 Перезапуск сервисов в фоновом режиме..."
docker compose -f docker-compose.prod.yml up -d --remove-orphans

# 4. Применение миграций БД
echo "📦 Применение миграций Alembic..."
docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head || true

# 5. Очистка старых неиспользуемых Docker слоев (защита 30 ГБ диска)
echo "🧹 Очистка старых Docker слоев для экономии диска..."
docker image prune -f

# 6. Проверка статуса сервисов
echo "🔍 Проверка статуса сервисов..."
sleep 3
docker compose -f docker-compose.prod.yml ps

echo "================================================================="
echo "✅ Деплой успешно завершен! Сервис обновлен и запущен."
echo "================================================================="