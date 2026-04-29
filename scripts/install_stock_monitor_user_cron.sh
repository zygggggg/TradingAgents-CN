#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/tradingagent-cn}"
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
  echo "env file not found: $ENV_FILE" >&2
  exit 1
fi

TMP_EXISTING="$(mktemp)"
TMP_CRON="$(mktemp)"
crontab -l 2>/dev/null > "$TMP_EXISTING" || true
awk '
  /# BEGIN TradingAgents-CN A股监控/ {skip=1; next}
  /# END TradingAgents-CN A股监控/ {skip=0; next}
  skip == 0 {print}
' "$TMP_EXISTING" > "$TMP_CRON"
cat >> "$TMP_CRON" <<EOF
# BEGIN TradingAgents-CN A股监控
SHELL=/bin/bash

# 盘中每小时检查关键价位：09:30、10:30、11:30、13:30、14:30。
30 9-11,13-14 * * 1-5 cd $PROJECT_DIR && set -a && source $ENV_FILE && set +a && $PYTHON_BIN scripts/monitor_watchlist.py --notify --ai-on-trigger >> $LOG_DIR/stock_monitor.log 2>&1

# 午盘/收盘心跳：不触发关键线也发一次状态。
35 11 * * 1-5 cd $PROJECT_DIR && set -a && source $ENV_FILE && set +a && $PYTHON_BIN scripts/monitor_watchlist.py --force --notify --repeat-heartbeat >> $LOG_DIR/stock_monitor.log 2>&1
10 15 * * 1-5 cd $PROJECT_DIR && set -a && source $ENV_FILE && set +a && $PYTHON_BIN scripts/monitor_watchlist.py --force --notify --repeat-heartbeat >> $LOG_DIR/stock_monitor.log 2>&1
# END TradingAgents-CN A股监控
EOF
crontab "$TMP_CRON"
rm -f "$TMP_EXISTING" "$TMP_CRON"

echo "Installed user crontab for A-share monitor."
echo "Schedule: 09:30, 10:30, 11:30, 13:30, 14:30 + 11:35/15:10 heartbeat"
echo "Log: $LOG_DIR/stock_monitor.log"
