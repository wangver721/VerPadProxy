#!/data/data/com.termux/files/usr/bin/env bash
# 安装 root 常驻守护：热点每次开启后自动改成固定 IP
# 用法：bash install_hotspot_guard.sh 192.168.68.1/24
set -e
TARGET_CIDR="${1:-192.168.68.1/24}"
SERVICE_DIR="/data/adb/service.d"
SERVICE_FILE="$SERVICE_DIR/99-verpadproxy-hotspot-guard.sh"

su -c "mkdir -p '$SERVICE_DIR'"

TMP_LOCAL="/sdcard/VerPadProxy/.hotspot_guard.tmp.sh"
cat > "$TMP_LOCAL" <<EOF
#!/system/bin/sh
TARGET_CIDR="$TARGET_CIDR"
TARGET_IP="\${TARGET_CIDR%%/*}"

# 开机后常驻，每 4 秒检查一次 ap0
while true; do
  if ip link show ap0 >/dev/null 2>&1; then
    CUR="\$(ip -4 -o addr show dev ap0 2>/dev/null | awk '{print \\$4}' | head -n 1)"
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

su -c "cp '$TMP_LOCAL' '$SERVICE_FILE'"
su -c "chmod 755 '$SERVICE_FILE'"
rm -f "$TMP_LOCAL"

echo "已安装 root 热点守护: $SERVICE_FILE"
echo "目标固定网关: $TARGET_CIDR"
echo "重启安卓设备后自动生效。"
echo "卸载命令: su -c 'rm -f $SERVICE_FILE'"
