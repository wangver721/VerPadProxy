# -*- coding: utf-8 -*-
"""尝试对 forclass 内置下载：仅对 fcdata.forclass.net 做 HTTPS 解密，并把 PDF 响应替换为本地文件。

前提：平板/App 必须信任 mitmproxy 证书，否则会 TLS 握手失败（多数校管平板做不到）。
若失败，日志里会出现 client does not trust / handshake failed。

用法：
  mitmdump -s .\\replace_pdf_addon.py --listen-host 0.0.0.0 --listen-port 8080

环境变量：
  MITM_REPLACE_PDF   本地 PDF 路径（默认 payload/JOJOPART7_01.pdf）
  MITM_REPLACE_FORCE=1  大文件响应也替换（见 MITM_REPLACE_MIN_BYTES，默认约 500KB，降低误伤小接口）
  MITM_REPLACE_MIN_BYTES  与 FORCE 联用，默认 500000
"""

from __future__ import annotations

import os
from datetime import datetime

from mitmproxy.http import Response

_BASE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PDF = os.path.join(_BASE, "payload", "JOJOPART7_01.pdf")
_LOG = os.path.join(_BASE, "mitm_logs", "pdf_replace.txt")

_NOISE = (
    "connectivitycheck.platform.hicloud.com",
    "connectivitycheck.cbg-app.huawei.com",
    "connectivitycheck.gstatic.com",
    "clients3.google.com",
    "www.gstatic.com",
)

# 仅解密此主机；其它 HTTPS 仍透传，减少对 zzn / webservice 的干扰
_MITM_HOST = "fcdata.forclass.net"

_PDF_CACHE: bytes | None = None


def _log(msg: str) -> None:
    os.makedirs(os.path.dirname(_LOG), exist_ok=True)
    with open(_LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()}\t{msg}\n")


def _noise(h: str | None) -> bool:
    return bool(h) and h in _NOISE


def _pdf_path() -> str:
    return os.environ.get("MITM_REPLACE_PDF", _DEFAULT_PDF).strip() or _DEFAULT_PDF


def _load_pdf() -> bytes | None:
    global _PDF_CACHE
    if _PDF_CACHE is not None:
        return _PDF_CACHE
    p = _pdf_path()
    if not os.path.isfile(p):
        _log(f"ERR\t找不到文件\t{p}")
        return None
    with open(p, "rb") as f:
        _PDF_CACHE = f.read()
    _log(f"LOAD\t{p}\t{len(_PDF_CACHE)} bytes")
    return _PDF_CACHE


def http_connect(flow) -> None:
    host = flow.request.pretty_host or flow.request.host
    if _noise(host):
        flow.ignore_connection = True
        return
    if host == _MITM_HOST:
        # 不解密则无法改 PDF；交给 mitmproxy 做 HTTPS 中间人
        return
    flow.ignore_connection = True


def tls_clienthello(data) -> None:
    try:
        sni = data.client_hello.sni
    except Exception:
        sni = None
    if _noise(sni):
        data.ignore_connection = True
        return
    if sni == _MITM_HOST:
        return
    data.ignore_connection = True


def request(flow) -> None:
    if flow.request.method.upper() == "CONNECT":
        return
    host = (flow.request.host_header or flow.request.pretty_host or "").split(":")[0].lower()
    if host != _MITM_HOST:
        return
    if "Range" in flow.request.headers:
        del flow.request.headers["Range"]
        _log(f"NOTE\t去掉 Range\t{flow.request.path}")
    low = (flow.request.path or "").lower()
    if ".pdf" in low or "pdf" in low:
        setattr(flow, "_maybe_replace_pdf", True)
    _log(f"REQ\t{flow.request.method}\t{flow.request.pretty_url}")


def response(flow) -> None:
    if flow.response is None:
        return
    req = flow.request
    if req.method.upper() == "CONNECT":
        return
    host = (req.host_header or req.pretty_host or "").split(":")[0].lower()
    if host != _MITM_HOST:
        return

    ct = (flow.response.headers.get("Content-Type") or "").lower()
    cd = (flow.response.headers.get("Content-Disposition") or "").lower()
    path_low = (req.path or "").lower()

    is_pdf = (
        "application/pdf" in ct
        or path_low.endswith(".pdf")
        or ".pdf" in path_low
        or ("application/octet-stream" in ct and ".pdf" in cd)
        or ("filename" in cd and ".pdf" in cd)
    )
    force = os.environ.get("MITM_REPLACE_FORCE", "0") == "1"
    if force:
        try:
            min_b = int(os.environ.get("MITM_REPLACE_MIN_BYTES", "500000"))
        except ValueError:
            min_b = 500_000
        body_len = len(flow.response.raw_content or b"")
        is_pdf = (
            req.method.upper() == "GET"
            and flow.response.status_code == 200
            and body_len >= min_b
        )

    if not is_pdf and not getattr(flow, "_maybe_replace_pdf", False):
        _log(f"RSP\tSKIP\t{flow.response.status_code}\t{req.path}\tCT={ct}")
        return

    data = _load_pdf()
    if data is None:
        return

    for hk in (
        "Content-Encoding",
        "Transfer-Encoding",
        "Content-Range",
        "Accept-Ranges",
    ):
        if hk in flow.response.headers:
            del flow.response.headers[hk]

    flow.response.status_code = 200
    flow.response.reason = "OK"
    flow.response.headers["Content-Type"] = "application/pdf"
    flow.response.headers["Content-Length"] = str(len(data))
    flow.response.headers.pop("Content-Disposition", None)

    if req.method.upper() == "HEAD":
        flow.response.raw_content = b""
    else:
        flow.response.raw_content = data

    _log(f"REPLACE_OK\t{req.method}\t{req.path}\t{len(data)} bytes")
