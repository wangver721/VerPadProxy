# -*- coding: utf-8 -*-
"""仅抓包记录，不篡改、不重定向。用于定位教材 PDF 下载（明文 HTTP 可见完整 URL；HTTPS 只能看到域名）。

输出：mitm_logs/capture_download.txt
命中关键字会在行尾标记 <<<PDF相关>>>（含 .pdf、乡村/城镇/空间、红楼梦等）。

用法：
  mitmdump -s .\\capture_only.py --listen-host 0.0.0.0 --listen-port 8080

平板 Wi-Fi 代理：热点网关 IP + 8080，在 forclass 内触发下载后把日志里相关行发我。
"""

from __future__ import annotations

import os
from datetime import datetime

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mitm_logs")
_LOG_FILE = os.path.join(_LOG_DIR, "capture_download.txt")

def _pdf_flag(text: str) -> str:
    t = text.lower()
    if ".pdf" in t or "/pdf" in t or "application/pdf" in t:
        return "\t<<<PDF相关>>>"
    markers = (
        "红楼梦",
        "hongloumeng",
        "乡村",
        "城镇",
        "空间结构",
        "xiangcun",
        "chengzhen",
    )
    if any(m in text for m in markers):
        return "\t<<<PDF相关>>>"
    return ""


_NOISE = (
    "connectivitycheck.platform.hicloud.com",
    "connectivitycheck.cbg-app.huawei.com",
    "connectivitycheck.gstatic.com",
    "clients3.google.com",
    "www.gstatic.com",
)


def _append(line: str) -> None:
    os.makedirs(_LOG_DIR, exist_ok=True)
    with open(_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def _noise_host(h: str) -> bool:
    return bool(h) and h in _NOISE


def http_connect(flow) -> None:
    req = flow.request
    host = req.pretty_host or req.host
    if _noise_host(host):
        flow.ignore_connection = True
        return
    flow.ignore_connection = True
    _append(f"{datetime.now().isoformat()}\tCONNECT\t{host}:{req.port}\n")


def tls_clienthello(data) -> None:
    try:
        sni = data.client_hello.sni
    except Exception:
        sni = None
    if not sni or _noise_host(sni):
        return
    data.ignore_connection = True
    _append(f"{datetime.now().isoformat()}\tSNI\t{sni}\n")


def request(flow) -> None:
    req = flow.request
    if req.method.upper() == "CONNECT":
        return
    host = (req.headers.get("Host") or req.pretty_host or "").split(":")[0]
    if _noise_host(host):
        return
    url = req.pretty_url
    flag = _pdf_flag(url + "\t" + (req.path or ""))
    _append(
        f"{datetime.now().isoformat()}\tREQ\t{req.method}\t{url}{flag}\n"
    )


def response(flow) -> None:
    if flow.response is None:
        return
    req = flow.request
    if req.method.upper() == "CONNECT":
        return
    host = (req.headers.get("Host") or req.pretty_host or "").split(":")[0]
    if _noise_host(host):
        return
    r = flow.response
    ct = r.headers.get("Content-Type", "")
    cl = r.headers.get("Content-Length", "")
    cd = r.headers.get("Content-Disposition", "")
    url = req.pretty_url
    flag = _pdf_flag(url + ct + cd)
    _append(
        f"{datetime.now().isoformat()}\tRSP\t{r.status_code}\t{req.method}\t"
        f"{url}\tCT={ct}\tCL={cl}\tCD={cd}{flag}\n"
    )
