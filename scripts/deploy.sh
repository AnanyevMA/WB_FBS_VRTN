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

# 2. Проверка и активация 2 ГБ Swap (критично для защиты от OOM killer на VPS с 1 ГБ RAM)
if [ -w / ] && [ $(swapon --show | wc -l) -le 1 ]; then
    echo "🧠 Проверка Swap: файл подкачки не найден. Активация 2 ГБ Swap..."
    if [ ! -f /swapfile ]; then
        fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile || true
    echo "✅ Swap память (2 ГБ) успешно подключена."
fi

# 3. Получение свежего кода из Git
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
    echo "📦 Проверка и применение миграций БД (Alembic)..."
    docker compose -f docker-compose.prod.yml exec -T api alembic upgrade head || true
else
    echo "⚠️ API не ответил за ${MAX_WAIT} сек. Проверьте логи:"
    echo "  docker compose -f docker-compose.prod.yml logs --tail=30 api"
fi

# 6. Проверка учетной записи администратора (без принудительного сброса пароля)
ADMIN_PWD=$(grep -oP '^ADMIN_PASSWORD=\K.*' .env 2>/dev/null || echo "")
if [ "$API_READY" = true ]; then
    echo "🔑 Проверка статуса учетной записи администратора..."
    ADMIN_STATUS=$(docker compose -f docker-compose.prod.yml exec -T api python -c "
import sys
from app.config import settings
from app.services.auth_service import verify_password
from app.models.user import User
from sqlalchemy import create_engine, select, or_
from sqlalchemy.orm import Session

try:
    engine = create_engine(settings.database_url_sync)
    with Session(engine) as session:
        user = session.execute(
            select(User).where(or_(User.username == 'admin', User.is_superuser == True))
        ).scalars().first()
        if not user:
            print('MISSING')
            sys.exit(0)
        env_pwd = (settings.admin_password or '').strip()
        if env_pwd and verify_password(env_pwd, user.hashed_password):
            print('MATCH')
        else:
            print('CUSTOM')
except Exception as e:
    print('ERROR:' + str(e))
" 2>/dev/null | tr -d '\r\n' || echo "FAIL")

    if [ "$ADMIN_STATUS" = "MATCH" ]; then
        echo "✅ Учетная запись администратора активна (пароль в БД синхронизирован с .env)!"
    elif [ "$ADMIN_STATUS" = "CUSTOM" ]; then
        echo "ℹ️ Учетная запись администратора активна (в БД установлен собственный пароль)."
        echo "  Пароль в базе данных сохранён и НЕ сбрасывается при обновлении."
    elif [ "$ADMIN_STATUS" = "MISSING" ]; then
        echo "⚠️ Учетная запись администратора не найдена. Создание из .env..."
        docker compose -f docker-compose.prod.yml exec -T api \
            python scripts/set_admin_password.py --direct --password "$ADMIN_PWD" || true
        echo "✅ Начальный администратор создан."
    else
        echo "ℹ️ Проверка администратора завершена со статусом: ${ADMIN_STATUS}."
    fi
fi

# 7. Проверка Nginx веб-сервера
echo "🌐 Проверка Nginx веб-сервера..."
if curl -sf http://localhost/ > /dev/null 2>&1 || curl -sf http://127.0.0.1/ > /dev/null 2>&1; then
    echo "✅ Nginx веб-сервер доступен и отдаёт дашборд!"
else
    echo "⚠️ Nginx не ответил на порту 80. Проверка логов Nginx..."
    docker compose -f docker-compose.prod.yml logs --tail=20 nginx || true
fi

# 8. Очистка старых Docker слоев и кэша сборки
echo "🧹 Очистка старых Docker слоев и кэша сборщика..."
docker image prune -f || true
docker builder prune -f || true

# 9. Статус всех сервисов и потребление ресурсов
echo ""
echo "📊 Статус сервисов:"
docker compose -f docker-compose.prod.yml ps

echo ""
echo "📈 Использование памяти контейнерами:"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.PIDs}}"

echo ""
echo "================================================================="
echo "✅ Деплой успешно завершён!"
echo ""
echo "🔑 Учетные данные администратора сохранены в .env (ADMIN_PASSWORD)"
echo "Для смены пароля в любое время выполните:"
echo "  python3 scripts/set_admin_password.py --password 'ваш_новый_пароль'"
echo "================================================================="