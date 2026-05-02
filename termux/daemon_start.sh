#!/data/data/com.termux/files/usr/bin/env bash
set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/sdcard/VerPadProxy/_mitmdump.log"
PID_FILE="/sdcard/VerPadProxy/_mitmdump.pid"

_is_running() {
  pgrep -af "mitmdump|mitmproxy.tools.main" >/dev/null 2>&1
}

if _is_running; then
  echo "mitmdump 已在运行。"
  pgrep -af "mitmdump|mitmproxy.tools.main" | head -n 1 | awk '{print $1}' > "$PID_FILE"
  exit 0
fi

termux-wake-lock >/dev/null 2>&1 || true
nohup bash "$SELF_DIR/start.sh" > "$LOG_FILE" 2>&1 &
MPID=$!
echo "$MPID" > "$PID_FILE"
sleep 4
if _is_running; then
  echo "已后台启动 mitmdump，PID=$MPID"
  echo "日志: $LOG_FILE"
else
  echo "启动失败，请查看日志: $LOG_FILE"
  tail -n 80 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi
