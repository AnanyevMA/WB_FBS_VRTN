#!/bin/bash
# =============================================================================
# WB FBS Manager — Automated Let's Encrypt SSL Setup Script
# Usage: ./scripts/init_ssl.sh your-domain.com your-email@example.com
# =============================================================================
set -e

DOMAIN=$1
EMAIL=$2

if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
    echo "Использование: $0 <доменное_имя> <email>"
    echo "Пример: $0 my-wb-fbs.ru admin@my-wb-fbs.ru"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "🔐 Настройка SSL Let's Encrypt для домена: $DOMAIN ($EMAIL)..."

# 1. Убеждаемся, что HTTP контейнеры запущены
docker compose -f docker-compose.prod.yml up -d nginx

# 2. Запрос сертификата через Certbot webroot
echo "📜 Запрос сертификата в Let's Encrypt..."
docker compose -f docker-compose.prod.yml run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    --email $EMAIL \
    -d $DOMAIN \
    --rsa-key-size 4096 \
    --agree-tos \
    --no-eff-email \
    --force-renewal" certbot

# 3. Подстановка домена в конфиг Nginx
echo "⚙️ Обновление конфигурации Nginx на HTTPS..."
sed "s/\${DOMAIN_NAME}/$DOMAIN/g" nginx/conf.d/app.ssl.conf.template > nginx/conf.d/app.conf

# 4. Перезагрузка Nginx для применения SSL
echo "🔄 Перезагрузка Nginx..."
docker compose -f docker-compose.prod.yml exec -T nginx nginx -s reload

echo "================================================================="
echo "🎉 SSL успешно настроен!"
echo "Ваш сервис доступен по адресу: https://$DOMAIN"
echo "Сертификаты будут автоматически обновляться сервисом certbot."
echo "================================================================="