#!/data/data/com.termux/files/usr/bin/env bash
# 固定热点网关 IP（root）
# 用法：bash fix_hotspot_ip.sh 192.168.68.1/24
set -e
TARGET_CIDR="${1:-192.168.68.1/24}"
TARGET_IP="${TARGET_CIDR%%/*}"

echo "[1] 设置 Android 全局 DHCP 范围（尽量与热点网段一致）"
su -c "settings put global tether_dhcp_range ${TARGET_IP%.*}.2,${TARGET_IP%.*}.254" || true

echo "[2] 检查热点接口 ap0 是否存在"
if ! su -c 'ip link show ap0 >/dev/null 2>&1'; then
  echo "ap0 不存在。请先手动打开热点后再执行本脚本。"
  exit 1
fi

echo "[3] 应用固定网关到 ap0: $TARGET_CIDR"
su -c "ip addr flush dev ap0"
su -c "ip addr add $TARGET_CIDR dev ap0"
su -c "ip link set ap0 up"

echo "[4] 结果"
su -c 'ip -4 -o addr show dev ap0'
echo "完成。"
