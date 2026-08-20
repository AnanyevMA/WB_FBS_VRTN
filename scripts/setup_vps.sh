#!/bin/bash
# =============================================================================
# WB FBS Manager — Automated VPS Setup Script
# Target OS: Ubuntu 22.04 LTS / 24.04 LTS (Optimized for 1 Core / 1 GB RAM / 30 GB SSD)
# =============================================================================
set -e

echo "🚀 Начало первоначальной настройки VPS для WB FBS Manager..."

# Проверка прав root
if [ "$EUID" -ne 0 ]; then
  echo "❌ Ошибка: скрипт должен быть запущен с правами root (sudo)."
  exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ─── 1. Обновление пакетов ──────────────────────────────────────────────────
echo "📦 Обновление системных пакетов..."
apt-get update && apt-get upgrade -y
apt-get install -y \
    curl \
    git \
    ufw \
    fail2ban \
    logrotate \
    ca-certificates \
    gnupg \
    lsb-release \
    htop \
    ncdu \
    python3 \
    python3-pip \
    python3-venv

# ─── 2. Настройка SWAP (2 ГБ — критически важно для 1 ГБ RAM) ──────────────
echo "🧠 Проверка и настройка Swap памяти..."
if [ $(swapon --show | wc -l) -le 1 ]; then
    echo "Создание файла подкачки (Swap) на 2 ГБ..."
    fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
    
    # Настройка агрессивности использования Swap (swappiness=10)
    sysctl vm.swappiness=10
    sysctl vm.vfs_cache_pressure=50
    echo "vm.swappiness=10" >> /etc/sysctl.conf
    echo "vm.vfs_cache_pressure=50" >> /etc/sysctl.conf
    echo "✅ Swap 2 ГБ успешно активирован!"
else
    echo "ℹ️  Swap уже существует, пропускаем создание."
fi

# ─── 3. Установка Docker CE и Docker Compose ────────────────────────────────
echo "🐳 Проверка установки Docker..."
if ! command -v docker &> /dev/null; then
    echo "Установка Docker CE..."
    mkdir -p /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    echo \
      "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
      $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable docker
    systemctl start docker
    echo "✅ Docker успешно установлен!"
else
    echo "ℹ️  Docker уже установлен."
fi

# ─── 4. Настройка глобального лимита логов Docker ───────────────────────────
echo "⚙️ Настройка daemon.json для Docker (защита диска 30 ГБ от переполнения логами)..."
mkdir -p /etc/docker
cat << 'EOF' > /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
systemctl restart docker

# ─── 5. Настройка Logrotate и расписания очистки логов ──────────────────────
echo "🧹 Настройка автоматической ротации и очистки логов..."
if [ -f "$PROJECT_DIR/scripts/logrotate.conf" ]; then
    cp "$PROJECT_DIR/scripts/logrotate.conf" /etc/logrotate.d/wb-fbs
    chmod 644 /etc/logrotate.d/wb-fbs
fi

# Добавление задач очистки логов и бэкапов в cron (если еще не добавлены)
CRON_TMP=$(mktemp)
crontab -l 2>/dev/null > "$CRON_TMP" || true

if ! grep -q "cleanup_logs.sh" "$CRON_TMP"; then
    echo "0 3 * * * $PROJECT_DIR/scripts/cleanup_logs.sh >> $PROJECT_DIR/logs/cleanup.log 2>&1" >> "$CRON_TMP"
    echo "✅ Задача ежедневной очистки логов в 03:00 добавлена в cron."
fi

if ! grep -q "backup_db.sh" "$CRON_TMP"; then
    echo "0 4 * * * $PROJECT_DIR/scripts/backup_db.sh >> $PROJECT_DIR/logs/backup.log 2>&1" >> "$CRON_TMP"
    echo "✅ Задача ежедневного бэкапа БД в 04:00 добавлена в cron."
fi

crontab "$CRON_TMP"
rm -f "$CRON_TMP"

# ─── 6. Настройка Фаервола (UFW) ───────────────────────────────────────────
echo "🛡️ Настройка фаервола UFW (открываем 22, 80, 443; закрываем БД от внешнего мира)..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP'
ufw allow 443/tcp comment 'HTTPS'
ufw --force enable

# ─── 7. Настройка Fail2ban (защита SSH) ─────────────────────────────────────
echo "🔒 Запуск и включение Fail2ban..."
systemctl enable fail2ban
systemctl restart fail2ban

# ─── 8. Создание рабочих директорий ─────────────────────────────────────────
echo "📁 Создание необходимых рабочих директорий..."
mkdir -p "$PROJECT_DIR/certs" "$PROJECT_DIR/logs" "$PROJECT_DIR/backups" "$PROJECT_DIR/certbot/conf" "$PROJECT_DIR/certbot/www" "$PROJECT_DIR/nginx/ssl"

# ─── 9. Установка прав на скрипты ───────────────────────────────────────────
chmod +x "$PROJECT_DIR"/scripts/*.sh 2>/dev/null || true
chmod +x "$PROJECT_DIR"/docker/entrypoint.sh 2>/dev/null || true

echo "================================================================="
echo "🎉 Сервер успешно подготовлен к запуску WB FBS Manager!"
echo "RAM + Swap:"
free -h
echo "================================================================="
echo "Следующие шаги:"
echo "1. Сгенерируйте секреты: python3 scripts/generate_secrets.py"
echo "2. Запустите проект: docker compose -f docker-compose.prod.yml up -d --build"
echo "3. Для настройки SSL домена запустите: ./scripts/init_ssl.sh your-domain.ru admin@your-domain.ru"
echo "================================================================="