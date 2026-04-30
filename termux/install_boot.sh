#!/data/data/com.termux/files/usr/bin/env bash
set -e
BOOT_DIR="$HOME/.termux/boot"
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOT_FILE="$BOOT_DIR/verpadproxy.sh"

mkdir -p "$BOOT_DIR"
cat > "$BOOT_FILE" <<EOF
#!/data/data/com.termux/files/usr/bin/env bash
sleep 12
bash "$SELF_DIR/daemon_start.sh"
EOF
chmod +x "$BOOT_FILE"

echo "Termux:Boot 自启脚本已写入: $BOOT_FILE"
echo "重启安卓设备后会自动后台拉起媒体中心。"

echo
echo "若要禁用自启："
echo "  rm -f $BOOT_FILE"
