#!/data/data/com.termux/files/usr/bin/env bash
# 连接详情（root）：显示热点直连与局域网两种连接信息
set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SELF_DIR/env.sh"

AP_INFO=$(su -c 'ip -4 -o addr show dev ap0 2>/dev/null | awk "{print \$4}" | head -n 1' || true)
WLAN_INFO=$(su -c 'ip -4 -o addr show dev wlan0 2>/dev/null | awk "{print \$4}" | head -n 1' || true)

AP_IP="${AP_INFO%%/*}"
WLAN_IP="${WLAN_INFO%%/*}"

echo "============================================================"
echo " VerPadProxy连接详情（root）"
echo " 端口：$MC_LISTEN_PORT"
echo "============================================================"

if [ -n "$AP_IP" ]; then
  echo "[A] 安卓热点直连"
  echo "  代理主机: $AP_IP"
  echo "  代理端口: $MC_LISTEN_PORT"
  echo "  地址格式: $AP_IP:$MC_LISTEN_PORT"
else
  echo "[A] 安卓热点直连"
  echo "  未检测到 ap0 IPv4（请先打开热点）"
fi

echo
if [ -n "$WLAN_IP" ]; then
  echo "[B] 局域网下连接安卓设备（同一 WiFi）"
  echo "  代理主机: $WLAN_IP"
  echo "  代理端口: $MC_LISTEN_PORT"
  echo "  地址格式: $WLAN_IP:$MC_LISTEN_PORT"
else
  echo "[B] 局域网下连接安卓设备（同一 WiFi）"
  echo "  未检测到 wlan0 IPv4（安卓设备可能未连上游 WiFi）"
fi

echo
su -c 'ip route 2>/dev/null | awk "/^default/ {print \"上游默认网关:\", \$3, \"接口:\", \$5; exit}"' || true
echo "============================================================"
