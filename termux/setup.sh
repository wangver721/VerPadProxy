#!/data/data/com.termux/files/usr/bin/env bash
# -*- coding: utf-8 -*-
# 媒体中心·Termux 首次安装脚本（纯 LF；可在 Termux 里重复执行）

set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SELF_DIR/env.sh"

echo "=== 1/4 切换国内源并更新 Termux ==="
# 切到清华 Termux 镜像（避免默认源下载过慢）
MIRROR_MAIN="deb https://mirrors.tuna.tsinghua.edu.cn/termux/apt/termux-main stable main"
if [ -w "$PREFIX/etc/apt/sources.list" ]; then
  echo "$MIRROR_MAIN" > "$PREFIX/etc/apt/sources.list"
fi
# 关闭交互询问、保留本地配置
export DEBIAN_FRONTEND=noninteractive
yes | pkg update || true
yes | pkg upgrade || true
pkg install -y python python-pip openssl libffi clang make pkg-config termux-api || true

# pip 用清华镜像（永久写入 ~/.config/pip/pip.conf）
python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1 || true
python -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn >/dev/null 2>&1 || true

echo "=== 2/4 申请存储权限（若未授权会弹框） ==="
if [ ! -d "$HOME/storage/shared" ]; then
  yes | termux-setup-storage || true
  sleep 2
else
  echo "  已检测到 $HOME/storage，跳过重复授权。"
fi

echo "=== 3/4 安装 mitmproxy ==="
# 优先 Termux 预编译包：内含 cryptography 等 Rust/C 依赖，避免现场编译
if pkg install -y mitmproxy 2>/dev/null; then
  echo "  已通过 pkg 安装 mitmproxy"
else
  echo "  pkg 未命中，退回 pip（依赖仍用 pkg 预编译以免现场编译 Rust）"
  pkg install -y python-cryptography python-brotli python-pillow python-numpy || true
  python -m pip install --no-cache-dir "mitmproxy>=10"
fi

echo "=== 4/4 尝试安装 PyMuPDF（可选） ==="
if pkg install -y python-pymupdf 2>/dev/null; then
  echo "  已通过 pkg 安装 python-pymupdf"
elif python -m pip install --upgrade pymupdf 2>/dev/null; then
  echo "  已通过 pip 安装 pymupdf"
else
  echo "  未能安装 PyMuPDF：/pdf.png 栅格化不可用；PDF 阅读器仍可用。"
fi

echo
echo "=== 创建目录骨架 ==="
mkdir -p "$MITM_SHARE_DIR"/{PDF,视频,音乐,private,upl}
mkdir -p "$MITM_DATA_DIR"
mkdir -p "$MC_SCRIPTS"

echo
echo "====================================================="
echo " 安装完成。下一步执行："
echo "     bash $SELF_DIR/start.sh"
echo "====================================================="
