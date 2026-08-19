#!/bin/bash
# =============================================================================
# WB FBS Manager — Automated PostgreSQL Database Backup Script
# Рекомендуется запускать через cron: 0 4 * * * /path/to/project/scripts/backup_db.sh
# =============================================================================
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/backups"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_FILE="$BACKUP_DIR/db_backup_$TIMESTAMP.sql.gz"
RETENTION_DAYS=14

mkdir -p "$BACKUP_DIR"

echo "💾 Запуск бэкапа базы данных WB FBS..."

# Проверка, запущен ли контейнер postgres
CONTAINER_NAME="wbfbs_postgres"
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Ошибка: Контейнер PostgreSQL '${CONTAINER_NAME}' не запущен!"
    exit 1
fi

# Получение параметров пользователя и БД из .env или дефолтных
DB_USER=${POSTGRES_USER:-wbfbs}
DB_NAME=${POSTGRES_DB:-wbfbs}

# Создание сжатого дампа
docker exec -t "$CONTAINER_NAME" pg_dump -U "$DB_USER" -d "$DB_NAME" | gzip > "$BACKUP_FILE"

if [ -s "$BACKUP_FILE" ]; then
    BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "✅ Бэкап успешно создан: $BACKUP_FILE ($BACKUP_SIZE)"
else
    echo "❌ Ошибка: Файл бэкапа пустой!"
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Очистка бэкапов старше RETENTION_DAYS дней
echo "🧹 Очистка бэкапов старше $RETENTION_DAYS дней..."
find "$BACKUP_DIR" -type f -name "db_backup_*.sql.gz" -mtime +"$RETENTION_DAYS" -exec rm {} \;

echo "📊 Текущие доступные бэкапы:"
ls -lh "$BACKUP_DIR"/*.sql.gz 2>/dev/null || echo "Нет бэкапов."