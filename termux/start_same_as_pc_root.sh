#!/system/bin/sh
# 安卓端复刻电脑成功参数（root）
set -e

# 1) 杀旧进程，避免端口冲突
pkill -9 -f mitmdump 2>/dev/null || true
sleep 1

# 2) 与电脑端一致的环境变量（只改路径）
export HOME=/data/data/com.termux/files/home
export PREFIX=/data/data/com.termux/files/usr
export PATH=/data/data/com.termux/files/usr/bin:/data/data/com.termux/files/usr/bin/applets:$PATH

: "${MITM_REDIRECT_HOSTS:=example.com:8080}"
export MITM_REDIRECT_HOSTS
export MITM_SHARE_DIR="/data/data/com.termux/files/home/storage/shared/VerPadProxy/payload"
export MITM_DATA_DIR="/data/data/com.termux/files/home/storage/shared/VerPadProxy"
export MITM_LOG_QUIET="0"

cd /data/data/com.termux/files/home/storage/shared/VerPadProxy/scripts

# 3) 启动
nohup /data/data/com.termux/files/usr/bin/mitmdump \
  -s ./redirect_addon.py \
  --listen-host 0.0.0.0 \
  --listen-port 2345 \
  >/sdcard/VerPadProxy/_mitmdump.log 2>&1 &

sleep 2

echo "== 进程 =="
pgrep -af mitmdump || true
echo "== 监听 =="
ss -lntp 2>/dev/null | grep 2345 || true
echo "== 末尾日志 =="
tail -n 40 /sdcard/VerPadProxy/_mitmdump.log || true
