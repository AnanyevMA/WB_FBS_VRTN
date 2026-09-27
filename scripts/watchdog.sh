#!/bin/bash
# =============================================================================
# WB FBS Manager — Watchdog & Proactive Memory Guard for 1GB VPS
# Checks container memory percentages and host load.
# Performs graceful restarts before the Linux kernel cgroup OOM-killer fires.
#
# Runs via cron: */15 * * * * /PROJECTS/WB_FBS_VRTN/wb-fbs/scripts/watchdog.sh >> /PROJECTS/WB_FBS_VRTN/wb-fbs/logs/watchdog.log 2>&1
# =============================================================================

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.prod.yml"
THRESHOLD=96

# Ensure logs dir exists
mkdir -p "$PROJECT_DIR/logs"

timestamp() {
    date '+%Y-%m-%d %H:%M:%S'
}

check_container_mem() {
    local container="$1"
    local service="$2"

    # Verify container is actually running
    if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^${container}$"; then
        return 0
    fi

    local mem_perc_raw
    mem_perc_raw=$(docker stats --no-stream --format "{{.MemPerc}}" "$container" 2>/dev/null | tr -d '%' | tr -d '[:space:]')

    if [ -n "$mem_perc_raw" ]; then
        # Extract integer part (e.g. 93.4 -> 93)
        local mem_perc=${mem_perc_raw%%.*}
        if [ -n "$mem_perc" ] && [ "$mem_perc" -ge "$THRESHOLD" ]; then
            echo "[$(timestamp)] ⚠️ [WATCHDOG] Container ${container} memory reached ${mem_perc}% (threshold: ${THRESHOLD}%)."
            echo "[$(timestamp)] 🔄 [WATCHDOG] Gracefully restarting service ${service} to prevent cgroup OOM kill..."
            cd "$PROJECT_DIR" && docker compose -f "$COMPOSE_FILE" restart "$service"
            echo "[$(timestamp)] ✅ [WATCHDOG] Service ${service} restarted cleanly."
        fi
    fi
}

# 1. Proactively monitor wbfbs_bot (cgroup limit 135M)
check_container_mem "wbfbs_bot" "bot"

# 2. Proactively monitor wbfbs_scheduler (cgroup limit 165M)
check_container_mem "wbfbs_scheduler" "scheduler"

# 3. Check for crashed or restarting containers
UNHEALTHY=$(docker ps -a --filter "name=wbfbs_" --filter "status=restarting" --filter "status=dead" --format "{{.Names}}: {{.Status}}" 2>/dev/null || true)
if [ -n "$UNHEALTHY" ]; then
    echo "[$(timestamp)] 🚨 [WATCHDOG ALERT] Unhealthy container state detected: $UNHEALTHY"
fi
