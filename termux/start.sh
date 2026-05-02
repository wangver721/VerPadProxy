#!/data/data/com.termux/files/usr/bin/env bash
# -*- coding: utf-8 -*-
# VerPadProxy·Termux 启动脚本
# 热点开好后执行本脚本；客户端把 WiFi 代理指向「热点 IP:2345」。

set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SELF_DIR/env.sh"

_detect_ip_lines() {
  # Android 13 上部分设备热点接口名称不是 ap0，这里做多策略探测。
  local all_lines
  all_lines="$(ip -o -4 addr show up scope global 2>/dev/null | awk '{print $2" "$4}')"
  if [ -z "$all_lines" ]; then
    return 0
  fi

  local hot_lines other_lines
  hot_lines="$(echo "$all_lines" | grep -E '^(ap|swlan|wlan1|wlan2|rndis|usb|bond|softap)' || true)"
  other_lines="$(echo "$all_lines" | grep -Ev '^(lo|dummy|tun|vti|ifb)' || true)"

  if [ -n "$hot_lines" ]; then
    echo "$hot_lines" | awk '{print $1": "$2}'
    return 0
  fi
  if [ -n "$other_lines" ]; then
    echo "$other_lines" | awk '{print $1": "$2}'
    return 0
  fi
}

_detect_gateway_hint() {
  local line iface gw
  line="$(ip route 2>/dev/null | awk '/^default/ {print; exit}')"
  if [ -z "$line" ]; then
    return 0
  fi
  iface="$(echo "$line" | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
  gw="$(echo "$line" | awk '{for (i=1;i<=NF;i++) if ($i=="via") {print $(i+1); exit}}')"
  if [ -n "$iface" ] || [ -n "$gw" ]; then
    echo "默认路由: ${iface:-?} ${gw:+(via $gw)}"
  fi
}

_detect_ip_from_getprop() {
  # 某些 ROM 在热点模式下 ip 命令不给全量信息，补一个 getprop 兜底。
  getprop 2>/dev/null \
    | grep -E 'dhcp\..*\.ipaddress' \
    | sed -n 's/^\[\(.*\)\]: \[\(.*\)\]$/\1 \2/p' \
    | awk '$2 ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/ {print $1": "$2}'
}

# 1) 防熄屏/杀后台
termux-wake-lock >/dev/null 2>&1 || true

# 2) 目录检查
if [ ! -d "$MC_SCRIPTS" ]; then
  echo "[错误] 代码目录不存在：$MC_SCRIPTS" >&2
  echo "       请先把 redirect_addon.py 等放进去，或改 env.sh 里的 MC_SCRIPTS。" >&2
  exit 1
fi
if [ ! -f "$MC_SCRIPTS/redirect_addon.py" ]; then
  echo "[错误] 未找到 $MC_SCRIPTS/redirect_addon.py" >&2
  exit 1
fi
mkdir -p "$MITM_SHARE_DIR"/{PDF,视频,音乐,private,upl} "$MITM_DATA_DIR"

# 3) 显示网络信息
IP_LINES="$(_detect_ip_lines | head -n 8)"
if [ -z "$IP_LINES" ]; then
  IP_LINES="$(_detect_ip_from_getprop | head -n 8)"
fi
GW_HINT="$(_detect_gateway_hint)"
echo "============================================================"
echo " VerPadProxy启动中…"
echo "   代码目录      : $MC_SCRIPTS"
echo "   媒体根        : $MITM_SHARE_DIR"
echo "   数据/日志目录 : $MITM_DATA_DIR"
echo "   监听          : $MC_LISTEN_HOST:$MC_LISTEN_PORT"
echo "   命中策略      : MITM_REDIRECT_HOSTS=$MITM_REDIRECT_HOSTS"
echo "   证书目录      : $MC_MITM_CONFDIR"
echo "------------------------------------------------------------"
if [ -n "$IP_LINES" ]; then
  echo " 当前可用网卡 IPv4（优先显示热点接口）："
  echo "$IP_LINES" | sed 's/^/   /'
  if [ -n "$GW_HINT" ]; then
    echo "   $GW_HINT"
  fi
else
  echo " 未读到网卡 IPv4。请确认已开启热点后重试。"
  echo " 也可手动在安卓设备热点页面查看网关 IP（常见 192.168.43.1 或 192.168.49.1）。"
fi
echo "============================================================"
echo " 客户端设置：WiFi 代理=手动，主机=上方热点 IP，端口=$MC_LISTEN_PORT"
echo "============================================================"
echo

cd "$MC_SCRIPTS"
echo "[预检] mitmdump 路径: $(command -v mitmdump || echo 未找到)"
mitmdump --version 2>/dev/null | head -n 1 || true
exec mitmdump \
  --set confdir="$MC_MITM_CONFDIR" \
  --listen-host "$MC_LISTEN_HOST" \
  --listen-port "$MC_LISTEN_PORT" \
  -s ./redirect_addon.py
