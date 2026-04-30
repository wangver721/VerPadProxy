#!/data/data/com.termux/files/usr/bin/env bash
# -*- coding: utf-8 -*-
# 媒体中心·Termux 一键引导脚本 v2（幂等，可重复执行）
# 用法：  bash /sdcard/VerPadProxy/scripts/termux/bootstrap.sh
# 每个兜底阶段的错误都保留可见，便于排查

set +e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
. "$SELF_DIR/env.sh"

GREEN="\033[1;32m"; YEL="\033[1;33m"; RED="\033[1;31m"; RST="\033[0m"
log(){ echo; echo -e "${GREEN}=== $* ===${RST}"; }
warn(){ echo -e "${YEL}[!] $*${RST}"; }
err(){ echo -e "${RED}[x] $*${RST}"; }

have_mitm(){ command -v mitmdump >/dev/null 2>&1; }

# ----------------------------------------------------------------------------
log "1) 切换 Termux 包源到清华镜像"
# ----------------------------------------------------------------------------
MIRROR_MAIN="deb https://mirrors.tuna.tsinghua.edu.cn/termux/apt/termux-main stable main"
if [ -w "$PREFIX/etc/apt/sources.list" ]; then
  echo "$MIRROR_MAIN" > "$PREFIX/etc/apt/sources.list"
  echo "  sources.list -> 清华"
else
  warn "sources.list 不可写，跳过"
fi

# ----------------------------------------------------------------------------
log "2) 更新索引 & 装编译/运行时依赖"
# ----------------------------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
yes | pkg update  2>/dev/null
yes | pkg upgrade 2>/dev/null
for p in python python-pip openssl libffi clang make pkg-config termux-api \
         python-cryptography python-brotli python-pillow; do
  pkg install -y "$p" 2>/dev/null
done

# ----------------------------------------------------------------------------
log "3) 申请存储权限"
# ----------------------------------------------------------------------------
if [ ! -d "$HOME/storage/shared" ]; then
  yes | termux-setup-storage 2>/dev/null
  sleep 2
else
  echo "  已存在 $HOME/storage，跳过"
fi

# ----------------------------------------------------------------------------
log "4) 切 pip 到清华源"
# ----------------------------------------------------------------------------
python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple >/dev/null 2>&1
python -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn >/dev/null 2>&1
echo "  pip -> 清华"

# ----------------------------------------------------------------------------
log "5) 安装 mitmproxy（多路兜底：A pkg → B+Rust pip → C 降版本）"
# ----------------------------------------------------------------------------
if have_mitm; then
  echo "  mitmdump 已存在，跳过"
fi

# 兜底 A：Termux 预编译包
if ! have_mitm; then
  echo ">> [A] pkg install mitmproxy"
  pkg install -y mitmproxy
fi

# 兜底 B：pkg 预装原生依赖 + Rust 编译 + pip 装 mitmproxy
if ! have_mitm; then
  echo ">> [B] pkg install rust（给 pip 提供 Rust 编译器）"
  pkg install -y rust binutils make
  hash -r 2>/dev/null
  echo ">> [B] pip install mitmproxy>=10"
  python -m pip install --no-cache-dir "mitmproxy>=10"
fi

# 兜底 C：限定较老分支（依赖更少）
if ! have_mitm; then
  echo ">> [C] pip install mitmproxy==10.4.*"
  python -m pip install --no-cache-dir "mitmproxy==10.4.*"
fi

if ! have_mitm; then
  err "mitmdump 仍未安装成功。请把 A/B/C 三段的红字报错贴出来继续排查。"
  err "也可以手动试： pkg search mitmproxy ； pkg install mitmproxy"
  exit 1
fi
echo "  mitmdump 路径：$(command -v mitmdump)"

# ----------------------------------------------------------------------------
log "6) 可选：PyMuPDF（仅 /pdf.png 渲染需要）"
# ----------------------------------------------------------------------------
pkg install -y python-pymupdf 2>/dev/null \
  || python -m pip install --no-cache-dir pymupdf 2>/dev/null \
  || echo "  PyMuPDF 未装，忽略"

# ----------------------------------------------------------------------------
log "7) 建目录骨架"
# ----------------------------------------------------------------------------
mkdir -p "$MITM_SHARE_DIR"/{PDF,视频,音乐,private,upl} "$MITM_DATA_DIR" "$MC_SCRIPTS"
ls -ld "$MITM_SHARE_DIR" "$MITM_DATA_DIR" "$MC_SCRIPTS"

# ----------------------------------------------------------------------------
log "8) 自检"
# ----------------------------------------------------------------------------
mitmdump --version | head -n 3

# ----------------------------------------------------------------------------
log "9) 启动媒体中心"
# ----------------------------------------------------------------------------
exec bash "$SELF_DIR/start.sh"
