#!/data/data/com.termux/files/usr/bin/env bash
# 媒体中心总控脚本（单文件）
# 用法：
#   bash verpadproxy.sh init [固定热点CIDR]
#   bash verpadproxy.sh start
#   bash verpadproxy.sh stop
#   bash verpadproxy.sh restart
#   bash verpadproxy.sh status
#   bash verpadproxy.sh info
#   bash verpadproxy.sh fixip [固定热点CIDR]
#   bash verpadproxy.sh guard-install [固定热点CIDR]
#   bash verpadproxy.sh guard-remove
#
# 示例：
#   bash verpadproxy.sh init 192.168.68.1/24

set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"

# 在 MT root 环境下强制切到 Termux 运行时，避免 PATH/HOME 缺失。
TERMUX_HOME="/data/data/com.termux/files/home"
TERMUX_PREFIX="/data/data/com.termux/files/usr"
export HOME="$TERMUX_HOME"
export PREFIX="$TERMUX_PREFIX"
export PATH="$TERMUX_PREFIX/bin:$TERMUX_PREFIX/bin/applets:$PATH"

. "$SELF_DIR/env.sh"

MC_ROOT="/sdcard/VerPadProxy"
LOG_FILE="$MC_ROOT/_mitmdump.log"
PID_FILE="$MC_ROOT/_mitmdump.pid"
GUARD_FILE="/data/adb/service.d/99-verpadproxy-hotspot-guard.sh"
DEFAULT_HOTSPOT_CIDR="192.168.68.1/24"
BASH_BIN="$TERMUX_PREFIX/bin/bash"
PY_BIN="$TERMUX_PREFIX/bin/python"
NOHUP_BIN="$TERMUX_PREFIX/bin/nohup"

_need_root() {
  if ! command -v su >/dev/null 2>&1; then
    echo "[错误] 未检测到 su，无法执行 root 操作。"
    exit 1
  fi
}

_is_running() {
  pgrep -af "mitmdump|mitmproxy.tools.main" >/dev/null 2>&1
}

_port_ok() {
  "$PY_BIN" - <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(1.2)
try:
    s.connect(("127.0.0.1", 2345))
    print("ok")
except Exception:
    print("fail")
finally:
    s.close()
PY
}

_detect_ap_ip() {
  su -c 'ip -4 -o addr show dev ap0 2>/dev/null | awk "{print \$4}" | head -n 1' | sed 's#/.*##' || true
}

_detect_wlan_ip() {
  su -c 'ip -4 -o addr show dev wlan0 2>/dev/null | awk "{print \$4}" | head -n 1' | sed 's#/.*##' || true
}

cmd_info() {
  _need_root
  local ap_ip wlan_ip
  ap_ip="$(_detect_ap_ip)"
  wlan_ip="$(_detect_wlan_ip)"

  echo "============================================================"
  echo " 媒体中心连接详情"
  echo " 服务端口: $MC_LISTEN_PORT"
  echo "============================================================"

  echo "[A] 安卓热点直连"
  if [ -n "$ap_ip" ]; then
    echo "  代理主机: $ap_ip"
    echo "  代理端口: $MC_LISTEN_PORT"
    echo "  地址格式: $ap_ip:$MC_LISTEN_PORT"
  else
    echo "  未检测到 ap0 IPv4（请先开启热点）"
  fi

  echo
  echo "[B] 局域网同 WiFi"
  if [ -n "$wlan_ip" ]; then
    echo "  代理主机: $wlan_ip"
    echo "  代理端口: $MC_LISTEN_PORT"
    echo "  地址格式: $wlan_ip:$MC_LISTEN_PORT"
  else
    echo "  未检测到 wlan0 IPv4（安卓设备可能未连上游 WiFi）"
  fi

  echo
  su -c 'ip route 2>/dev/null | awk "/^default/ {print \"上游默认网关:\", \$3, \"接口:\", \$5; exit}"' || true
  echo "============================================================"
}

cmd_start() {
  if _is_running; then
    echo "mitmdump 已在运行。"
    return 0
  fi
  termux-wake-lock >/dev/null 2>&1 || true
  : > "$LOG_FILE"
  if [ -x "$NOHUP_BIN" ]; then
    "$NOHUP_BIN" "$BASH_BIN" "$SELF_DIR/start.sh" > "$LOG_FILE" 2>&1 &
  elif command -v nohup >/dev/null 2>&1; then
    nohup "$BASH_BIN" "$SELF_DIR/start.sh" > "$LOG_FILE" 2>&1 &
  else
    "$BASH_BIN" "$SELF_DIR/start.sh" > "$LOG_FILE" 2>&1 &
  fi
  local pid=$!
  # 第 3 兜底：若首发命令立刻退出，再尝试 setsid 脱离控制终端
  sleep 1
  if ! kill -0 "$pid" 2>/dev/null; then
    setsid "$BASH_BIN" "$SELF_DIR/start.sh" > "$LOG_FILE" 2>&1 &
    pid=$!
  fi
  echo "$pid" > "$PID_FILE"
  sleep 4
  if _is_running; then
    echo "已启动，PID=$pid"
    echo "日志: $LOG_FILE"
  else
    echo "启动失败，日志如下："
    tail -n 80 "$LOG_FILE" 2>/dev/null || true
    exit 1
  fi
}

cmd_stop() {
  if _is_running; then
    pkill -f "mitmdump|mitmproxy.tools.main" || true
    sleep 1
  fi
  if _is_running; then
    pkill -9 -f "mitmdump|mitmproxy.tools.main" || true
  fi
  termux-wake-unlock >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
  if _is_running; then
    echo "停止失败，请手动检查。"
    exit 1
  fi
  echo "已停止。"
}

cmd_status() {
  echo "=== 进程状态 ==="
  if _is_running; then
    pgrep -af "mitmdump|mitmproxy.tools.main"
  else
    echo "mitmdump 未运行"
  fi

  echo
  echo "=== 端口连通性 ==="
  if [ "$(_port_ok)" = "ok" ]; then
    echo "本地 TCP 探测: 2345 可连通（代理已监听）"
  else
    echo "本地 TCP 探测: 2345 不可连通"
  fi

  echo
  echo "=== 连接详情 ==="
  cmd_info

  echo
  echo "=== 近期日志 ==="
  tail -n 40 "$LOG_FILE" 2>/dev/null || echo "日志不存在: $LOG_FILE"
}

cmd_logs() {
  local lines="${1:-80}"
  if [ -f "$LOG_FILE" ]; then
    tail -n "$lines" "$LOG_FILE"
  else
    echo "日志不存在: $LOG_FILE"
  fi
}

cmd_where() {
  cat <<EOF
关键文件位置：
- 总控脚本：$SELF_DIR/verpadproxy.sh
- 启动脚本：$SELF_DIR/start.sh
- 环境配置：$SELF_DIR/env.sh
- 后台日志：$LOG_FILE
- 后台 PID ：$PID_FILE
- 媒体目录：$MITM_SHARE_DIR
- 数据目录：$MITM_DATA_DIR
- 用户库  ：$MITM_USERS_FILE
- 证书目录：$MC_MITM_CONFDIR
- 热点守护：$GUARD_FILE
- Termux:Boot 自启：$HOME/.termux/boot/verpadproxy.sh
EOF
}

cmd_doctor() {
  echo "=== 健康检查 ==="
  echo "[1] 命令存在性"
  command -v mitmdump >/dev/null 2>&1 && echo "  mitmdump: OK" || echo "  mitmdump: 缺失"
  command -v su >/dev/null 2>&1 && echo "  su: OK" || echo "  su: 缺失"
  command -v "$PY_BIN" >/dev/null 2>&1 && echo "  python: OK" || echo "  python: 缺失"

  echo "[2] 版本"
  mitmdump --version 2>/dev/null | head -n 1 || echo "  mitmdump 版本读取失败"

  echo "[3] 服务状态"
  if _is_running; then
    echo "  进程: 运行中"
  else
    echo "  进程: 未运行"
  fi
  if [ "$(_port_ok)" = "ok" ]; then
    echo "  端口: 2345 可连通"
  else
    echo "  端口: 2345 不可连通"
  fi

  echo "[4] 网络信息"
  cmd_info
}

cmd_alias_install() {
  local wrapper="$PREFIX/bin/mc"
  cat > "$wrapper" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
exec bash "$SELF_DIR/verpadproxy.sh" "\$@"
EOF
  chmod 755 "$wrapper"
  echo "已安装快捷命令：mc"
  echo "现在可直接使用："
  echo "  mc status"
  echo "  mc info"
  echo "  mc restart"
}

cmd_fixip() {
  _need_root
  local cidr="${1:-$DEFAULT_HOTSPOT_CIDR}"
  local ip="${cidr%%/*}"
  echo "应用固定热点网关: $cidr"
  su -c "settings put global tether_dhcp_range ${ip%.*}.2,${ip%.*}.254" || true
  if ! su -c 'ip link show ap0 >/dev/null 2>&1'; then
    echo "ap0 不存在。请先开启热点后再执行 fixip。"
    exit 1
  fi
  su -c "ip addr flush dev ap0"
  su -c "ip addr add $cidr dev ap0"
  su -c "ip link set ap0 up"
  su -c 'ip -4 -o addr show dev ap0'
}

cmd_guard_install() {
  _need_root
  local cidr="${1:-$DEFAULT_HOTSPOT_CIDR}"
  local ip="${cidr%%/*}"
  su -c 'mkdir -p /data/adb/service.d'

  cat > "$MC_ROOT/.guard.tmp.sh" <<EOF
#!/system/bin/sh
TARGET_CIDR="$cidr"
TARGET_IP="$ip"
while true; do
  if ip link show ap0 >/dev/null 2>&1; then
    CUR="\$(ip -4 -o addr show dev ap0 2>/dev/null | awk '{print \$4}' | head -n 1)"
    if [ "\$CUR" != "\$TARGET_CIDR" ]; then
      ip addr flush dev ap0 >/dev/null 2>&1
      ip addr add "\$TARGET_CIDR" dev ap0 >/dev/null 2>&1
      ip link set ap0 up >/dev/null 2>&1
      settings put global tether_dhcp_range "\${TARGET_IP%.*}.2,\${TARGET_IP%.*}.254" >/dev/null 2>&1
      echo "[hotspot-guard] ap0 -> \$TARGET_CIDR" >> /sdcard/VerPadProxy/_hotspot_guard.log
    fi
  fi
  sleep 4
done
EOF

  su -c "cp '$MC_ROOT/.guard.tmp.sh' '$GUARD_FILE'"
  su -c "chmod 755 '$GUARD_FILE'"
  rm -f "$MC_ROOT/.guard.tmp.sh"
  echo "已安装热点守护: $GUARD_FILE"
  echo "目标固定网关: $cidr"
}

cmd_guard_remove() {
  _need_root
  su -c "rm -f '$GUARD_FILE'"
  echo "已卸载热点守护。"
}

cmd_root_start() {
  # root 身份启动（与 start_same_as_pc_root.sh 等价，内联到总控）
  _need_root
  local payload="$MITM_SHARE_DIR"
  local data_dir="${MITM_DATA_DIR:-$MC_ROOT}"
  local hosts="${MITM_REDIRECT_HOSTS:-example.com:8080}"
  local scripts_dir
  scripts_dir="$(cd "$SELF_DIR/.." && pwd)"
  su -c "pkill -9 -f mitmdump 2>/dev/null; sleep 1; \
         export HOME=$TERMUX_HOME; export PREFIX=$TERMUX_PREFIX; \
         export PATH=$TERMUX_PREFIX/bin:$TERMUX_PREFIX/bin/applets:\$PATH; \
         export MITM_REDIRECT_HOSTS='$hosts'; \
         export MITM_SHARE_DIR='$payload'; \
         export MITM_DATA_DIR='$data_dir'; \
         cd '$scripts_dir' && \
         nohup $TERMUX_PREFIX/bin/mitmdump -s ./redirect_addon.py \
           --listen-host 0.0.0.0 --listen-port ${MC_LISTEN_PORT:-2345} \
           >$LOG_FILE 2>&1 &"
  sleep 3
  if _is_running; then
    echo "已 root 后台启动 mitmdump。"
    pgrep -af "mitmdump|mitmproxy.tools.main" | head -n 1
  else
    echo "启动失败，最后 40 行日志："
    tail -n 40 "$LOG_FILE" 2>/dev/null
    return 1
  fi
}

cmd_clean() {
  local script_path="$SELF_DIR/cleanup.sh"
  if [ ! -f "$script_path" ]; then
    echo "找不到 $script_path，无法清理。"
    return 1
  fi
  _need_root
  su -c "sh '$script_path'"
}

cmd_init() {
  local cidr="${1:-$DEFAULT_HOTSPOT_CIDR}"
  chmod +x "$SELF_DIR/start.sh" || true
  termux-wake-lock >/dev/null 2>&1 || true
  echo "[1/4] 安装热点守护..."
  cmd_guard_install "$cidr"
  echo "[2/4] 尝试立即固定热点IP（需热点已开启）..."
  cmd_fixip "$cidr" || true
  echo "[3/4] 重启服务..."
  cmd_stop || true
  cmd_start
  echo "[4/4] 打印连接信息..."
  cmd_info
  echo "初始化完成。"
}

usage() {
  cat <<EOF
用法:
  bash verpadproxy.sh init [固定热点CIDR]
  bash verpadproxy.sh start
  bash verpadproxy.sh stop
  bash verpadproxy.sh restart
  bash verpadproxy.sh status
  bash verpadproxy.sh info
  bash verpadproxy.sh fixip [固定热点CIDR]
  bash verpadproxy.sh guard-install [固定热点CIDR]
  bash verpadproxy.sh guard-remove
  bash verpadproxy.sh logs [行数]
  bash verpadproxy.sh where
  bash verpadproxy.sh doctor
  bash verpadproxy.sh alias-install
  bash verpadproxy.sh root-start
  bash verpadproxy.sh clean

示例:
  bash verpadproxy.sh init 192.168.68.1/24
EOF
}

case "${1:-}" in
  init) shift; cmd_init "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop || true; cmd_start ;;
  status) cmd_status ;;
  info) cmd_info ;;
  fixip) shift; cmd_fixip "$@" ;;
  guard-install) shift; cmd_guard_install "$@" ;;
  guard-remove) cmd_guard_remove ;;
  logs) shift; cmd_logs "$@" ;;
  where) cmd_where ;;
  doctor) cmd_doctor ;;
  alias-install) cmd_alias_install ;;
  root-start) cmd_root_start ;;
  clean) cmd_clean ;;
  *) usage ;;
esac
