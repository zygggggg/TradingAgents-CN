#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/tradingagent-cn}"
PYTHON_BIN="${PYTHON_BIN:-$PROJECT_DIR/.venv/bin/python}"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
ENV_FILE="${ENV_FILE:-$PROJECT_DIR/.stock-monitor.env}"
mkdir -p "$LOG_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
  else
    echo "python not found" >&2
    exit 1
  fi
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "warning: env file not found: $ENV_FILE" >&2
fi

CRON_FILE="/etc/cron.d/stock-monitor"
cat > "$CRON_FILE" <<EOF
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# A股盘中，每小时检查一次关键价位；脚本自身也会判断交易时段。
30 9-11,13-14 * * 1-5 root cd $PROJECT_DIR && set -a && source $ENV_FILE && set +a && $PYTHON_BIN scripts/monitor_watchlist.py --notify >> $LOG_DIR/stock_monitor.log 2>&1

# 午盘/收盘心跳，不触发交易线也会发一次状态（可按需注释）。
35 11 * * 1-5 root cd $PROJECT_DIR && set -a && source $ENV_FILE && set +a && $PYTHON_BIN scripts/monitor_watchlist.py --force --notify --repeat-heartbeat >> $LOG_DIR/stock_monitor.log 2>&1
10 15 * * 1-5 root cd $PROJECT_DIR && set -a && source $ENV_FILE && set +a && $PYTHON_BIN scripts/monitor_watchlist.py --force --notify --repeat-heartbeat >> $LOG_DIR/stock_monitor.log 2>&1
EOF
chmod 644 "$CRON_FILE"

if command -v systemctl >/dev/null 2>&1; then
  systemctl reload cron 2>/dev/null || systemctl reload crond 2>/dev/null || true
fi

echo "Installed cron: $CRON_FILE"
echo "Schedule: 09:30, 10:30, 11:30, 13:30, 14:30 + 11:35/15:10 heartbeat"
echo "Log: $LOG_DIR/stock_monitor.log"
