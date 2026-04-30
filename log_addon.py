# -*- coding: utf-8 -*-
"""mitmproxy 插件（教材明文下载替换）：
1) 记录教材相关请求/响应摘要；
2) 对 forclass 域名强制连真实 IP（绕过本机 Clash 曾用的 fake-IP）；
3) 对 forclass HTTPS：改连接目标 + TLS 透传（平板无需装证书）；
4) 其它 HTTPS：透传并记 CONNECT 日志；
5) 替换：仅对「明文 HTTP」且路径为教材分片 zip 的请求，用本地文件整体替换响应体。

重要：不得把 HTTP 请求的 host 改成 IP（否则请求行会变成 GET http://183.../ ，
部分 CDN 会返回 418）。应在 server_connect 里只改 TCP 连接目标，保留域名与 Host 头。

使用说明：
- 默认优先使用 payload 下的 PDF：在内存中打成 zip 再下发（URL 仍是 .zip，_body_ 为 zip）。
- 若无该 PDF，则退回 payload/replacement.zip。
- 环境变量 MITM_PAYLOAD 可指定任意替包路径（.pdf 会动态打包为 zip，.zip 则原样下发）。
- MITM_ZIP_INNER_NAME 可指定 zip 内文件名（默认与 PDF 文件名相同）。
- MITM_REPLACE=0 关闭替换；MITM_RELOAD=1 每次重新读文件（调试用）。
- MITM_REPLACE_SEGMENTS：默认「1」只替换第一个分片 *_1.zip（多卷书每卷同一替包会破坏校验）。
  设为 all 或 * 则每个分片都替换（多数情况下仍会下载失败）；也可写 1,2,3。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import datetime

_BASE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_BASE, "mitm_logs")
LOG_FILE = os.path.join(LOG_DIR, "capture_urls.txt")
# 默认 PDF（按需修改为你实际要塞进平板的文件）
_DEFAULT_PDF = os.path.join(_BASE, "payload", "漫画算法：小灰的算法之旅.pdf")
_FALLBACK_ZIP = os.path.join(_BASE, "payload", "replacement.zip")

# 仅改 TCP 连接目标 (域名, 端口) -> (IP, 端口)，不改 HTTP 请求行/Host
_FCDATA_IP = "183.237.146.143"
_WEBSERVICE_IP = "120.240.157.195"
FORCE_TCP: dict[tuple[str, int], tuple[str, int]] = {
    ("webservice.forclass.net", 443): (_WEBSERVICE_IP, 443),
    ("fcdata.forclass.net", 443): (_FCDATA_IP, 443),
    ("fcdata.forclass.net", 80): (_FCDATA_IP, 80),
}

# 这些域名 HTTPS 走透传（不解密）
HTTPS_PASSTHRU_HOSTS = frozenset({"webservice.forclass.net", "fcdata.forclass.net"})

INTEREST_SUFFIX = ("forclass.net",)

_NOISE_HOSTS = (
    "connectivitycheck.platform.hicloud.com",
    "connectivitycheck.cbg-app.huawei.com",
    "connectivitycheck.gstatic.com",
    "clients3.google.com",
    "www.gstatic.com",
)

_REPLACE_ENABLED = os.environ.get("MITM_REPLACE", "1") == "1"
_PAYLOAD_CACHE: bytes | None = None

# 路径末尾 …/61655_V6/61655_10.zip → 分片号 10
_SEGMENT_RE = re.compile(r"_(\d+)\.zip$", re.IGNORECASE)


def _zip_segment_index(path: str) -> int | None:
    m = _SEGMENT_RE.search(path or "")
    return int(m.group(1)) if m else None


def _segment_allowed_for_replace(seg: int) -> bool:
    """由 MITM_REPLACE_SEGMENTS 决定是否替换该分片。"""
    raw = os.environ.get("MITM_REPLACE_SEGMENTS", "1").strip().lower()
    if raw in ("*", "all"):
        return True
    parts = [p.strip() for p in raw.split(",") if p.strip() and p.strip().isdigit()]
    if not parts:
        return seg == 1
    allowed = {int(p) for p in parts}
    return seg in allowed


def _resolved_source_path() -> str | None:
    """决定使用哪个本地文件作为替包来源。"""
    env = os.environ.get("MITM_PAYLOAD", "").strip()
    candidates = []
    if env:
        candidates.append(env)
    candidates.extend((_DEFAULT_PDF, _FALLBACK_ZIP))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return None


def _pdf_to_zip_bytes(pdf_bytes: bytes, inner_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(
        buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6
    ) as zf:
        zf.writestr(inner_name, pdf_bytes)
    return buf.getvalue()


def _is_noise(host: str) -> bool:
    return bool(host) and host in _NOISE_HOSTS


def _is_interesting(host: str) -> bool:
    if not host:
        return False
    return any(host == s or host.endswith("." + s) for s in INTEREST_SUFFIX)


def _append(line: str) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line)


def server_connect(data) -> None:
    """在建立 TCP 前把 forclass 相关连接指到真实 IP（避免 fake-IP，且不把请求行改成 IP）。"""
    addr = data.server.address
    if not addr or len(addr) < 2:
        return
    host, port = addr[0], addr[1]
    if not isinstance(host, str):
        return
    key = (host.lower().rstrip("."), int(port))
    if key in FORCE_TCP:
        new = FORCE_TCP[key]
        data.server.address = new
        _append(
            f"{datetime.now().isoformat()}\tSERVER_TCP\t"
            f"{host}:{port}\t->\t{new[0]}:{new[1]}\n"
        )


def _host_from_request(req) -> str:
    return (req.headers.get("Host") or req.pretty_host or "").split(":")[0].lower()


def _should_replace_zip(req) -> bool:
    if not _REPLACE_ENABLED:
        return False
    host = _host_from_request(req)
    if host != "fcdata.forclass.net":
        return False
    path = req.path or ""
    path_lower = path.lower()
    if not path_lower.endswith(".zip"):
        return False
    if "/book/bookdata/" not in path_lower:
        return False
    if req.method.upper() not in ("GET", "HEAD"):
        return False
    seg = _zip_segment_index(path)
    if seg is None:
        return False
    return _segment_allowed_for_replace(seg)


def _load_payload() -> bytes | None:
    global _PAYLOAD_CACHE
    if os.environ.get("MITM_RELOAD") == "1":
        _PAYLOAD_CACHE = None
    if _PAYLOAD_CACHE is not None:
        return _PAYLOAD_CACHE

    src = _resolved_source_path()
    if src is None:
        _append(
            f"{datetime.now().isoformat()}\tWARN\t未找到替包："
            f"已尝试 MITM_PAYLOAD、{_DEFAULT_PDF}、{_FALLBACK_ZIP}\n"
        )
        return None

    lower = src.lower()
    if lower.endswith(".pdf"):
        with open(src, "rb") as f:
            pdf = f.read()
        inner = os.environ.get("MITM_ZIP_INNER_NAME", "").strip()
        if not inner:
            inner = os.path.basename(src)
        _PAYLOAD_CACHE = _pdf_to_zip_bytes(pdf, inner)
        _append(
            f"{datetime.now().isoformat()}\tLOAD\tPDF→ZIP\t{src}\t"
            f"内文件名={inner}\tzip 总大小={len(_PAYLOAD_CACHE)} bytes\n"
        )
    else:
        with open(src, "rb") as f:
            _PAYLOAD_CACHE = f.read()
        _append(
            f"{datetime.now().isoformat()}\tLOAD\tZIP 原文件\t{src}\t"
            f"{len(_PAYLOAD_CACHE)} bytes\n"
        )
    return _PAYLOAD_CACHE


def http_connect(flow) -> None:
    req = flow.request
    host = req.pretty_host or req.host

    if _is_noise(host):
        flow.ignore_connection = True
        return

    if host in HTTPS_PASSTHRU_HOSTS:
        flow.ignore_connection = True
        _append(
            f"{datetime.now().isoformat()}\tHTTPS_PASSTHRU\t{host}:{req.port}\n"
        )
        return

    if not _is_interesting(host):
        flow.ignore_connection = True
        _append(
            f"{datetime.now().isoformat()}\tCONNECT_PASSTHRU\t{host}:{req.port}\n"
        )
        return

    _append(f"{datetime.now().isoformat()}\tCONNECT\t{host}:{req.port}\n")


def tls_clienthello(data) -> None:
    try:
        sni = data.client_hello.sni
    except Exception:
        sni = None
    if not sni or _is_noise(sni):
        return
    if sni and sni.lower().rstrip(".") in HTTPS_PASSTHRU_HOSTS:
        data.ignore_connection = True
        return
    if not _is_interesting(sni):
        data.ignore_connection = True
        return
    _append(f"{datetime.now().isoformat()}\tSNI\t{sni}\n")


def request(flow) -> None:
    req = flow.request
    host = req.pretty_host
    if _is_noise(host):
        return

    if _should_replace_zip(req):
        # 去掉分片 Range，整体替包更简单；若 App 强依赖 Range 需再改逻辑
        if "Range" in req.headers:
            del req.headers["Range"]
            _append(f"{datetime.now().isoformat()}\tNOTE\t已去掉 Range 头以便整包替换\n")
        setattr(flow, "_mitm_replace_zip", True)

    seg = _zip_segment_index(req.path or "")
    seg_txt = f"\tseg={seg}" if seg is not None else ""
    flag = "\tREPLACE_CANDIDATE" if getattr(flow, "_mitm_replace_zip", False) else ""
    _append(
        f"{datetime.now().isoformat()}\tREQ\t{req.method}\t"
        f"{host}\t{req.path}{seg_txt}{flag}\n"
    )


def response(flow) -> None:
    if flow.response is None:
        return
    req = flow.request
    host = req.headers.get("Host") or req.pretty_host
    if _is_noise(host):
        return

    if getattr(flow, "_mitm_replace_zip", False):
        payload = _load_payload()
        if payload is not None:
            # 清除影响体长与分片的头
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
            flow.response.headers["Content-Type"] = "application/zip"
            flow.response.headers["Content-Length"] = str(len(payload))
            if req.method.upper() == "HEAD":
                # HEAD：与 GET 相同的头，无正文
                flow.response.raw_content = b""
            else:
                flow.response.raw_content = payload
            _append(
                f"{datetime.now().isoformat()}\tREPLACE_OK\t"
                f"{host}\t{req.path}\t{len(payload)} bytes\n"
            )

    ct = flow.response.headers.get("Content-Type", "")
    cl = flow.response.headers.get("Content-Length", "")
    _append(
        f"{datetime.now().isoformat()}\tRSP\t{flow.response.status_code}\t"
        f"{req.method}\t{host}\t{req.path}\tCT={ct}\tCL={cl}\n"
    )
