# 🚀 Руководство по развертыванию и обновлению WB FBS Manager на VPS

Полная пошаговая инструкция по публикации на **GitHub**, первоначальной настройке и **безопасному обновлению** на VPS-сервере:
**1 CPU / 1 GB RAM / 30 GB SSD (Ubuntu 22.04 LTS / 24.04 LTS)**.

---

## 📋 Содержание
1. [Архитектура оптимизации под 1 GB RAM и защита от OOM](#1-архитектура-оптимизации-под-1-gb-ram-и-защита-от-oom)
2. [Шаг 1: Публикация проекта на GitHub](#шаг-1-публикация-проекта-на-github)
3. [Шаг 2: Первоначальная настройка VPS](#шаг-2-первоначальная-настройка-vps)
4. [Шаг 3: Настройка секретов и .env](#шаг-3-настройка-секретов-и-env)
5. [Шаг 4: Запуск проекта в Production](#шаг-4-запуск-проекта-в-production)
6. [Шаг 5: Настройка домена и SSL (Let's Encrypt)](#шаг-5-настройка-домена-и-ssl-lets-encrypt)
7. [Шаг 6: Автозапуск при перезагрузке ОС (Systemd)](#шаг-6-автозапуск-при-перезагрузке-ос-systemd)
8. [Шаг 7: Автоматическое резервное копирование (Cron)](#шаг-7-автоматическое-резервное-копирование-cron)
9. [Шаг 8: Правила и регламент обновления проекта (Zero-Downtime Deploy)](#шаг-8-правила-и-регламент-обновления-проекта-zero-downtime-deploy)
10. [Шаг 9: Настройка КриптоПро и сертификатов УКЭП](#шаг-9-настройка-криптопро-и-сертификатов-укэп)
11. [Диагностика, управление паролями и полезные команды](#диагностика-управление-паролями-и-полезные-команды)

---

## 1. Архитектура оптимизации под 1 GB RAM и защита от OOM

На сервере с 1 ГБ оперативной памяти одновременный запуск микросервисов может вызвать нехватку памяти (Linux OOM Killer). В проекте реализована многоуровневая система защиты:

- **Swap 2 GB (Обязательно)**: Автоматически создается и подключается скриптами `setup_vps.sh` и `deploy.sh` с параметром `swappiness=10`. Предотвращает аварийное завершение Celery и PostgreSQL при пиках нагрузки.
- **PostgreSQL 16**: Ограничен буфер памяти (`shared_buffers=64MB`, `max_connections=30`, cgroup limit 180MB).
- **Redis 7**: Ограничен лимит памяти до 64 MB с политикой вытеснения `allkeys-lru`.
- **FastAPI (Uvicorn)**: 1 асинхронный воркер с лимитом соединений (cgroup limit 280MB).
- **Celery Worker**: Запускается с `--concurrency=1`, `--max-tasks-per-child=50` и `--max-memory-per-child=120000` (120 МБ на дочерний процесс для предотвращения утечек памяти, cgroup limit 300MB).
- **Nginx**: 
  - Смонтированы оба конфигурационных файла: `nginx.conf` (зоны rate-limiting) и `app.conf`.
  - Настроен динамический резолвинг DNS (`resolver 127.0.0.11 valid=10s ipv6=off;` и переменная `$upstream_api`). При пересборке API-контейнера Nginx автоматически переопределяет IP за 10 секунд и никогда не выдает ошибку 502 Bad Gateway.
- **Docker Logs**: Ограничены до 10 МБ на файл (максимум 3 файла), защищая диск 30 ГБ от переполнения.

---

## Шаг 1: Публикация проекта на GitHub

На локальном компьютере:

```bash
cd "d:/PyCharm_Projects/WB FBS"
git add .
git commit -m "feat: updates and robust deployment setup"
git push origin main
```

---

## Шаг 2: Первоначальная настройка VPS

Подключитесь к VPS по SSH:

```bash
ssh root@IP_ВАШЕГО_СЕРВЕРА
```

Установите `git` и клонируйте проект в каталог `/PROJECTS/WB_FBS_VRTN/wb-fbs`:

```bash
mkdir -p /PROJECTS/WB_FBS_VRTN
cd /PROJECTS/WB_FBS_VRTN
git clone https://github.com/AnanyevMA/WB_FBS_VRTN.git wb-fbs
cd /PROJECTS/WB_FBS_VRTN/wb-fbs
```

Запустите скрипт первоначальной настройки:

```bash
chmod +x scripts/*.sh docker/entrypoint.sh
./scripts/setup_vps.sh
```

---

## Шаг 3: Настройка секретов и .env

Сгенерируйте файл `.env` (если создается впервые):

```bash
python3 scripts/generate_secrets.py
```

Отредактируйте `.env` при необходимости:

```bash
nano .env
```

Параметры администратора:
- `ADMIN_USERNAME=admin`
- `ADMIN_PASSWORD=ваш_надежный_пароль`
- `ADMIN_EMAIL=admin@example.com`

---

## Шаг 4: Запуск проекта в Production

Соберите и запустите сервисы:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Проверьте статус всех контейнеров:

```bash
docker compose -f docker-compose.prod.yml ps
```

Все контейнеры должны быть в статусе `Up` / `Up (healthy)`.

---

## Шаг 5: Настройка домена и SSL (Let's Encrypt)

1. В панели управления вашим доменом создайте **A-запись** с IP вашего VPS.
2. Запустите скрипт выпуска SSL:

```bash
./scripts/init_ssl.sh yourdomain.ru admin@yourdomain.ru
```

---

## Шаг 6: Автозапуск при перезагрузке ОС (Systemd)

```bash
cp systemd/wb-fbs.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable wb-fbs.service
```

---

## Шаг 7: Автоматическое резервное копирование (Cron)

Настройте ежедневный бэкап БД в 04:00:

```bash
crontab -e
```

Добавьте строку:
```cron
0 4 * * * /PROJECTS/WB_FBS_VRTN/wb-fbs/scripts/backup_db.sh >> /PROJECTS/WB_FBS_VRTN/wb-fbs/logs/backup.log 2>&1
```

---

## Шаг 8: Правила и регламент обновления проекта (Zero-Downtime Deploy)

> [!IMPORTANT]
> **Главное правило**: Никогда не используйте команду `docker compose down -v` на боевом сервере! Это приведет к уничтожению томов базы данных PostgreSQL.

### Стандартная команда обновления:

Для обновления проекта до последней версии с GitHub выполните:

```bash
cd /PROJECTS/WB_FBS_VRTN/wb-fbs && git pull origin main && bash scripts/deploy.sh
```

### Что автоматически делает `scripts/deploy.sh`:
1. **Проверяет и активирует 2 ГБ Swap** (защита от нехватки RAM).
2. **Сохраняет конфигурацию `.env`** и учетные данные.
3. **Скачивает обновления из Git** (`git pull origin main`).
4. **Пересобирает и перезапускает все контейнеры** (`--force-recreate` обновляет Nginx и сетевые маршруты).
5. **Ожидает готовности API** (активный опрос `/health` до 60 секунд).
6. **Проверяет авторизацию администратора** с автоматической синхронизацией пароля.
7. **Проверяет ответ веб-сервера Nginx на порту 80**.
8. **Очищает старые слои Docker** для экономии места на диске.

---

## Шаг 9: Настройка КриптоПро и сертификатов УКЭП

1. Скопируйте файлы сертификатов (`.cer`, `.pfx` или папки ключей) в каталог `/PROJECTS/WB_FBS_VRTN/wb-fbs/certs/`.
2. В файле `.env` укажите отпечаток:
   ```env
   CRYPTOPRO_CERT_THUMBPRINT=ваш_отпечаток_сертификата
   ```
3. Перезапустите контейнеры:
   ```bash
   docker compose -f docker-compose.prod.yml restart worker api
   ```

---

## Диагностика, управление паролями и полезные команды

### Смена или сброс пароля администратора:
Скрипт работает без внешних зависимостей и автоматически обновляет и PostgreSQL, и `.env`:
```bash
python3 scripts/set_admin_password.py --password "НовыйПароль123"
```

### Просмотр логов сервисов:
```bash
# Все логи
docker compose -f docker-compose.prod.yml logs -f

# Логи API
docker compose -f docker-compose.prod.yml logs --tail=50 -f api

# Логи Nginx
docker compose -f docker-compose.prod.yml logs --tail=50 -f nginx

# Логи воркеров Celery
docker compose -f docker-compose.prod.yml logs --tail=50 -f worker
```

### Проверка памяти и диска:
```bash
./scripts/check_memory.sh
```

### Вход в базу данных PostgreSQL:
```bash
docker compose -f docker-compose.prod.yml exec postgres psql -U wbfbs -d wbfbs
```