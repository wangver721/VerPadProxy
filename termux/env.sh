# -*- coding: utf-8 -*-
# 媒体中心·Termux 环境变量（start.sh / setup.sh 会 source 本文件）
# 修改本文件后重新执行 start.sh 即可生效。

# 根目录；默认使用「内部存储」下的 VerPadProxy（termux-setup-storage 之后可访问）
: "${MC_ROOT:=$HOME/storage/shared/VerPadProxy}"

# 代码目录（redirect_addon.py 等脚本所在位置）
: "${MC_SCRIPTS:=$MC_ROOT/scripts}"

# 媒体根（内部再分 PDF/视频/音乐/private/upl）
: "${MITM_SHARE_DIR:=$MC_ROOT/payload}"

# 用户库与日志的数据目录（避免与 PC 上的 mitm_users.json 混用）
: "${MITM_DATA_DIR:=$MC_ROOT/data}"
: "${MITM_USERS_FILE:=$MITM_DATA_DIR/mitm_users.json}"
: "${MITM_VISIT_LOG:=$MITM_DATA_DIR/mitm_visit.log}"
: "${MITM_EXIT_TELEMETRY_LOG:=$MITM_DATA_DIR/mitm_exit_telemetry.log}"

# 命中策略：
#   "*" 表示处理所有经代理的 HTTP 明文请求（最适合「平板专用代理」场景）；
#   如只想劫持若干域名，改成「host1,host2:8080」形式。
: "${MITM_REDIRECT_HOSTS:=*}"

# 日志只写文件、不在终端里滚屏
: "${MITM_LOG_QUIET:=1}"

# 监听地址 / 端口（热点下必须 0.0.0.0）
: "${MC_LISTEN_HOST:=0.0.0.0}"
: "${MC_LISTEN_PORT:=2345}"

# mitmproxy 配置目录（放证书与 key），第一次启动会自动生成
: "${MC_MITM_CONFDIR:=$HOME/.mitmproxy}"

# PyMuPDF 可选：装上之后 /pdf.png 页面栅格化才可用
: "${MITM_PYMUPDF_PYTHON:=python}"

export MC_ROOT MC_SCRIPTS
export MITM_SHARE_DIR MITM_DATA_DIR MITM_USERS_FILE
export MITM_VISIT_LOG MITM_EXIT_TELEMETRY_LOG
export MITM_REDIRECT_HOSTS MITM_LOG_QUIET
export MC_LISTEN_HOST MC_LISTEN_PORT MC_MITM_CONFDIR
export MITM_PYMUPDF_PYTHON
