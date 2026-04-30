#!/data/data/com.termux/files/usr/bin/env bash
set -e
PID_FILE="/sdcard/VerPadProxy/_mitmdump.pid"

if pgrep -af "mitmdump|mitmproxy.tools.main" >/dev/null 2>&1; then
  pkill -f "mitmdump|mitmproxy.tools.main" || true
  sleep 1
fi

if pgrep -af "mitmdump|mitmproxy.tools.main" >/dev/null 2>&1; then
  echo "普通停止失败，尝试强制停止..."
  pkill -9 -f "mitmdump|mitmproxy.tools.main" || true
fi

termux-wake-unlock >/dev/null 2>&1 || true
rm -f "$PID_FILE"

if pgrep -af "mitmdump|mitmproxy.tools.main" >/dev/null 2>&1; then
  echo "停止失败，请手动检查。"
  exit 1
fi

echo "mitmdump 已停止。"
