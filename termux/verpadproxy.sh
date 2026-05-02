#!/data/data/com.termux/files/usr/bin/env bash
# VerPadProxy总控脚本（单文件）
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

_mitm_pids() {
  # 关键：必须用 root 跑 ss，否则普通用户看不到 root 拥有的 socket 的 PID。
  # 没看到 PID 时再用 fuser / lsof / pgrep 三重兜底。
  local port="${MC_LISTEN_PORT:-2345}" pids=""

  pids="$(_run_root "ss -lntp 2>/dev/null" 2>/dev/null \
    | awk -v p=":${port}" '$0 ~ p && /pid=/{
        match($0, /pid=[0-9]+/); s=substr($0,RSTART,RLENGTH); sub(/pid=/, "", s); print s
      }' | sort -u)"

  if [ -z "$pids" ] && command -v fuser >/dev/null 2>&1; then
    pids="$(_run_root "fuser -n tcp ${port} 2>/dev/null" 2>/dev/null \
      | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u)"
  fi

  if [ -z "$pids" ] && command -v lsof >/dev/null 2>&1; then
    pids="$(_run_root "lsof -ti tcp:${port} 2>/dev/null" 2>/dev/null \
      | grep -E '^[0-9]+$' | sort -u)"
  fi

  # 最终兜底：按命令名找（这是上次切歌 bug 修复时刻意避开的，但只在前面都没结果时才用）。
  if [ -z "$pids" ]; then
    pids="$(_run_root "pgrep -f '/mitmdump|mitmdump\\b' 2>/dev/null" 2>/dev/null \
      | grep -E '^[0-9]+$' | sort -u)"
  fi

  echo "$pids"
}

_is_running() {
  local p
  p="$(_mitm_pids 2>/dev/null)"
  [ -n "$p" ]
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

_run_root() {
  # 已 root 直接跑，否则套 su -c。避免在 su 上下文里再嵌套 su。
  if [ "$(id -u 2>/dev/null)" = "0" ]; then
    sh -c "$1"
  else
    su -c "$1"
  fi
}

_detect_ap_ip() {
  _run_root 'ip -4 -o addr show dev ap0 2>/dev/null | awk "{print \$4}" | head -n 1' | sed 's#/.*##' || true
}

_detect_wlan_ip() {
  _run_root 'ip -4 -o addr show dev wlan0 2>/dev/null | awk "{print \$4}" | head -n 1' | sed 's#/.*##' || true
}

cmd_info() {
  _need_root
  local ap_ip wlan_ip ap_cidr ap_link wlan_link
  ap_ip="$(_detect_ap_ip)"
  wlan_ip="$(_detect_wlan_ip)"
  ap_cidr="$(_run_root 'ip -4 -o addr show dev ap0 2>/dev/null | awk "{print \$4}" | head -n 1' 2>/dev/null)"
  ap_link="$(_run_root 'ip link show dev ap0 2>/dev/null' 2>/dev/null)"
  wlan_link="$(_run_root 'ip link show dev wlan0 2>/dev/null' 2>/dev/null)"

  echo "============================================================"
  echo " VerPadProxy 连接详情"
  echo " 服务端口: $MC_LISTEN_PORT"
  echo "============================================================"

  # ---------------- [A] 本机热点 ----------------
  if [ -n "$ap_ip" ]; then
    echo "[A] 本机热点 (ap0)  ✅ 已开启"
    echo "    Pad 代理: $ap_ip:$MC_LISTEN_PORT"
    echo "    网段    : $ap_cidr"
    local dhcp
    dhcp="$(_run_root 'settings get global tether_dhcp_range 2>/dev/null' 2>/dev/null)"
    if [ -n "$dhcp" ] && [ "$dhcp" != "null" ]; then
      echo "    DHCP    : $dhcp（系统 settings 强制值）"
    fi
    local neigh
    neigh="$(_run_root 'ip neigh show dev ap0 2>/dev/null' 2>/dev/null \
              | awk '/(REACHABLE|STALE|DELAY|PROBE) /{print $1}' | grep -v "^fe80::" | head -n 4)"
    if [ -n "$neigh" ]; then
      echo "    已连接Pad客户端:"
      echo "$neigh" | sed 's/^/        /'
      local prefix conflict=""
      prefix="$(echo "$ap_ip" | cut -d. -f1-3)"
      while IFS= read -r line; do
        [ -z "$line" ] && continue
        local lp
        lp="$(echo "$line" | cut -d. -f1-3)"
        if [ "$lp" != "$prefix" ]; then conflict="1"; fi
      done <<EOF
$neigh
EOF
      if [ -n "$conflict" ]; then
        echo "    [!] 客户端 IP 与 ap0 不同网段 → 填 $ap_ip 可能连不上"
        echo "        建议: bash verpadproxy.sh unguard  然后关一次热点再开"
      fi
    fi
  elif [ -n "$ap_link" ]; then
    echo "[A] 本机热点 (ap0)  ⚠ 接口存在但无 IPv4（系统正在重建中？稍候再试）"
  else
    echo "[A] 本机热点 (ap0)  ❌ 已关闭 / 未启用"
  fi

  echo
  # ---------------- [B] 同上游 WiFi ----------------
  if [ -n "$wlan_ip" ]; then
    echo "[B] 同 WiFi 直连 (wlan0)  ✅ 已连接"
    echo "    Pad 代理: $wlan_ip:$MC_LISTEN_PORT"
  elif [ -n "$wlan_link" ]; then
    echo "[B] 同 WiFi 直连 (wlan0)  ⚠ 接口存在但无 IPv4（未关联上游 AP？）"
  else
    echo "[B] 同 WiFi 直连 (wlan0)  ❌ 已关闭 / 未启用"
  fi

  echo
  # ---------------- 附加信息 ----------------
  _run_root 'ip route 2>/dev/null | awk "/^default/ {print \"上游默认网关:\", \$3, \"接口:\", \$5; exit}"' 2>/dev/null
  if [ -f "$GUARD_FILE" ]; then
    echo
    echo "[提醒] 已装热点守护 ($GUARD_FILE)，强制 ap0 = $DEFAULT_HOTSPOT_CIDR"
    echo "       异常时执行: bash verpadproxy.sh unguard"
  fi
  echo "============================================================"
}

cmd_unguard() {
  _need_root
  echo "[1/3] 移除热点守护..."
  su -c "rm -f '$GUARD_FILE'"
  pkill -9 -f "99-verpadproxy-hotspot-guard" 2>/dev/null || true
  echo "[2/3] 还原系统 DHCP 默认 range..."
  su -c "settings delete global tether_dhcp_range" 2>/dev/null || true
  echo "[3/3] 完成。请关一次热点再打开（让 Android 重建 ap0 + 用系统默认 IP）。"
  echo "      之后 'bash verpadproxy.sh info' 看到的就是真实热点 IP。"
}

_kill_old_mitmdump() {
  local pids
  pids="$(_mitm_pids 2>/dev/null)"
  [ -z "$pids" ] && return 0
  for pid in $pids; do
    _run_root "kill -9 $pid 2>/dev/null" || true
  done
}

_wait_port_free() {
  # 端口空闲检查：最多等 N 秒，期间每秒重试。返回 0 = 空闲，1 = 仍占
  local port="${MC_LISTEN_PORT:-2345}" max="${1:-6}" i=0
  while [ $i -lt $max ]; do
    # 端口连得通 = 还在监听 = 仍占用
    if [ "$(_port_ok 2>/dev/null)" != "ok" ]; then
      return 0
    fi
    sleep 1
    i=$((i+1))
  done
  return 1
}

cmd_run() {
  # 一键：杀旧 + 起新 + 显示 IP
  echo "[1/3] 终止旧 mitmdump（如有）..."
  _kill_old_mitmdump
  if ! _wait_port_free 4; then
    echo "  端口 ${MC_LISTEN_PORT:-2345} 仍被占用，再杀一次..."
    _kill_old_mitmdump
    sleep 2
    if ! _wait_port_free 4; then
      echo "  仍未释放！可能存在异常进程，请手动："
      echo "    su -c 'ss -lntp | grep ${MC_LISTEN_PORT:-2345}'"
      echo "    su -c 'fuser -k -n tcp ${MC_LISTEN_PORT:-2345}'"
      return 1
    fi
  fi
  echo "[2/3] 启动 mitmdump (root)..."
  if [ -f /sdcard/VerPadProxy/start_same_as_pc_root.sh ]; then
    _run_root 'sh /sdcard/VerPadProxy/start_same_as_pc_root.sh' >/dev/null 2>&1 || true
  else
    cmd_start
  fi
  sleep 2
  # 用端口探测代替进程扫描（更快、不会卡）
  if [ "$(_port_ok 2>/dev/null)" = "ok" ]; then
    echo "  ✓ 已启动，端口 $MC_LISTEN_PORT 监听中"
  else
    echo "  ✗ 启动失败，看日志: $LOG_FILE"
    tail -n 20 "$LOG_FILE" 2>/dev/null
    return 1
  fi
  echo "[3/3] 连接信息:"
  cmd_info
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
  _kill_old_mitmdump
  sleep 1
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
  local pids
  pids="$(_mitm_pids 2>/dev/null)"
  if [ -n "$pids" ]; then
    for pid in $pids; do
      [ -r "/proc/$pid/cmdline" ] && echo "  PID $pid: $(tr '\0' ' ' < /proc/$pid/cmdline)"
    done
  else
    echo "  mitmdump 未运行"
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
  bash verpadproxy.sh up                     # 一键：杀旧 + 起新 + 显示 IP（推荐）
  bash verpadproxy.sh info                   # 查看 IP/连接信息
  bash verpadproxy.sh status                 # 查看进程/端口/日志
  bash verpadproxy.sh restart                # 仅重启服务，不显示 IP
  bash verpadproxy.sh stop                   # 停止服务
  bash verpadproxy.sh logs [行数]            # 查看后台日志
  bash verpadproxy.sh unguard                # 移除热点守护，恢复系统默认热点 IP（推荐用于 IP 异常时）
  bash verpadproxy.sh init [固定热点CIDR]    # 初始化：装守护 + 修 IP + 起服务
  bash verpadproxy.sh fixip [固定热点CIDR]   # 立即修一次 ap0 IP
  bash verpadproxy.sh guard-install [CIDR]   # 安装热点守护（开机自动锁定 ap0 IP）
  bash verpadproxy.sh guard-remove           # 卸载热点守护
  bash verpadproxy.sh clean                  # 清理临时调试脚本与日志
  bash verpadproxy.sh where                  # 列关键路径
  bash verpadproxy.sh doctor                 # 健康检查
  bash verpadproxy.sh alias-install          # 装快捷命令 mc
  bash verpadproxy.sh root-start             # 通过 su 直接启动
  bash verpadproxy.sh start                  # 普通后台启动
EOF
}

case "${1:-}" in
  up|run) cmd_run ;;
  init) shift; cmd_init "$@" ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_stop || true; cmd_start ;;
  status) cmd_status ;;
  info) cmd_info ;;
  unguard) cmd_unguard ;;
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
