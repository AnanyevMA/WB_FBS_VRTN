#!/bin/bash
# =============================================================================
# WB FBS Manager — Automated Server Log & Storage Cleanup Script
# Запуск через cron: 0 3 * * * /opt/wb-fbs/scripts/cleanup_logs.sh >> /opt/wb-fbs/logs/cleanup.log 2>&1
# =============================================================================
set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOGS_DIR="$PROJECT_DIR/logs"
DAYS_TO_KEEP=7

echo "🧹 [$(date '+%Y-%m-%d %H:%M:%S')] Запуск периодической очистки логов..."

# ─── 1. Очистка файлов логов в директории logs/ старше DAYS_TO_KEEP дней ────
if [ -d "$LOGS_DIR" ]; then
    echo "1. Очистка файлов логов приложения старше $DAYS_TO_KEEP дней..."
    find "$LOGS_DIR" -type f -name "*.log" -mtime +"$DAYS_TO_KEEP" -exec rm -f {} \;
    find "$LOGS_DIR" -type f -name "*.log.*" -mtime +"$DAYS_TO_KEEP" -exec rm -f {} \;
    # Усечение самого файла cleanup.log если он превысил 5MB
    if [ -f "$LOGS_DIR/cleanup.log" ] && [ $(stat -c%s "$LOGS_DIR/cleanup.log" 2>/dev/null || stat -f%z "$LOGS_DIR/cleanup.log" 2>/dev/null || echo 0) -gt 5242880 ]; then
        tail -n 1000 "$LOGS_DIR/cleanup.log" > "$LOGS_DIR/cleanup.log.tmp" && mv "$LOGS_DIR/cleanup.log.tmp" "$LOGS_DIR/cleanup.log"
    fi
fi

# ─── 2. Усечение тяжелых Docker json-логов ──────────────────────────────────
echo "2. Проверка и усечение системных Docker логов контейнеров..."
if command -v docker &> /dev/null; then
    DOCKER_LOGS=$(find /var/lib/docker/containers/ -name "*-json.log" 2>/dev/null || true)
    for log_file in $DOCKER_LOGS; do
        if [ -f "$log_file" ]; then
            FILE_SIZE=$(stat -c%s "$log_file" 2>/dev/null || echo 0)
            # Если лог контейнера больше 15 МБ, усекаем его
            if [ "$FILE_SIZE" -gt 15728640 ]; then
                echo "   Усечение лога: $log_file (размер: $(du -h "$log_file" | cut -f1))"
                truncate -s 0 "$log_file" || true
            fi
        fi
    done

    # ─── 3. Очистка неиспользуемых Docker слоев и временных объектов ───────────
    echo "3. Очистка временных Docker слоев и висячих образов..."
    docker image prune -f --filter "until=72h" || true
    docker container prune -f --filter "until=72h" || true
fi

# ─── 4. Очистка старых записей аудита в БД PostgreSQL ────────────────────────
echo "4. Очистка старых записей AuditLog в базе данных (старше 30 дней)..."
if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^wbfbs_postgres$"; then
    docker exec -t wbfbs_postgres psql -U wbfbs -d wbfbs -c \
        "DELETE FROM audit_logs WHERE created_at < NOW() - INTERVAL '30 days';" 2>/dev/null || true
fi

# ─── 5. Очистка временных файлов Certbot и Nginx ────────────────────────────
if [ -d "$PROJECT_DIR/certbot/www/.well-known" ]; then
    echo "5. Очистка старых проверок Certbot..."
    find "$PROJECT_DIR/certbot/www/.well-known" -type f -mtime +1 -delete 2>/dev/null || true
fi

echo "✅ [$(date '+%Y-%m-%d %H:%M:%S')] Очистка успешно завершена."
echo "📊 Доступное место на диске:"
df -h "$PROJECT_DIR"