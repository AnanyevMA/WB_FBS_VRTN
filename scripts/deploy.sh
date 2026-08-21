#!/bin/bash
# =============================================================================
# WB FBS Manager — Deploy / Update Script
# Безопасное обновление с гарантированным перезапуском всех сервисов
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

# 4. Перезапуск ВСЕХ сервисов (включая nginx для обновления DNS)
echo "🔄 Перезапуск всех сервисов..."
docker compose -f docker-compose.prod.yml up -d --force-recreate --remove-orphans

# 5. Ожидание готовности API с health-check
echo "⏳ Ожидание запуска API..."
MAX_WAIT=60
ELAPSED=0
API_READY=false

while [ $ELAPSED -lt $MAX_WAIT ]; do
    if docker compose -f docker-compose.prod.yml exec -T api curl -sf http://localhost:8000/health > /dev/null 2>&1; then
        API_READY=true
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
    echo "  ... ожидание ($ELAPSED сек)..."
done

if [ "$API_READY" = true ]; then
    echo "✅ API сервер запущен и отвечает! (за ${ELAPSED} сек)"
else
    echo "⚠️ API не ответил за ${MAX_WAIT} сек. Проверьте логи:"
    echo "  docker compose -f docker-compose.prod.yml logs --tail=30 api"
fi

# 6. Проверка входа администратора
ADMIN_PWD=$(grep -oP '^ADMIN_PASSWORD=\K.*' .env 2>/dev/null || echo "")
if [ -n "$ADMIN_PWD" ] && [ "$API_READY" = true ]; then
    echo "🔑 Проверка входа администратора..."
    LOGIN_RESULT=$(docker compose -f docker-compose.prod.yml exec -T api curl -sf \
        -X POST http://localhost:8000/api/v1/auth/login \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"admin\",\"password\":\"${ADMIN_PWD}\"}" 2>/dev/null || echo "FAIL")

    if echo "$LOGIN_RESULT" | grep -q "access_token"; then
        echo "✅ Вход администратора работает!"
    else
        echo "⚠️ Вход администратора не удался. Синхронизация пароля из .env..."
        docker compose -f docker-compose.prod.yml exec -T api \
            python scripts/set_admin_password.py --direct --password "$ADMIN_PWD" || true
        echo "🔄 Пароль синхронизирован. Попробуйте войти в дашборд."
    fi
fi

# 7. Очистка старых Docker слоев
echo "🧹 Очистка старых Docker слоев..."
docker image prune -f

# 8. Статус всех сервисов
echo ""
echo "📊 Статус сервисов:"
docker compose -f docker-compose.prod.yml ps

echo ""
echo "================================================================="
echo "✅ Деплой успешно завершён!"
echo ""
echo "🔑 Учетные данные администратора сохранены в .env (ADMIN_PASSWORD)"
echo "Для смены пароля в любое время выполните:"
echo "  python3 scripts/set_admin_password.py --password 'ваш_новый_пароль'"
echo "================================================================="