#!/bin/bash
set -e

# Function to wait for PostgreSQL
wait_for_postgres() {
    echo "⏳ Waiting for PostgreSQL to become available..."
    python - << 'EOF'
import sys
import time
import socket
import os
from urllib.parse import urlparse

db_url = os.environ.get("DATABASE_URL", "")
host = "postgres"
port = 5432

if db_url:
    try:
        # Handle custom url schemes like postgresql+asyncpg://
        clean_url = db_url.replace("postgresql+asyncpg://", "http://").replace("postgresql://", "http://")
        parsed = urlparse(clean_url)
        if parsed.hostname:
            host = parsed.hostname
        if parsed.port:
            port = parsed.port
    except Exception as e:
        print(f"Warning parsing DATABASE_URL: {e}")

start_time = time.time()
while time.time() - start_time < 60:
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        print(f"✅ PostgreSQL at {host}:{port} is reachable.")
        sys.exit(0)
    except Exception:
        time.sleep(1)

print(f"❌ Timeout waiting for PostgreSQL at {host}:{port}")
sys.exit(1)
EOF
}

# Function to wait for Redis
wait_for_redis() {
    echo "⏳ Waiting for Redis to become available..."
    python - << 'EOF'
import sys
import time
import socket
import os
from urllib.parse import urlparse

redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
host = "redis"
port = 6379

try:
    clean_url = redis_url.replace("redis://", "http://")
    parsed = urlparse(clean_url)
    if parsed.hostname:
        host = parsed.hostname
    if parsed.port:
        port = parsed.port
except Exception as e:
    print(f"Warning parsing REDIS_URL: {e}")

start_time = time.time()
while time.time() - start_time < 60:
    try:
        s = socket.create_connection((host, port), timeout=2)
        s.close()
        print(f"✅ Redis at {host}:{port} is reachable.")
        sys.exit(0)
    except Exception:
        time.sleep(1)

print(f"❌ Timeout waiting for Redis at {host}:{port}")
sys.exit(1)
EOF
}

# Wait for DB and Redis if configured
if [[ -n "$DATABASE_URL" ]] && [[ "$DATABASE_URL" != *"sqlite"* ]]; then
    wait_for_postgres
fi

if [[ -n "$REDIS_URL" ]]; then
    wait_for_redis
fi

# If starting API, initialize database schema
if [[ "$1" == *"uvicorn"* ]] || [[ "$*" == *"app.main:app"* ]]; then
    echo "📦 Initializing database schema..."
    python -c "import asyncio; from app.database import init_db; asyncio.run(init_db())" || echo "⚠️ init_db warning"
    echo "📦 Running alembic migrations..."
    alembic upgrade head || true
fi

# Execute passed command
exec "$@"