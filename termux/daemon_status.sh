#!/data/data/com.termux/files/usr/bin/env bash
set -e
LOG_FILE="/sdcard/VerPadProxy/_mitmdump.log"

echo "=== 进程状态 ==="
if pgrep -af "mitmdump|mitmproxy.tools.main" >/dev/null 2>&1; then
  pgrep -af "mitmdump|mitmproxy.tools.main"
else
  echo "mitmdump 未运行"
fi

echo
echo "=== 端口监听 (2345) ==="
if command -v ss >/dev/null 2>&1; then
  ss -lnt 2>/dev/null | grep ':2345' || true
fi
python - <<'PY'
import socket
s = socket.socket()
s.settimeout(1.2)
try:
    s.connect(("127.0.0.1", 2345))
    print("本地 TCP 探测: 2345 可连通（代理已监听）")
except Exception as e:
    print(f"本地 TCP 探测: 2345 不可连通（{e}）")
finally:
    s.close()
PY

echo
echo "=== 近期日志 ==="
if [ -f "$LOG_FILE" ]; then
  tail -n 40 "$LOG_FILE"
else
  echo "日志不存在: $LOG_FILE"
fi
