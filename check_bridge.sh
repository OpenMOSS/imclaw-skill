#!/bin/bash
# Bridge 检活脚本 — cron 兜底守护
#
# 用法: 添加到 crontab，每分钟运行一次
#   * * * * * /path/to/imclaw-skill/check_bridge.sh
#
# 检查逻辑:
#   1. bridge.state 是否为 enabled（或不存在，默认 enabled）
#   2. bridge.pid 是否存在且进程存活
#   3. 如果应该运行但没运行，启动 bridge_wrapper.py

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SKILL_DIR/bridge.state"
PID_FILE="$SKILL_DIR/bridge.pid"
LOG_FILE="$SKILL_DIR/bridge.log"
WATCHDOG_LOG="$SKILL_DIR/bridge_watchdog.log"
VENV_PYTHON="$SKILL_DIR/venv/bin/python3"

# 检查 state file
if [ -f "$STATE_FILE" ]; then
    STATE=$(cat "$STATE_FILE" | tr -d '[:space:]')
    if [ "$STATE" = "disabled" ]; then
        exit 0
    fi
fi

# 检查 PID 文件 + 进程存活
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" | tr -d '[:space:]')
    if kill -0 "$PID" 2>/dev/null; then
        exit 0
    fi
fi

# 也检查 wrapper 进程是否存活
if pgrep -f "bridge_wrapper.py" > /dev/null 2>&1; then
    exit 0
fi

# 进程不存在，启动 wrapper
echo "$(date '+%Y-%m-%d %H:%M:%S') Bridge 未运行，自动启动 wrapper" >> "$WATCHDOG_LOG"
cd "$SKILL_DIR"
nohup "$VENV_PYTHON" bridge_wrapper.py >> "$LOG_FILE" 2>&1 &
