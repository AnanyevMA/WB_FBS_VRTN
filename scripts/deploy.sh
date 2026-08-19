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
    git pull
else
    echo "ℹ️  Git репозиторий не инициализирован локально, сборка текущих файлов..."
fi

# 2. Сборка контейнеров
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

# 6. Проверка здоровья API
echo "🔍 Проверка статуса API..."
sleep 3
if curl -s http://127.0.0.1/health | grep -q "ok"; then
    echo "✅ Деплой успешно завершен! Сервис работает в штатном режиме."
else
    echo "⚠️  API еще инициализируется или ответил нестандартно. Проверьте логи: docker compose -f docker-compose.prod.yml logs -f api"
fi