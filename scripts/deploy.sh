#!/bin/bash
# =============================================================================
# WB FBS Manager — Fast Zero-Downtime Deploy / Update Script
# =============================================================================
set -e

echo "🚀 Обновление проекта WB FBS Manager на VPS..."

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

# 1. Проверка файла .env (защита от потери конфигурации и паролей)
if [ ! -f ".env" ]; then
    echo "⚠️ Файл .env не найден! Создаю начальный файл конфигурации..."
    if [ -f "scripts/generate_secrets.py" ]; then
        python3 scripts/generate_secrets.py
    elif [ -f ".env.example" ]; then
        cp .env.example .env
    fi
fi

# 2. Получение свежего кода из Git
if [ -d ".git" ]; then
    echo "📥 Получение обновлений из Git..."
    git fetch origin main
    git reset --hard origin/main
else
    echo "ℹ️  Git репозиторий не инициализирован локально, сборка текущих файлов..."
fi

# 3. Сборка Docker контейнеров
echo "🔨 Сборка Docker контейнеров..."
docker compose -f docker-compose.prod.yml build

# 4. Перезапуск сервисов
echo "🔄 Перезапуск сервисов в фоновом режиме..."
docker compose -f docker-compose.prod.yml up -d --remove-orphans

# 5. Применение миграций БД
echo "📦 Применение миграций Alembic..."
docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head || true

# 6. Очистка старых неиспользуемых Docker слоев (защита 30 ГБ диска)
echo "🧹 Очистка старых Docker слоев для экономии диска..."
docker image prune -f

# 7. Проверка статуса сервисов
echo "🔍 Проверка статуса сервисов..."
sleep 3
docker compose -f docker-compose.prod.yml ps

echo "================================================================="
echo "✅ Деплой успешно завершен! Сервис обновлен и запущен."
echo ""
echo "🔑 Учетные данные администратора сохранены в .env (ADMIN_PASSWORD)"
echo "Для смены пароля в любое время выполните:"
echo "  python3 scripts/set_admin_password.py --password 'ваш_новый_пароль'"
echo "================================================================="