#!/system/bin/sh
# 恢复默认热点 DHCP + IP：卸守护、清 settings、重启热点、输出新 IP
set -e

echo "[1/5] 移除热点守护..."
rm -f /data/adb/service.d/99-verpadproxy-hotspot-guard.sh 2>/dev/null || true
# 若 verpadproxy.sh 的 guard 也安装过，保险再移除一次
[ -f /data/data/com.termux/files/home/storage/shared/VerPadProxy/scripts/termux/verpadproxy.sh ] && \
  /data/data/com.termux/files/usr/bin/bash /data/data/com.termux/files/home/storage/shared/VerPadProxy/scripts/termux/verpadproxy.sh guard-remove >/dev/null 2>&1 || true

echo "[2/5] 清除自定义 DHCP 范围（恢复系统默认）..."
settings delete global tether_dhcp_range >/dev/null 2>&1 || true

echo "[3/5] 关闭热点..."
# Android 13 小米（HyperOS）关热点的官方路径：settings + service call 都试一遍
cmd wifi stop-softap >/dev/null 2>&1 || true
svc wifi disable-softap >/dev/null 2>&1 || true
sleep 2

echo "[4/5] 打开热点（Android 会自动重建 ap0 + 内建 DHCP server）..."
cmd wifi start-softap >/dev/null 2>&1 || true
svc wifi enable-softap >/dev/null 2>&1 || true
sleep 4

# 有些机型只能用 GUI 开，所以这里给提示
if ! ip link show ap0 >/dev/null 2>&1; then
  echo "[!] 未能自动启动热点。请手动在系统设置里开热点，然后重跑本脚本的后半段：'ip -4 -o addr show dev ap0'"
fi

echo "[5/5] 查看新 ap0 的系统默认 IP..."
ip -4 -o addr show dev ap0 2>/dev/null || true
echo
echo "完成。客户端断开热点后再重连，然后代理主机填上面 ap0 的 IP，端口 2345。"
