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
if "@" in db_url:
    netloc = db_url.split("@")[1].split("/")[0]
    if ":" in netloc:
        host, port = netloc.split(":")
        port = int(port)
    else:
        host, port = netloc, 5432
else:
    host, port = "postgres", 5432

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
parsed = urlparse(redis_url)
host = parsed.hostname or "redis"
port = parsed.port or 6379

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

# Wait for DB and Redis if they are configured
if [[ -n "$DATABASE_URL" ]] && [[ "$DATABASE_URL" != *"sqlite"* ]]; then
    wait_for_postgres
fi

if [[ -n "$REDIS_URL" ]]; then
    wait_for_redis
fi

# If starting API, run database migrations automatically
if [[ "$1" == *"uvicorn"* ]] || [[ "$*" == *"app.main:app"* ]]; then
    echo "📦 Running database migrations (alembic upgrade head)..."
    alembic upgrade head || echo "⚠️ Alembic migration returned non-zero exit code (schema might already be initialized)."
fi

# Execute passed command
exec "$@"