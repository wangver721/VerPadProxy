# -*- coding: utf-8 -*-
"""mitm 脚本：把命中域名改写成「个人媒体中心」。

功能：
- 首页：PDF 阅读 / 视频 / 音乐 / 文件上传四大入口
- 文件浏览器：面包屑、搜索、排序（不提供「下载」直链）
- PDF 阅读器：默认 fit-to-screen、双指捏合缩放与单指拖动、双击切换；**翻页条在右下角**（与全局退出小方块错层）
- 视频播放器：尺寸/对齐预设、倍速、**长按画面临时 2 倍速**、**双指在画面上缩放/平移**、外挂字幕
- 图片查看器、文本查看器（自动转码）
- 音乐播放器：遍历 `音乐/` 全部音频，支持顺序/单曲循环/随机、封面识别、MediaSession
- 文件上传：多选上传到 `upl/`，自动避免覆盖；**需登录**且具备「上传」权限；浏览 `upl/` 与 `private/` 由管理员配置名单与功能开关
- 任意页面右下角无文字小按钮：**一键退出**（全黑后优先进 **`/__mitm-trap`** 同源专页再跳 **`/__mitm-exit`**；`data:` 为备选；系统返回键是否被外层拦截取决于客户端壳本身）
- 多用户登录（用户名+密码），**mitm_users.json** 存用户与权限；默认管理员 **admin** / **change-me-please**（**首次启动务必用 `MITM_BOOTSTRAP_PASSWORD` 显式覆盖**）；`private/` 与浏览 `upl/` 由管理员配置名单与功能开关

目录约定（可由环境变量覆盖）：
  payload/
  ├── PDF/    （电子书）
  ├── 视频/   （含字幕 sidecar）
  ├── 音乐/   （支持子目录 + cover.jpg）
  ├── private/（可访问用户由管理端配置）
  └── upl/    （上传/浏览 u 由权限控制，不存在会自动创建）

所有路径均基于 `MITM_SHARE_DIR`（默认为 `C:\\VerPadProxy\\payload` 或 `/sdcard/VerPadProxy/payload`）。

环境与劫持：
- `MITM_REDIRECT_HOSTS`：未在系统环境中设置时，默认劫持 `example.com:8080`；若**显式**设为空字符串则完全不劫持。支持 `主机` 或 `主机:端口`（带端口则只匹配该端口）。
- `MITM_EXIT_TELEMETRY_LOG`：记录「退出相关」页面事件（`pagehide` / `vis_hidden` 等）的路径，默认 `脚本目录/mitm_exit_telemetry.log`；设为 `off` 关闭。用于对照**手动正常退出**与右下角小方块的时序，便于在壳上实现同等退出方式。
- `MITM_VISIT_LOG`：记录访问的日志路径，默认 `脚本目录/mitm_visit.log`；设为 `off` 关闭记录。
- `MITM_LOG_QUIET`：设 `1` 只写文件、不在 mitmdump 里逐行 print。
- `MITM_USERS_FILE`：用户库 JSON 路径，默认脚本目录下 `mitm_users.json`；`MITM_DATA_DIR`：数据目录（与默认用户库同级的父目录等）；`MITM_BOOTSTRAP_PASSWORD`：首次生成 **admin** 时的口令；`MITM_SESSION_MAX_AGE`：会话 Cookie 秒数，`0` 表示仅会话级（关 WebView 后视壳清除 Cookie 而定）。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from mitmproxy.http import Response

import user_auth


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def _which(name: str) -> str | None:
    """缓存 PATH 扫描结果，避免热路径每请求都扫一次。"""
    return shutil.which(name)

# ---------------------------------------------------------------------------
# 常量与配置
# ---------------------------------------------------------------------------

_BASE = Path(os.path.dirname(os.path.abspath(__file__)))
_FALLBACK_SHARE_DIR = _BASE / "payload"
_USER_PAYLOAD_DIR = Path("payload").resolve()
_PDF_RENDER_HELPER = str(_BASE / "pdf_page_render.py")
# 内置分区名（可用环境变量覆盖）
_DIR_PDF = "PDF"
_DIR_VIDEO = "视频"
_DIR_MUSIC = "音乐"
_DIR_UPLOAD = "upl"
_DIR_PRIVATE = "private"

_VIDEO_EXTS = {".mp4", ".webm", ".m4v", ".mov", ".ogv", ".mkv"}
_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".aac", ".wav", ".ogg", ".oga", ".opus"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".log", ".ini", ".cfg", ".conf",
    ".json", ".xml", ".yaml", ".yml", ".csv", ".tsv",
    ".py", ".js", ".ts", ".css", ".html", ".htm",
    ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs", ".sh", ".ps1", ".bat",
}
_SUB_EXTS = {".srt", ".vtt", ".ass", ".ssa"}
_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png")

mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/opus", ".opus")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("application/pdf", ".pdf")
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("text/vtt", ".vtt")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _share_root() -> Path:
    env = os.environ.get("MITM_SHARE_DIR", "").strip()
    if env:
        return Path(env).resolve()
    if _USER_PAYLOAD_DIR.is_dir():
        return _USER_PAYLOAD_DIR.resolve()
    return _FALLBACK_SHARE_DIR.resolve()


def _redirect_hosts_raw() -> str:
    # 未设置环境变量时：默认劫持示例占位主机；请通过 MITM_REDIRECT_HOSTS 显式指定
    if "MITM_REDIRECT_HOSTS" in os.environ:
        return (os.environ.get("MITM_REDIRECT_HOSTS") or "").strip()
    return "example.com:8080"


def _visit_log_file() -> Path | None:
    raw = (os.environ.get("MITM_VISIT_LOG", "") or "").strip().lower()
    if raw in ("0", "off", "no", "false", "none"):
        return None
    if (os.environ.get("MITM_VISIT_LOG", "") or "").strip():
        return Path((os.environ.get("MITM_VISIT_LOG", "") or "").strip()).expanduser()
    return _BASE / "mitm_visit.log"


def _log_quiet() -> bool:
    return (os.environ.get("MITM_LOG_QUIET", "") or "").strip() in ("1", "true", "yes", "on")


def _client_addr_str(flow) -> str:
    try:
        a = flow.client_conn.peername
        if a and len(a) >= 1:
            return str(a[0])
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    return "-"


def _log_visit_line(kind: str, detail: str, flow) -> None:
    """在控制台与 MITM_VISIT_LOG 中记录：CONNECT=尝试连的宿主机:端口，HTTP=解密后的方法+URL。"""
    p = _visit_log_file()
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t{detail}\t{_client_addr_str(flow)}\n"
    if not _log_quiet():
        try:
            print(line.rstrip(), flush=True)
        except OSError:
            pass
    if p is not None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8", newline="") as f:
                f.write(line)
        except OSError:
            pass


def _exit_telemetry_log_file() -> Path | None:
    """退出事件遥测日志路径；`off` 等关闭。默认脚本目录下 mitm_exit_telemetry.log。"""
    raw = (os.environ.get("MITM_EXIT_TELEMETRY_LOG", "") or "").strip().lower()
    if raw in ("0", "off", "no", "false", "none"):
        return None
    if (os.environ.get("MITM_EXIT_TELEMETRY_LOG", "") or "").strip():
        return Path((os.environ.get("MITM_EXIT_TELEMETRY_LOG", "") or "").strip()).expanduser()
    return _BASE / "mitm_exit_telemetry.log"


def _exit_telemetry_response(flow) -> Response:
    """记录页面卸载/不可见/小方块点击等，便于对照「手动关 WebView」与脚本退出的时序。返回 204 便于 sendBeacon。"""
    method = flow.request.method.upper()
    if method == "OPTIONS":
        return Response.make(
            204,
            b"",
            {
                "Allow": "GET, POST, HEAD, OPTIONS",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, HEAD, OPTIONS",
                "Access-Control-Max-Age": "86400",
            },
        )
    if method not in ("GET", "HEAD", "POST"):
        return Response.make(
            405,
            b"Method Not Allowed",
            {
                "Allow": "GET, POST, HEAD, OPTIONS",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
    p = _exit_telemetry_log_file()
    u = urlparse(flow.request.pretty_url)
    qs = parse_qs(u.query)
    def _one(k: str, default: str = "-") -> str:
        v = qs.get(k, [default])
        return (v[0] if v else default) or default
    ev = _one("e", "?")
    ts_cli = _one("t", "?")
    page = _one("p", "-")
    if len(page) > 600:
        page = page[:600] + "…"
    ref = ((flow.request.headers.get("Referer") or "-").replace("\n", " ").replace("\t", " "))[:400]
    ua = ((flow.request.headers.get("User-Agent") or "-").replace("\n", " "))[:240]
    line = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\tEXIT-TEL\t{ev}\t"
        f"client_t={ts_cli}\tpage={page}\tref={ref}\tua={ua}\t{_client_addr_str(flow)}\n"
    )
    if p is not None:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8", newline="") as f:
                f.write(line)
        except OSError:
            pass
    if p is not None and not _log_quiet():
        try:
            print(line.rstrip(), flush=True)
        except OSError:
            pass
    h: dict[str, str] = {
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
    }
    if method == "HEAD":
        return Response.make(204, b"", h)
    return Response.make(204, b"", h)


def _pymupdf_python_exe() -> str:
    return (os.environ.get("MITM_PYMUPDF_PYTHON", "python") or "python").strip()


def _raster_scale() -> float:
    return max(0.5, min(3.0, _env_float("MITM_PDF_RASTER_SCALE", 1.35)))


def _error_http_status() -> int:
    s = _env_int("MITM_HTTP_ERROR_STATUS", 200)
    return s if 100 <= s <= 599 else 200


def _max_inline_bytes() -> int:
    return max(64 * 1024, _env_int("MITM_MAX_INLINE_BYTES", 8 * 1024 * 1024))


def _reload_flag() -> bool:
    return os.environ.get("MITM_RELOAD", "0").strip() == "1"


# ---------------------------------------------------------------------------
# Host / 路径
# ---------------------------------------------------------------------------

def _normalize_host(flow) -> str:
    h = flow.request.host_header or flow.request.pretty_host or ""
    return h.split(":")[0].lower().rstrip(".")


def _request_host_port(flow) -> tuple[str, int]:
    """从 flow 得到 (主机名小写, 端口)。HTTP 非标准端口在 request.port 或 Host: ip:port 中。"""
    try:
        hraw = (flow.request.host or "").strip()
        port = int(getattr(flow.request, "port", 0) or 0)
    except (TypeError, ValueError):
        hraw, port = "", 0
    h = hraw.lower().rstrip(".")
    if ":" in h and not port:
        try:
            head, tail = h.rsplit(":", 1)
            if tail.isdigit():
                h = head.rstrip(".").lower()
                port = int(tail)
        except ValueError:
            h = h.split(":")[0].lower().rstrip(".")
    if not h:
        u = urlparse(flow.request.pretty_url)
        h = (u.hostname or "").lower().rstrip(".")
        if u.port is not None:
            port = int(u.port)
        else:
            port = 443 if (u.scheme or "").lower() == "https" else 80
    if port <= 0:
        port = 80
    return h, port


def _request_match_tokens(flow) -> set[str]:
    """与 MITM_REDIRECT_HOSTS 比较的 token：纯主机名 + host:port（便于匹配 8081/8082 等不同端口）。"""
    h, port = _request_host_port(flow)
    out: set[str] = set()
    if h:
        out.add(h)
        out.add(f"{h}:{port}")
    return out


def _host_matches(flow) -> bool:
    raw = _redirect_hosts_raw()
    if not raw:
        return False
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if any(p in ("*", "all", "any") for p in parts):
        return True
    wanted = {p.rstrip(".") for p in parts}
    return bool(_request_match_tokens(flow) & wanted)


def _url_path(flow) -> str:
    return urlparse(flow.request.pretty_url).path or "/"


def _url_query(flow) -> dict[str, list[str]]:
    return parse_qs(urlparse(flow.request.pretty_url).query, keep_blank_values=True)


def _query_first(flow, *keys: str, default: str = "") -> str:
    q = _url_query(flow)
    for k in keys:
        if k in q and q[k]:
            v = unquote(q[k][0]).strip()
            # 自动解密：值若是 Fernet token 形态，先解出原文，否则原样返回
            v = _unobfuscate(v)
            if v or not default:
                return v
    return default


def _safe_child(root: Path, rel: str) -> Path | None:
    rel = (rel or "").replace("\\", "/").strip().lstrip("/")
    if ".." in rel.split("/"):
        return None
    if not rel:
        return root
    cand = (root / rel).resolve()
    try:
        cand.relative_to(root)
    except ValueError:
        return None
    return cand


def _rel_of(p: Path) -> str:
    root = _share_root()
    try:
        return str(p.resolve().relative_to(root)).replace("\\", "/")
    except ValueError:
        return p.name


# --------- URL 路径加密（避免 Pad 网络抓包看出请求内容） ---------
# 由于 Pad 没法装 CA 证书，HTTPS 走不通；这里退而求其次：把 URL 里
# 暴露源路径的 `path=...` 之类参数值用 Fernet 加密成不可读 token。
# 示例：/pdf?path=PDF/秘密.pdf  →  /pdf?path=gAAAAABm...
# 解密失败的情况下（旧链接/明文）原样使用，向后兼容。
_URL_OBFUSCATE = (os.environ.get("MITM_URL_OBFUSCATE", "1") or "1").strip().lower() not in ("0", "false", "no", "off", "")
_URL_FERNET_REF: dict = {}


def _get_url_fernet():
    if "f" in _URL_FERNET_REF:
        return _URL_FERNET_REF["f"]
    try:
        from cryptography.fernet import Fernet  # type: ignore
    except ImportError:
        _URL_FERNET_REF["f"] = None
        return None
    secret = (os.environ.get("MITM_URL_SECRET", "") or "").strip()
    if not secret:
        sec_file = _BASE / ".url_secret"
        try:
            if sec_file.is_file():
                secret = sec_file.read_text(encoding="utf-8").strip()
        except OSError:
            secret = ""
        if not secret:
            secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            try:
                sec_file.write_text(secret, encoding="utf-8")
            except OSError:
                pass
    key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    _URL_FERNET_REF["f"] = Fernet(key)
    return _URL_FERNET_REF["f"]


def _obfuscate(s: str) -> str:
    if not s or not _URL_OBFUSCATE:
        return s
    f = _get_url_fernet()
    if f is None:
        return s
    try:
        return f.encrypt(s.encode("utf-8")).decode("ascii")
    except Exception:  # noqa: BLE001
        return s


def _unobfuscate(s: str) -> str:
    """看起来像 Fernet token 就尝试解密；否则原样返回（兼容旧明文链接）。"""
    if not s:
        return s
    f = _get_url_fernet()
    if f is None:
        return s
    # Fernet v0x80 token 必以 "gAAAAA" 开头（base64 of [0x80, 4 bytes timestamp]）；
    # 用此特征快速短路，避免对每个普通参数都做一次失败的 base64 解码。
    if not s.startswith("gAAAAA"):
        return s
    try:
        return f.decrypt(s.encode("ascii"), ttl=None).decode("utf-8")
    except Exception:  # noqa: BLE001
        return s


def _q(p: Path | str) -> str:
    """生成 URL 中的 path 参数值；启用混淆时返回加密 token，否则 urlencoded 路径。"""
    rel = _rel_of(p) if isinstance(p, Path) else p
    if _URL_OBFUSCATE:
        return quote(_obfuscate(rel))
    return quote(rel)


def _ensure_upload_dir() -> Path:
    root = _share_root()
    d = root / _DIR_UPLOAD
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _ensure_private_dir() -> Path:
    root = _share_root()
    d = root / _DIR_PRIVATE
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _current_path_query(flow) -> str:
    u = urlparse(flow.request.pretty_url)
    p = u.path or "/"
    return p + (("?" + u.query) if u.query else "")


def _login_redirect_response(flow, next_url: str) -> Response:
    if not next_url.startswith("/"):
        next_url = "/"
    loc = f"/__login?next={quote(next_url, safe='/?:=&%')}"
    return Response.make(302, b"", {"Location": loc, "Cache-Control": "no-store"})


def _public_paths() -> frozenset[str]:
    return frozenset({
        "/__login", "/__logout",
        "/__mitm-exit", "/__mitm-exit-telemetry", "/__mitm-trap",
    })


def _rel_access_allowed(ctx: user_auth.UserCtx, rel: str) -> bool:
    """相对 payload 根的路径：能否「进入」该路径（浏览/打开文件）。规则与 /browse 一致。"""
    r = (rel or "").replace("\\", "/").strip().lstrip("/")
    if not r or r == ".":
        return user_auth.feature_allowed(ctx, "fe_browse")
    if r == _DIR_PRIVATE or r.startswith(_DIR_PRIVATE + "/"):
        return user_auth.can_browse_private_dir(ctx)
    if r == _DIR_UPLOAD or r.startswith(_DIR_UPLOAD + "/"):
        return user_auth.can_browse_upl_dir(ctx)
    if r == _DIR_PDF or r.startswith(_DIR_PDF + "/"):
        return user_auth.feature_allowed(ctx, "fe_pdf") or user_auth.feature_allowed(ctx, "fe_browse")
    if r == _DIR_VIDEO or r.startswith(_DIR_VIDEO + "/"):
        return user_auth.feature_allowed(ctx, "fe_video") or user_auth.feature_allowed(ctx, "fe_browse")
    if r == _DIR_MUSIC or r.startswith(_DIR_MUSIC + "/"):
        return user_auth.feature_allowed(ctx, "fe_music") or user_auth.feature_allowed(ctx, "fe_browse")
    return user_auth.feature_allowed(ctx, "fe_browse")


def _feature_gate_response(flow, path: str, ctx: user_auth.UserCtx) -> Response | None:
    """已登录：按角色与功能开关拦截。"""
    if ctx.banned:
        return _error_page("该账号已封禁。", status=403)
    if (path == "/__admin" or path.startswith("/__admin/")) and not ctx.is_admin:
        return _error_page("需要管理员权限。", status=403)
    if path == "/":
        if not user_auth.feature_allowed(ctx, "fe_home"):
            return _error_page("无权访问首页。", status=403)
        return None
    if path == "/music":
        if not user_auth.feature_allowed(ctx, "fe_music"):
            return _error_page("无权使用音乐播放器。", status=403)
        return None
    if path == "/upload":
        if not user_auth.feature_allowed(ctx, "fe_upload"):
            return _error_page("无权使用上传。", status=403)
        return None
    if path == "/browse":
        rel = _query_first(flow, "path", "dir", "mitm_path")
        r = (rel or "").replace("\\", "/").strip().lstrip("/")
        if not _rel_access_allowed(ctx, r):
            return _error_page("无权浏览此路径。分区入口与全库浏览已分开，请让管理员在功能分配中检查对应项。", status=403)
        return None
    if path == "/pdf":
        if not user_auth.feature_allowed(ctx, "fe_pdf"):
            return _error_page("无权使用 PDF 阅读器。", status=403)
        p = _resolve_path_from_query(flow)
        if p is not None and p.is_file() and not _rel_access_allowed(ctx, _rel_of(p)):
            return _error_page("无权访问此 PDF 所在路径。", status=403)
        return None
    if path in ("/video", "/subtitle"):
        if not user_auth.feature_allowed(ctx, "fe_video"):
            return _error_page("无权使用视频/音频播放。", status=403)
        p = _resolve_path_from_query(flow)
        if p is not None and p.is_file() and not _rel_access_allowed(ctx, _rel_of(p)):
            return _error_page("无权访问此媒体所在路径。", status=403)
        return None
    if path == "/image":
        if not user_auth.feature_allowed(ctx, "fe_image"):
            return _error_page("无权查看图片。", status=403)
        p = _resolve_path_from_query(flow)
        if p is not None and p.is_file() and not _rel_access_allowed(ctx, _rel_of(p)):
            return _error_page("无权访问此图片所在路径。", status=403)
        return None
    if path == "/text":
        if not user_auth.feature_allowed(ctx, "fe_text"):
            return _error_page("无权查看文本。", status=403)
        p = _resolve_path_from_query(flow)
        if p is not None and p.is_file() and not _rel_access_allowed(ctx, _rel_of(p)):
            return _error_page("无权访问此文件所在路径。", status=403)
        return None
    if path == "/pdf.png":
        if not user_auth.feature_allowed(ctx, "fe_pdf"):
            return _error_page("无权使用 PDF 渲染。", status=403)
        p = _resolve_path_from_query(flow)
        if p is not None and p.is_file() and not _rel_access_allowed(ctx, _rel_of(p)):
            return _error_page("无权访问此 PDF 所在路径。", status=403)
        return None
    if path in ("/file", "/open"):
        p = _resolve_path_from_query(flow)
        if p is None or not p.exists():
            return None
        rpath = _rel_of(p)
        if not _rel_access_allowed(ctx, rpath):
            return _error_page("无权访问此路径。", status=403)
        if p.is_file():
            k = _classify(p)
            fe_map = {
                "pdf": "fe_pdf", "video": "fe_video", "audio": "fe_video", "image": "fe_image", "text": "fe_text",
            }
            fk = fe_map.get(k)
            if fk and not user_auth.feature_allowed(ctx, fk):
                return _error_page("无权访问该类型资源。", status=403)
        return None
    return None


def _auth_gate_response(flow, path: str) -> Response | None:
    """未登录重定向；已登录做功能/目录权限。"""
    if path in _public_paths():
        return None
    ctx = user_auth.get_user_ctx_from_flow(flow)
    if ctx is None:
        return _login_redirect_response(flow, _current_path_query(flow))
    g = _feature_gate_response(flow, path, ctx)
    if g is not None:
        return g
    return None


def _login_response(flow) -> Response:
    next_url = _query_first(flow, "next") or "/"
    if not next_url.startswith("/"):
        next_url = "/"
    if user_auth.get_user_ctx_from_flow(flow) is not None and flow.request.method.upper() == "GET":
        h = {"Location": next_url, "Cache-Control": "no-store"}
        return Response.make(303, b"", h)
    method = flow.request.method.upper()
    err = ""
    if method == "POST":
        u_in, pw_in = "", ""
        try:
            ct = (flow.request.headers.get("Content-Type") or "").lower()
            if "application/x-www-form-urlencoded" in ct:
                body = flow.request.get_text() or ""
                q = parse_qs(body)
                u_in = (q.get("username") or q.get("user") or [""])[0].strip()
                pw_in = (q.get("password") or q.get("pwd") or [""])[0]
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            ip = flow.client_conn.peername[0] if getattr(flow, "client_conn", None) else ""
        except (AttributeError, IndexError, TypeError):
            ip = ""
        try:
            ua = (flow.request.headers.get("User-Agent") or "")[:200]
        except (AttributeError, TypeError):
            ua = ""
        sid, emsg = user_auth.login_user(u_in, pw_in or "", ip=ip, ua=ua)
        if sid:
            h3 = {
                "Location": next_url,
                "Set-Cookie": user_auth.set_session_headers(sid),
                "Cache-Control": "no-store",
            }
            return Response.make(303, b"", h3)
        err = (emsg or "登录失败")
    # 登录页极简：仅账号 + 密码 + 登录按钮（错误时一行红字）。
    # 性能优化：禁用全站背景噪点层（body::after），登录页不需要那点纹理；
    # 同时给登录卡 contain:layout paint，隔离重绘范围。
    body = f"""
<style>
body::after{{display:none!important}}
.login-wrap{{display:flex;align-items:center;justify-content:center;min-height:90vh;min-height:90dvh;padding:18px;box-sizing:border-box}}
.login-card{{position:relative;max-width:340px;width:100%;padding:22px 22px 18px;border-radius:var(--radius-lg);
  background:linear-gradient(160deg,rgba(95,161,255,.14),rgba(185,124,255,.12) 60%,rgba(255,122,168,.08));
  border:1px solid rgba(255,255,255,.18);
  box-shadow:var(--shadow-lg), inset 0 1px 0 rgba(255,255,255,.12);
  backdrop-filter:blur(28px) saturate(1.5);-webkit-backdrop-filter:blur(28px) saturate(1.5);
  contain:layout paint;will-change:transform;overflow:hidden}}
.login-card:before{{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:radial-gradient(60% 60% at 0% 0%,rgba(255,255,255,.12),transparent 70%)}}
.login-err{{text-align:center;color:#ff9aa2;font-size:.86rem;margin:0 0 10px;padding:7px 12px;
  background:rgba(255,90,90,.1);border:1px solid rgba(255,90,90,.25);border-radius:10px}}
.login-card form{{display:flex;flex-direction:column;gap:10px;margin:0}}
.login-card .input{{width:100%;box-sizing:border-box}}
.login-card .btn{{width:100%;margin-top:6px}}
</style>
<div class="content login-wrap">
  <div class="login-card">
    {f'<div class="login-err">{html.escape(err)}</div>' if err else ""}
    <form method="post" action="/__login" autocomplete="off">
      <input type="hidden" name="next" value="{html.escape(next_url)}">
      <input class="input" type="text" name="username" required autocomplete="off" inputmode="text" placeholder="账号" aria-label="账号">
      <input class="input" type="password" name="password" required autocomplete="off" placeholder="密码" aria-label="密码">
      <button type="submit" class="btn btn-primary">登 录</button>
    </form>
  </div>
</div>"""
    return _html_response(_shell("登录", body, show_splash_fab=False, exit_telemetry=False, mini_player=False))


def _logout_response(flow) -> Response:
    user_auth.logout_from_flow(flow)
    h = {
        "Location": "/__login",
        "Set-Cookie": user_auth.clear_session_cookie(),
        "Cache-Control": "no-store",
    }
    return Response.make(303, b"", h)


_FE_LABELS: dict[str, str] = {
    "fe_home": "首页",
    "fe_pdf": "PDF",
    "fe_video": "视频/音频",
    "fe_music": "音乐",
    "fe_upload": "上传",
    "fe_browse": "全库/根与未单独授权的路径（关此项仍可只开下面 PDF/视频/音乐 分区）",
    "fe_upl": "浏览上传目录 u/",
    "fe_private": "访问私密并显示入口",
    "fe_image": "图片",
    "fe_text": "文本",
}


# --------- 用户探针：记录每个用户最近在干什么 ---------
_USER_ACTIVITY: dict[str, dict] = {}
_USER_ACTIVITY_LOCK = threading.Lock()
_PROBE_MAX_RECENT = 8  # 每用户保留最近 N 条访问


def _activity_label_for_path(path: str, query: str, decoded_path: str) -> str:
    """给一条请求生成「用户能看懂」的活动描述。"""
    p = (path or "/").strip()
    short = decoded_path or ""
    if short and short.startswith("payload/"):
        short = short[len("payload/"):]
    if p == "/" or p == "":
        return "🏠 首页"
    if p == "/browse":
        return f"📂 浏览 {short or '根目录'}"
    if p == "/pdf":
        return f"📕 阅读 PDF：{short or '?'}"
    if p == "/pdf.png":
        return f"📕 PDF 翻页：{short or '?'}"
    if p == "/video":
        return f"🎬 观看视频：{short or '?'}"
    if p.startswith("/hls/") or p == "/video_trans_status" or p == "/video_trans_jump":
        return f"🎬 视频转码/播放：{short or '?'}"
    if p == "/video_trans_session":
        return "🎬 视频心跳"
    if p == "/music":
        return "🎵 音乐播放器"
    if p == "/music_tracks":
        return "🎵 加载音乐列表"
    if p == "/subtitle" or p == "/subtitle_internal":
        return f"💬 字幕：{short or '?'}"
    if p == "/image":
        return f"🖼 看图：{short or '?'}"
    if p == "/text":
        return f"📝 看文本：{short or '?'}"
    if p == "/open":
        return f"📂 打开：{short or '?'}"
    if p == "/file":
        return f"⬇ 下载/读取：{short or '?'}"
    if p == "/upload":
        return "📤 上传"
    if p == "/pdf_progress":
        return "📕 同步阅读进度"
    if p.startswith("/__admin"):
        return f"🔧 管理面板：{p}"
    return f"{p}"


def _track_user_activity(flow, ctx) -> None:
    """请求处理后调用：把这次访问归档到该用户的活动记录里。"""
    try:
        if not ctx or not getattr(ctx, "username", ""):
            return
        un = ctx.username
        path = _url_path(flow) or "/"
        # 避免高频 ping 类接口刷屏
        if path in ("/video_trans_status", "/__mitm-exit-telemetry", "/video_trans_session"):
            return
        # 解码 path 参数（同时兼容 Fernet token）作为可读标签
        decoded = _query_first(flow, "path", "f", "file", "v", "open", "mitm_open") or ""
        try:
            ip = flow.client_conn.peername[0] if getattr(flow, "client_conn", None) else ""
        except (AttributeError, IndexError, TypeError):
            ip = ""
        try:
            ua = (flow.request.headers.get("User-Agent") or "")[:160]
        except (AttributeError, TypeError):
            ua = ""
        now = time.time()
        label = _activity_label_for_path(path, "", decoded)
        with _USER_ACTIVITY_LOCK:
            d = _USER_ACTIVITY.setdefault(un, {"recent": []})
            d["last_at"] = now
            d["last_path"] = path
            d["last_label"] = label
            d["last_ip"] = ip
            d["last_ua"] = ua
            recent = d.setdefault("recent", [])
            # 同 label 在 30 秒内合并次数
            if recent and recent[-1].get("label") == label and now - recent[-1].get("at", 0) < 30:
                recent[-1]["at"] = now
                recent[-1]["count"] = recent[-1].get("count", 1) + 1
            else:
                recent.append({"at": now, "label": label, "path": path})
                if len(recent) > _PROBE_MAX_RECENT:
                    del recent[: len(recent) - _PROBE_MAX_RECENT]
    except Exception:  # noqa: BLE001
        pass


def _admin_activity_data() -> dict:
    """汇总所有已知用户的活动 + 当前会话信息（给管理员面板用）。"""
    sessions = user_auth.list_user_sessions()
    by_user: dict[str, list[dict]] = {}
    for s in sessions:
        by_user.setdefault(s["username"], []).append(s)
    out_users: list[dict] = []
    all_data = user_auth.admin_list_actions()
    user_records: dict = all_data.get("users") or {}
    seen = set()
    with _USER_ACTIVITY_LOCK:
        snapshot = {un: dict(meta) for un, meta in _USER_ACTIVITY.items()}
    for un in user_records.keys():
        seen.add(un)
        meta = snapshot.get(un) or {}
        sess = by_user.get(un) or []
        out_users.append({
            "username": un,
            "online": bool(sess),
            "session_count": len(sess),
            "session_ip": (sess[0]["ip"] if sess else ""),
            "session_ua": (sess[0]["ua"] if sess else ""),
            "session_since": (sess[0]["created"] if sess else 0),
            "last_seen": (sess[0]["last_seen"] if sess else 0),
            "last_label": meta.get("last_label", ""),
            "last_path": meta.get("last_path", ""),
            "last_at": meta.get("last_at", 0),
            "last_ip": meta.get("last_ip", ""),
            "recent": meta.get("recent") or [],
            "role": (user_records[un] or {}).get("role") or "user",
            "banned": bool((user_records[un] or {}).get("banned")),
        })
    # 已登出但仍在 _USER_ACTIVITY 里的也展示（可能有最近活动但 cookie 过期）
    for un in snapshot.keys():
        if un in seen:
            continue
        meta = snapshot[un]
        out_users.append({
            "username": un, "online": False, "session_count": 0,
            "session_ip": "", "session_ua": "", "session_since": 0, "last_seen": 0,
            "last_label": meta.get("last_label", ""), "last_path": meta.get("last_path", ""),
            "last_at": meta.get("last_at", 0), "last_ip": meta.get("last_ip", ""),
            "recent": meta.get("recent") or [], "role": "user", "banned": False,
        })
    out_users.sort(key=lambda x: (-int(x.get("online") or False), -float(x.get("last_at") or 0)))
    return {"now": time.time(), "users": out_users}


def _admin_activity_response(flow) -> Response:
    ctx0 = user_auth.get_user_ctx_from_flow(flow)
    if not ctx0 or not ctx0.is_admin:
        return Response.make(403, b'{"error":"admin required"}',
                             {"Content-Type": "application/json"})
    if flow.request.method.upper() == "POST":
        try:
            body = flow.request.get_text() or ""
            q = parse_qs(body)
            action = (q.get("action") or [""])[0]
            target = (q.get("target") or [""])[0].strip()
            if action == "kick" and target:
                user_auth.kick_user(target)
        except (OSError, TypeError, ValueError):
            pass
        return Response.make(303, b"",
                             {"Location": "/__admin", "Cache-Control": "no-store"})
    body = json.dumps(_admin_activity_data(), ensure_ascii=False).encode("utf-8")
    return Response.make(200, body, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    })


def _admin_trans_list_data() -> dict:
    """管理员看的转码列表：包含运行中任务 + 缓存目录已落盘的视频。"""
    cache_root = _trans_cache_dir()
    items = []
    seen = set()
    for key, job in list(_TRANS_JOBS.items()):
        proc = job.get("proc")
        running = (proc is not None and proc.poll() is None)
        out_dir = Path(job.get("out_dir") or (cache_root / key))
        src_ref = out_dir / "source.ref"
        src = src_ref.read_text(encoding="utf-8").strip() if src_ref.is_file() else ""
        items.append({
            "key": key, "src": src, "running": running,
            "progress": int(job.get("progress", 0)),
            "viewers": _trans_viewers_count(key),
            "ts_count": _trans_count_ts(Path(src)) if src else 0,
            "done": (out_dir / "DONE").is_file(),
        })
        seen.add(key)
    if cache_root.is_dir():
        try:
            for d in cache_root.iterdir():
                if not d.is_dir() or d.name in seen:
                    continue
                src_ref = d / "source.ref"
                if not src_ref.is_file():
                    continue
                src = src_ref.read_text(encoding="utf-8").strip()
                p = Path(src)
                items.append({
                    "key": d.name, "src": src, "running": False,
                    "progress": 100 if (d / "DONE").is_file() else 0,
                    "viewers": _trans_viewers_count(d.name),
                    "ts_count": _trans_count_ts(p) if p.is_file() else 0,
                    "done": (d / "DONE").is_file(),
                })
        except OSError:
            pass
    items.sort(key=lambda x: (-int(x["running"]), x["src"].lower()))
    return {"items": items, "max_concurrent": _MAX_CONCURRENT_TRANS}


def _admin_trans_response(flow) -> Response:
    """管理员转码任务面板 + 操作入口。"""
    ctx0 = user_auth.get_user_ctx_from_flow(flow)
    if not ctx0 or not ctx0.is_admin:
        return _error_page("需要管理员权限。", status=403)

    if flow.request.method.upper() == "POST":
        try:
            ct = (flow.request.headers.get("Content-Type") or "").lower()
            body = flow.request.get_text() or ""
            q = parse_qs(body) if "application/x-www-form-urlencoded" in ct else {}
            action = (q.get("action") or [""])[0]
            key = (q.get("key") or [""])[0]
            if action == "stop" and key:
                _trans_kill_job(key)
            elif action == "clear" and key:
                _trans_kill_job(key)
                with _TRANS_VIEWERS_LOCK:
                    _TRANS_VIEWERS.pop(key, None)
                import shutil as _sh
                try:
                    _sh.rmtree(_trans_cache_dir() / key, ignore_errors=True)
                except OSError:
                    pass
        except (OSError, TypeError, ValueError):
            pass
        # POST 后重定向到 GET，避免刷新重提交
        return Response.make(303, b"", {"Location": "/__admin/trans", "Cache-Control": "no-store"})

    data = _admin_trans_list_data()
    rows = []
    for it in data["items"]:
        src = html.escape(it.get("src") or "")
        key = html.escape(it.get("key") or "")
        if it["running"]:
            status = f'<span style="color:#9dd1ff">运行中 · {it["progress"]}%</span>'
        elif it["done"]:
            status = '<span style="color:#9af">已完成 · 100%</span>'
        else:
            status = f'<span style="color:#bbb">已缓存 {it["ts_count"]} 段</span>'
        rows.append(f"""
<tr>
  <td class="src">{src or '<span class="muted">(未知源)</span>'}</td>
  <td>{status}</td>
  <td style="text-align:center">{it["viewers"]}</td>
  <td class="ops">
    <form method="post" style="display:inline-block;margin:0 4px 0 0">
      <input type="hidden" name="action" value="stop">
      <input type="hidden" name="key" value="{key}">
      <button class="btn btn-ghost btn-sm" type="submit"{' disabled' if not it["running"] else ''}>停止</button>
    </form>
    <form method="post" style="display:inline-block" onsubmit="return confirm('删除该视频的全部转码缓存？');">
      <input type="hidden" name="action" value="clear">
      <input type="hidden" name="key" value="{key}">
      <button class="btn btn-ghost btn-sm" type="submit" style="color:#ff9aa2">清缓存</button>
    </form>
  </td>
</tr>""")
    table = "".join(rows) or '<tr><td colspan="4" class="muted" style="text-align:center;padding:20px">暂无转码任务或缓存。</td></tr>'

    body = f"""
<div class="topbar">
  <span class="brand">转码任务</span>
  <span class="spacer"></span>
  <span class="muted" style="margin-right:8px">并发上限 {data['max_concurrent']}</span>
  <a class="btn btn-ghost btn-sm" href="/__admin">用户管理</a>
  <a class="btn btn-ghost btn-sm" href="/">返回首页</a>
</div>
<div class="content">
  <div class="card" style="padding:0;overflow:auto">
    <table class="files" style="margin:0">
      <thead>
        <tr><th>视频源</th><th style="width:160px">状态</th><th style="width:70px">观众</th><th style="width:200px">操作</th></tr>
      </thead>
      <tbody>{table}</tbody>
    </table>
  </div>
  <div class="card">
    <p class="muted" style="margin:0">
      • <strong>同一视频同时观众数无上限</strong>：所有人共享一个 ffmpeg + 一份磁盘缓存，多人观看不会增加 CPU 负担。<br>
      • <strong>不同视频</strong>同时转码上限 {data['max_concurrent']} 个；用 <code>MITM_MAX_TRANS=4</code> 环境变量调整。<br>
      • 缓存机制：边转边播 → 全部观众离开 30 秒后自动停止 ffmpeg；磁盘缓存保留，下次秒开。<br>
      • HLS 分段（.ts）一旦写完即标记为 <code>immutable</code>，浏览器/上游 CDN 强缓存（多人重复播放只占带宽不占 CPU）。<br>
      • 字幕：打开视频页时后台异步预抽全部内封文本字幕到磁盘；切换字幕轨秒载，不再触发新的 ffmpeg。<br>
      • 编码器（默认）：<code>libx264 ultrafast tune=fastdecode crf23 maxrate=1500k 540p HLS段=2s</code>；可设 <code>MITM_TRANS_VENC=h264_v4l2m2m</code> 启硬编。<br>
      • 还卡？再降档：<code>MITM_TRANS_HEIGHT=360 MITM_TRANS_MAXRATE=900k MITM_TRANS_ABR=64k</code>；带宽足想更清晰：<code>MITM_TRANS_HEIGHT=720 MITM_TRANS_MAXRATE=2400k MITM_TRANS_CRF=22</code>。<br>
      • HLS 段长：<code>MITM_TRANS_SEG=2</code>（默认，首屏快）/ <code>MITM_TRANS_SEG=4</code>（请求少，但首段慢）。<br>
      • 「清缓存」会删除该视频对应的整个 HLS 段目录，下次播放会重新转码。
    </p>
  </div>
</div>"""
    return _html_response(_shell("转码任务", body))


def _admin_response(flow) -> Response:
    ctx0 = user_auth.get_user_ctx_from_flow(flow)
    if not ctx0 or not ctx0.is_admin:
        return _error_page("需要管理员权限。", status=403)
    notice = ""
    if flow.request.method.upper() == "POST":
        try:
            ct = (flow.request.headers.get("Content-Type") or "").lower()
            if "application/x-www-form-urlencoded" in ct:
                body = flow.request.get_text() or ""
                q = parse_qs(body)
                action = (q.get("action") or [""])[0]
                if action == "create":
                    uu = (q.get("new_username") or [""])[0].strip()
                    _ok, notice = user_auth.admin_create_user(
                        uu,
                        (q.get("new_password") or [""])[0],
                        (q.get("new_role") or ["user"])[0].strip() or "user",
                    )
                elif action == "set_private":
                    raw = (q.get("private_users") or [""])[0]
                    names = [x.strip() for x in re.split(r"[\s,，;；\n]+", raw) if x.strip()]
                    ok, notice = user_auth.admin_set_private_list(names)
                elif action == "update_user":
                    target = (q.get("target") or [""])[0].strip()
                    feats: dict[str, bool] = {}
                    for k in user_auth.FE_KEYS:
                        feats[k] = (q.get(k) or [""])[0] == "on"
                    role_in = (q.get("role") or [""])[0].strip()
                    banned_in = (q.get("banned") or [""])[0] == "1"
                    pwd_in = (q.get("newpw") or [""])[0]
                    pwd_arg = None
                    if len(pwd_in) >= 4:
                        pwd_arg = pwd_in
                    elif 0 < len(pwd_in) < 4:
                        notice = "密码过短，未修改密码"
                    _ok, n2 = user_auth.admin_update_user(
                        target,
                        role=role_in if role_in in ("admin", "user") else None,
                        banned=banned_in,
                        features=feats,
                        password=pwd_arg,
                    )
                    if not (0 < len(pwd_in) < 4):
                        notice = n2
                elif action == "delete_user":
                    _ok, notice = user_auth.admin_delete_user(
                        (q.get("target") or [""])[0].strip(), ctx0.username,
                    )
        except (OSError, TypeError, ValueError) as e:
            notice = f"操作异常：{e!r}"
    data = user_auth.admin_list_actions()
    users: dict = data.get("users") or {}
    priv: list = data.get("private_access_users") or []
    priv_txt = "\n".join(priv)

    def _user_block(un: str, rec: dict) -> str:
        fe = {**user_auth.DEFAULT_FEATURES, **(rec.get("features") or {})}
        cbs = "\n".join(
            f'<label style="display:inline-block;margin:4px 8px 4px 0">'
            f'<input type="checkbox" name="{k}" value="on"{" checked" if fe.get(k, True) else ""}> '
            f'{html.escape(_FE_LABELS.get(k, k))}</label>'
            for k in user_auth.FE_KEYS
        )
        is_admin = (rec.get("role") or "") == "admin"
        is_banned = bool(rec.get("banned"))
        sel_user = " selected" if not is_admin else ""
        sel_adm = " selected" if is_admin else ""
        sel_b0 = " selected" if not is_banned else ""
        sel_b1 = " selected" if is_banned else ""
        return f"""
<div class="card" style="margin-bottom:16px">
  <h3 style="margin:0 0 8px 0;font-size:1rem">用户 {html.escape(un)}</h3>
  <form method="post" class="row" style="flex-wrap:wrap;align-items:flex-start">
    <input type="hidden" name="action" value="update_user">
    <input type="hidden" name="target" value="{html.escape(un)}">
    <p class="row" style="width:100%;margin:0 0 6px 0;gap:10px;flex-wrap:wrap">
      <span>角色</span>
      <select name="role" class="input" style="width:auto">
        <option value="user"{sel_user}>普通</option>
        <option value="admin"{sel_adm}>管理员</option>
      </select>
      <span>封禁</span>
      <select name="banned" class="input" style="width:auto">
        <option value="0"{sel_b0}>否</option>
        <option value="1"{sel_b1}>是</option>
      </select>
    </p>
    <p style="width:100%;margin:0 0 4px 0" class="muted">功能</p>
    <div style="width:100%;line-height:1.6;margin-bottom:8px">{cbs}</div>
    <p class="row" style="width:100%;flex-wrap:wrap;gap:8px">
      <input class="input" type="password" name="newpw" placeholder="新密码（可空，至少 4 位）" style="flex:1;min-width:200px" autocomplete="new-password">
      <button class="btn btn-primary" type="submit">保存该用户</button>
    </p>
  </form>
  <form method="post" style="margin-top:8px" onsubmit="return confirm('确定删除该用户？');">
    <input type="hidden" name="action" value="delete_user">
    <input type="hidden" name="target" value="{html.escape(un)}">
    <button class="btn btn-ghost" type="submit" style="color:#ff9aa2">删除用户</button>
  </form>
</div>"""

    rows = "".join(_user_block(u, m) for u, m in sorted(users.items(), key=lambda x: x[0].lower()))
    admin_css = r"""
.adm-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 12px}
.adm-tabs button{padding:8px 14px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.05);color:var(--fg);font-weight:600;font-size:.9rem}
.adm-tabs button.on{background:linear-gradient(180deg,#4f8fff,#3a7ae8);color:#fff;border-color:transparent}
.adm-pane{display:none}
.adm-pane.on{display:block}
.adm-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.adm-grid .card{margin:0}
.probe-card{display:flex;flex-direction:column;gap:6px;padding:12px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.04)}
.probe-card .row1{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.probe-dot{width:10px;height:10px;border-radius:999px;background:#666;display:inline-block;flex:0 0 10px}
.probe-dot.on{background:#5fc36b;box-shadow:0 0 0 3px rgba(95,195,107,.18)}
.probe-card .name{font-weight:700;font-size:1rem}
.probe-card .role{font-size:.7rem;padding:1px 6px;border-radius:6px;background:rgba(124,193,255,.18);color:#9dd1ff}
.probe-card .role.admin{background:rgba(255,170,80,.2);color:#ffce82}
.probe-card .role.banned{background:rgba(255,90,90,.2);color:#ffa6a6}
.probe-card .label{font-size:.85rem;color:#cdd5e3;line-height:1.4;word-break:break-all}
.probe-card .meta{font-size:.74rem;color:rgba(236,241,255,.55);line-height:1.45;word-break:break-all}
.probe-card .ops{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}
.probe-card .ops form{margin:0}
.probe-card .ops button{padding:4px 10px;font-size:.78rem;border-radius:8px}
.probe-recent{font-size:.72rem;color:rgba(236,241,255,.45);margin-top:4px}
.probe-recent details summary{cursor:pointer;color:rgba(236,241,255,.6)}
.probe-recent ul{list-style:none;padding:6px 0 0;margin:0}
.probe-recent li{padding:2px 0;border-bottom:1px dashed rgba(255,255,255,.06)}
.adm-mini-help{font-size:.78rem;color:rgba(236,241,255,.55);margin:4px 0 10px}
"""
    body = f"""
<style>{admin_css}</style>
<div class="topbar">
  <span class="brand">VerPadProxy 管理</span>
  <span class="spacer"></span>
  <span class="muted" style="margin-right:8px">{html.escape(ctx0.username)}</span>
  <a class="btn btn-ghost btn-sm" href="/__admin/trans">转码任务</a>
  <a class="btn btn-ghost btn-sm" href="/">返回首页</a>
  <a class="btn btn-ghost btn-sm" href="/__logout">退出登录</a>
</div>
<div class="content">
  {f'<div class="card" style="background:rgba(100,200,150,.1);border-color:var(--border)"><p style="margin:0">{html.escape(notice)}</p></div>' if notice else ''}
  <div class="adm-tabs" id="adm-tabs">
    <button data-pane="pane-probe" class="on">用户在线 / 探针</button>
    <button data-pane="pane-users">账号与权限</button>
    <button data-pane="pane-private">私密名单</button>
    <button data-pane="pane-create">新建用户</button>
  </div>

  <div class="adm-pane on" id="pane-probe">
    <p class="adm-mini-help">绿点 = 在线（最近 30s 有请求）。每 5 秒自动刷新；可一键踢下线。单设备登录已开启 (<code>MITM_SINGLE_DEVICE=1</code>)，新登录会自动顶替原设备。</p>
    <div class="adm-grid" id="probe-grid">
      <div class="card muted">加载中…</div>
    </div>
  </div>

  <div class="adm-pane" id="pane-users">
    <p class="adm-mini-help">每个用户一张卡片：可改角色、封禁、功能勾选、改密、删除。</p>
    {rows}
  </div>

  <div class="adm-pane" id="pane-private">
    <div class="card">
      <h2 style="margin:0 0 8px 0">可访问「{_DIR_PRIVATE}」的用户</h2>
      <p class="muted" style="margin:0 0 8px 0">每行一个用户名，或逗号分隔。管理员始终可访问。</p>
      <form method="post">
        <input type="hidden" name="action" value="set_private">
        <textarea class="input" name="private_users" rows="4" style="width:100%;min-height:80px;font-family:inherit" placeholder="admin">{html.escape(priv_txt)}</textarea>
        <button class="btn btn-primary" type="submit" style="margin-top:8px">保存名单</button>
      </form>
    </div>
  </div>

  <div class="adm-pane" id="pane-create">
    <div class="card">
      <h2 style="margin:0 0 10px 0">新建用户</h2>
      <form method="post" class="row" style="flex-wrap:wrap;gap:8px;align-items:flex-end">
        <input type="hidden" name="action" value="create">
        <input class="input" name="new_username" placeholder="用户名" required style="min-width:120px" autocomplete="off">
        <input class="input" name="new_password" type="password" placeholder="密码（>= 4 位）" required style="min-width:160px" autocomplete="new-password">
        <select name="new_role" class="input" style="width:auto">
          <option value="user">普通</option>
          <option value="admin">管理员</option>
        </select>
        <button class="btn btn-primary" type="submit">创建</button>
      </form>
    </div>
  </div>
</div>
<script>
(function(){{
  var tabs = document.querySelectorAll('#adm-tabs button');
  var panes = document.querySelectorAll('.adm-pane');
  var SAVED = 'mitm_admin_tab';
  function showTab(name){{
    panes.forEach(function(p){{ p.classList.toggle('on', p.id === name); }});
    tabs.forEach(function(t){{ t.classList.toggle('on', t.getAttribute('data-pane') === name); }});
    try{{ localStorage.setItem(SAVED, name); }}catch(e){{}}
  }}
  tabs.forEach(function(t){{ t.addEventListener('click', function(){{ showTab(t.getAttribute('data-pane')); }}); }});
  try{{ var s = localStorage.getItem(SAVED); if (s) showTab(s); }}catch(e){{}}

  var grid = document.getElementById('probe-grid');
  function fmtAgo(ts){{
    if (!ts) return '从未';
    var d = Math.max(0, Date.now()/1000 - ts);
    if (d < 60) return Math.floor(d) + ' 秒前';
    if (d < 3600) return Math.floor(d/60) + ' 分钟前';
    if (d < 86400) return Math.floor(d/3600) + ' 小时前';
    return Math.floor(d/86400) + ' 天前';
  }}
  function esc(s){{
    return (s == null ? '' : String(s)).replace(/[&<>"']/g, function(c){{
      return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];
    }});
  }}
  function renderUsers(data){{
    var users = (data && data.users) || [];
    if (!users.length){{ grid.innerHTML = '<div class="card muted">还没有用户活动记录。</div>'; return; }}
    var now = (data && data.now) || Date.now()/1000;
    grid.innerHTML = users.map(function(u){{
      var online = !!u.online && (now - (u.last_seen || 0)) < 90;
      var dot = online ? '<span class="probe-dot on" title="在线"></span>' : '<span class="probe-dot" title="离线"></span>';
      var role = u.banned ? '<span class="role banned">已封禁</span>' :
                 (u.role === 'admin' ? '<span class="role admin">管理员</span>' :
                  '<span class="role">普通</span>');
      var label = esc(u.last_label || '（无活动记录）');
      var ip = u.session_ip || u.last_ip || '';
      var ua = (u.session_ua || '').slice(0, 80);
      var recent = (u.recent || []).slice().reverse();
      var recentHtml = '';
      if (recent.length){{
        recentHtml = '<div class="probe-recent"><details><summary>最近 ' + recent.length + ' 条</summary><ul>'
          + recent.map(function(r){{ return '<li>' + fmtAgo(r.at) + ' · ' + esc(r.label) + (r.count>1 ? ' ×' + r.count : '') + '</li>'; }}).join('')
          + '</ul></details></div>';
      }}
      var kickBtn = online ? ('<form method="post" action="/__admin/activity">'
                  + '<input type="hidden" name="action" value="kick">'
                  + '<input type="hidden" name="target" value="' + esc(u.username) + '">'
                  + '<button class="btn btn-ghost" type="submit" style="color:#ff9aa2">踢下线</button></form>') : '';
      return '<div class="probe-card">'
        + '<div class="row1">' + dot + '<span class="name">' + esc(u.username) + '</span>' + role
        + '<span class="muted" style="margin-left:auto;font-size:.78rem">' + (online ? '在线 · ' : '') + fmtAgo(u.last_at) + '</span></div>'
        + '<div class="label">📌 ' + label + '</div>'
        + '<div class="meta">IP: ' + esc(ip) + (ua ? ' · UA: ' + esc(ua) : '') + '</div>'
        + recentHtml
        + '<div class="ops">' + kickBtn + '</div>'
        + '</div>';
    }}).join('');
  }}
  function refreshProbe(){{
    fetch('/__admin/activity', {{cache:'no-store',credentials:'include'}})
      .then(function(r){{ return r.json(); }})
      .then(renderUsers)
      .catch(function(){{}});
  }}
  refreshProbe();
  setInterval(refreshProbe, 5000);
}})();
</script>"""
    return _html_response(_shell("管理", body))


def _mitm_exit_page_response() -> Response:
    """/__mitm-exit 专页：关窗/黑底兜底；小方块优先走 mitmFabExit，此页为最后一跳。"""
    doc = (
        r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="Cache-Control" content="no-store">
<title>退出</title>
<style>html,body{margin:0;background:#000;min-height:100vh;min-height:100dvh}</style>
</head>
<body>
<script>
(function(){
  var turl;
  try{ turl = new URL("/__mitm-exit-telemetry", document.baseURI).href; }
  catch(et){ turl = location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-exit-telemetry"; }
  function tlog(ev){
    try{
      var u = turl + "?e=" + encodeURIComponent(ev) + "&t=" + Date.now() + "&p=__mitm-exit";
      if (navigator.sendBeacon) navigator.sendBeacon(u);
    }catch(x){}
  }
  tlog("exit_page_in");
  try{ sessionStorage.removeItem("mitmFabTrap"); }catch(se){}
  addEventListener("pagehide", function(){ tlog("exit_page_pagehide"); });
})();
</script>
<script>
(function(){
  var bhtml = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#000;min-height:100vh;min-height:100dvh"></body></html>';
  function go(){
    try{ window.close(); }catch(e0){}
    try{ self.close(); }catch(e1){}
    try{ if (window.top) window.top.close(); }catch(e2){}
    try{ location.replace("data:text/html;charset=utf-8,"+encodeURIComponent(bhtml)); }catch(x){
      try{ document.documentElement.style.background = "#000";
        document.body.style.cssText = "margin:0;background:#000;min-height:100vh;min-height:100dvh;";
      }catch(y){}
    }
  }
  go();
  [0, 40, 120, 300].forEach(function(t){ setTimeout(go, t); });
})();
</script>
</body>
</html>
""".strip()
    )
    return _html_response(doc.encode("utf-8"))


def _mitm_trap_page_response(flow) -> Response:
    """同源全黑页：用长脚本堆 pushState/定时补栈/拦截 popstate，比 data: URL 更易压住「返回」；next 仅允许指向 /__mitm-exit。"""
    nxt = _query_first(flow, "next") or "/__mitm-exit"
    if not isinstance(nxt, str) or not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/__mitm-exit"
    if nxt not in ("/__mitm-exit",) and not nxt.startswith("/__mitm-exit?"):
        nxt = "/__mitm-exit"
    next_js = json.dumps(nxt)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="Cache-Control" content="no-store">
<title></title>
<style>html,body{{margin:0;padding:0;background:#000;height:100%;min-height:100vh;min-height:100dvh;overflow:hidden;touch-action:none}}</style>
</head>
<body>
<script>
(function(){{
  var NEXT = {next_js};
  function oc() {{
    try{{ window.close(); }} catch(e) {{}}
    try{{ if (window.top) window.top.close(); }} catch(e2) {{}}
  }}
  oc();
  [0,30,100,300,800].forEach(function(t){{ setTimeout(oc, t); }});
  var H0 = (location.href || "").split("#")[0];
  function _bed(n) {{
    n = n || 32;
    var a = 0;
    while (a < n) {{
      try {{ history.pushState({{a:a,t:Date.now()}}, document.title, H0); a++; }} catch (e0) {{ a = 9999; }}
    }}
  }}
  try {{ history.replaceState(null, document.title, H0); }} catch (e) {{}}
  _bed(80);
  setTimeout(function(){{ _bed(32); }}, 0);
  window.addEventListener("popstate", function() {{
    _bed(56);
    setTimeout(function(){{ _bed(24); }}, 0);
    setTimeout(function(){{ _bed(20); }}, 8);
  }}, false);
  window.addEventListener("pageshow", function(ev) {{
    if (ev.persisted) _bed(40);
  }}, false);
  var _i = 0, _k = setInterval(function() {{
    _i += 1;
    if (_i > 60) {{ clearInterval(_k); return; }}
    _bed(2);
  }}, 200);
  function goN() {{
    try {{ if (window.top) {{ window.top.location.replace(NEXT); return; }} }} catch (e1) {{}}
    try {{ location.replace(NEXT); }} catch (e2) {{ try {{ location.href = NEXT; }} catch (e3) {{}} }}
  }}
  setTimeout(goN, 350);
  setTimeout(goN, 1000);
  setTimeout(goN, 2200);
}})();
</script>
</body>
</html>"""
    return _html_response(html.encode("utf-8"))


# ---------------------------------------------------------------------------
# 分类 / 格式化
# ---------------------------------------------------------------------------

def _classify(path: Path) -> str:
    s = path.suffix.lower()
    if s == ".pdf":
        return "pdf"
    if s in _VIDEO_EXTS:
        return "video"
    if s in _AUDIO_EXTS:
        return "audio"
    if s in _IMAGE_EXTS:
        return "image"
    if s in _TEXT_EXTS:
        return "text"
    if s in _SUB_EXTS:
        return "subtitle"
    if s in _ARCHIVE_EXTS:
        return "archive"
    return "binary"


def _icon_of(kind: str) -> str:
    return {
        "dir": "📁",
        "pdf": "📕",
        "video": "🎬",
        "audio": "🎵",
        "image": "🖼️",
        "text": "📝",
        "subtitle": "💬",
        "archive": "🗜️",
        "binary": "📄",
    }.get(kind, "📄")


def _fmt_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    i = 0
    while f >= 1024 and i < len(units) - 1:
        f /= 1024.0
        i += 1
    return f"{int(f)} {units[i]}" if i == 0 else f"{f:.2f} {units[i]}"


def _fmt_mtime(ts: float) -> str:
    import datetime as _dt
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "-"


def _guess_mime(path: Path) -> str:
    m, _ = mimetypes.guess_type(path.name)
    if m:
        return m
    k = _classify(path)
    return {
        "pdf": "application/pdf",
        "video": "video/mp4",
        "audio": "audio/mpeg",
        "image": "image/png",
        "text": "text/plain; charset=utf-8",
        "subtitle": "text/vtt; charset=utf-8",
    }.get(k, "application/octet-stream")


def _ascii_fallback(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"


def _content_disposition(name: str, *, inline: bool) -> str:
    kind = "inline" if inline else "attachment"
    return f'{kind}; filename="{_ascii_fallback(name)}"; filename*=UTF-8\'\'{quote(name)}'


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


# ---------------------------------------------------------------------------
# Range 二进制输出
# ---------------------------------------------------------------------------

def _parse_range(header: str, total: int) -> tuple[int, int] | None:
    if not header or not header.lower().startswith("bytes="):
        return None
    spec = header[6:].split(",", 1)[0].strip()
    if not spec:
        return None
    a, _, b = spec.partition("-")
    try:
        if not a:
            suffix = max(1, min(int(b), total))
            return total - suffix, total - 1
        start = max(0, int(a))
        end = min(total - 1, int(b)) if b else total - 1
        if start > end or start >= total:
            return None
        return start, end
    except (ValueError, TypeError):
        return None


def _binary_response(flow, path: Path, *, inline: bool, mime: str | None = None) -> Response:
    try:
        total = path.stat().st_size
    except OSError:
        return _error_page("无法读取文件。")
    ctype = mime or _guess_mime(path)
    method = flow.request.method.upper()
    range_hdr = flow.request.headers.get("Range", "")
    rng = _parse_range(range_hdr, total) if total > 0 else None

    if range_hdr.lower().startswith("bytes=") and rng is None:
        return Response.make(416, b"", {
            "Content-Range": f"bytes */{total}",
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        })

    if rng is None:
        start, end, status = 0, max(0, total - 1), 200
    else:
        start, end = rng
        status = 206
    length = end - start + 1 if total > 0 else 0

    headers = {
        "Content-Type": ctype,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
        "Content-Length": str(length),
        "Content-Disposition": _content_disposition(path.name, inline=inline),
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    if method == "HEAD" or length == 0:
        return Response.make(status, b"", headers)

    try:
        with path.open("rb") as f:
            f.seek(start)
            data = f.read(length)
    except OSError:
        return _error_page("文件读取失败。")
    return Response.make(status, data, headers)


# ---------------------------------------------------------------------------
# HTML shell
# ---------------------------------------------------------------------------

_BASE_CSS = r"""
:root{
  color-scheme:dark;
  --bg:#070a14;          /* 主底色（深夜蓝黑）*/
  --fg:#eaf0fa;
  --muted:#8b9bb4;
  --accent:#5fa1ff;
  --accent-2:#b97cff;     /* 副色，用于渐变与微光 */
  --accent-3:#ff7aa8;     /* 暖粉色，节庆点缀 */
  --line:rgba(255,255,255,.08);
  --line-strong:rgba(255,255,255,.16);
  --card-bg:rgba(20,26,40,.55);
  --card-bg-strong:rgba(20,26,40,.78);
  --card-blur:24px;
  --radius-sm:10px;
  --radius-md:14px;
  --radius-lg:20px;
  --shadow-sm:0 4px 14px rgba(0,0,0,.32);
  --shadow-md:0 14px 38px rgba(0,0,0,.42);
  --shadow-lg:0 28px 70px rgba(0,0,0,.52);
  --glow:0 0 40px rgba(95,161,255,.22);
}
*,*:before,*:after{box-sizing:border-box}
html,body{margin:0;padding:0;color:var(--fg);
  font-family:-apple-system,system-ui,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-tap-highlight-color:transparent;touch-action:manipulation;
  background:var(--bg);
  text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
body{min-height:100vh;min-height:100dvh;position:relative;overflow-x:hidden}
/* 极光底纹：固定不滚动；轻量、不影响性能 */
body::before{
  content:"";position:fixed;inset:-15%;z-index:-2;pointer-events:none;
  background:
    radial-gradient(50% 40% at 18% 12%, rgba(95,161,255,.22) 0%, transparent 60%),
    radial-gradient(45% 40% at 82% 8%, rgba(185,124,255,.18) 0%, transparent 60%),
    radial-gradient(60% 50% at 50% 110%, rgba(255,122,168,.14) 0%, transparent 70%),
    linear-gradient(180deg,#0a0d1a 0%,#070a14 60%,#04060d 100%);
  filter:saturate(1.1) blur(2px);
}
body::after{
  content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background-image:radial-gradient(rgba(255,255,255,.03) 1px,transparent 1px);
  background-size:3px 3px;mix-blend-mode:overlay;opacity:.6;
}
a{color:#9dd1ff;text-decoration:none}
a:hover{color:#b9dcff}
a:active{opacity:.75}
button,input,select,textarea{font:inherit;color:inherit;font-family:inherit}
button{cursor:pointer;border:0;background:transparent}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);border-radius:6px;border:2px solid transparent;background-clip:padding-box}
::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.22);background-clip:padding-box}
::selection{background:rgba(95,161,255,.4);color:#fff}
.app{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}
/* 顶栏：玻璃 + 渐变描边 */
.topbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:10px;
  padding:12px 16px;padding-top:max(12px,env(safe-area-inset-top));
  background:linear-gradient(180deg,rgba(10,14,24,.72),rgba(10,14,24,.55));
  backdrop-filter:blur(24px) saturate(1.6);-webkit-backdrop-filter:blur(24px) saturate(1.6);
  border-bottom:1px solid var(--line)}
.topbar:after{content:"";position:absolute;left:0;right:0;bottom:-1px;height:1px;
  background:linear-gradient(90deg,transparent,var(--accent),var(--accent-2),transparent);opacity:.45;pointer-events:none}
.topbar .brand{font-weight:800;letter-spacing:.01em;font-size:1.04rem;margin-right:4px;max-width:60vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topbar .brand small{color:var(--muted);font-weight:500;margin-left:6px;font-size:.8rem}
.topbar .spacer{flex:1}
/* 按钮：玻璃 + 微光；主按钮渐变蓝紫 */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:40px;padding:9px 16px;
  border-radius:999px;border:1px solid var(--line-strong);
  background:rgba(255,255,255,.06);color:var(--fg);font-weight:600;text-decoration:none;white-space:nowrap;
  transition:transform .14s ease, background .14s ease, box-shadow .14s ease, border-color .14s ease;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.btn:hover{background:rgba(255,255,255,.1);border-color:rgba(255,255,255,.25)}
.btn:active{transform:scale(.97)}
.btn-primary{background:linear-gradient(135deg,#5fa1ff 0%,#7c5cff 60%,#b97cff 100%);
  border-color:transparent;color:#fff;
  box-shadow:0 8px 22px rgba(95,161,255,.36),inset 0 1px 0 rgba(255,255,255,.18)}
.btn-primary:hover{box-shadow:0 10px 26px rgba(124,124,255,.45),inset 0 1px 0 rgba(255,255,255,.25)}
.btn-ghost{background:transparent;border-color:var(--line)}
.btn-ghost:hover{background:rgba(255,255,255,.05)}
.btn-sm{min-height:32px;padding:5px 12px;border-radius:999px;font-size:.85rem}
.content{flex:1;padding:18px 16px;padding-bottom:max(22px,env(safe-area-inset-bottom));
  max-width:1280px;width:100%;margin:0 auto}
/* 卡片：玻璃 + 微微悬浮 */
.card{position:relative;background:var(--card-bg);
  border:1px solid var(--line);border-radius:var(--radius-md);padding:16px;
  box-shadow:var(--shadow-md);
  backdrop-filter:blur(var(--card-blur)) saturate(1.4);-webkit-backdrop-filter:blur(var(--card-blur)) saturate(1.4);
  transition:transform .25s ease, box-shadow .25s ease, border-color .25s ease}
.card:before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:linear-gradient(180deg,rgba(255,255,255,.06),transparent 30%);}
.card + .card{margin-top:14px}
.muted{color:var(--muted);font-size:.85rem}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tag{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.72rem;font-weight:600;
  background:rgba(124,193,255,.14);color:#b6dbff;border:1px solid rgba(124,193,255,.3)}
.breadcrumbs{display:flex;flex-wrap:wrap;gap:4px;font-size:.9rem;color:#97a8c2}
.breadcrumbs a{padding:5px 10px;border-radius:8px;background:rgba(255,255,255,.05);
  border:1px solid var(--line);transition:background .14s}
.breadcrumbs a:hover{background:rgba(255,255,255,.1)}
.breadcrumbs .sep{color:#53617a;padding:5px 0}
table.files{width:100%;border-collapse:separate;border-spacing:0}
table.files th,table.files td{padding:11px 10px;border-bottom:1px solid var(--line);font-size:.95rem;text-align:left}
table.files thead th{position:sticky;top:0;background:rgba(20,26,40,.7);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  color:var(--muted);font-weight:600;font-size:.78rem;letter-spacing:.04em;text-transform:uppercase}
table.files tbody tr{transition:background .14s ease}
table.files tbody tr:hover{background:rgba(255,255,255,.04)}
table.files td.name{word-break:break-all}
table.files td.size,table.files td.mtime{white-space:nowrap;color:#aab7ca;font-size:.85rem}
table.files .ops{white-space:nowrap}
table.files .ops a{margin-right:10px;padding:3px 8px;border-radius:6px;
  background:rgba(95,161,255,.12);color:#b6dbff;border:1px solid rgba(95,161,255,.22);
  transition:background .14s}
table.files .ops a:hover{background:rgba(95,161,255,.2)}
.input{min-height:40px;padding:9px 14px;border-radius:var(--radius-sm);border:1px solid var(--line-strong);
  background:rgba(255,255,255,.04);color:var(--fg);font-size:1rem;
  transition:border-color .14s, background .14s, box-shadow .14s;
  backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px)}
.input:focus{outline:none;border-color:rgba(95,161,255,.55);background:rgba(255,255,255,.07);
  box-shadow:0 0 0 3px rgba(95,161,255,.18)}
select.input{appearance:none;-webkit-appearance:none;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><path fill='%23a4b3cc' d='M3 6l5 5 5-5z'/></svg>");
  background-repeat:no-repeat;background-position:right 10px center;padding-right:30px}
.empty{padding:32px;text-align:center;color:var(--muted)}
/* 入口磁贴：彩色玻璃 + 悬浮发光 */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
.tile{position:relative;display:flex;flex-direction:column;justify-content:space-between;
  padding:20px;border-radius:var(--radius-lg);
  background:linear-gradient(160deg,rgba(95,161,255,.22) 0%,rgba(124,124,255,.16) 50%,rgba(185,124,255,.10) 100%);
  border:1px solid rgba(255,255,255,.16);
  color:var(--fg);min-height:130px;
  box-shadow:var(--shadow-sm), inset 0 1px 0 rgba(255,255,255,.08);
  backdrop-filter:blur(18px) saturate(1.4);-webkit-backdrop-filter:blur(18px) saturate(1.4);
  transition:transform .25s ease, box-shadow .25s ease, border-color .25s ease;
  overflow:hidden}
.tile:before{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:radial-gradient(50% 60% at 0% 0%,rgba(255,255,255,.18),transparent 70%);opacity:.7}
.tile:after{content:"";position:absolute;right:-30%;bottom:-50%;width:140%;height:140%;
  background:radial-gradient(closest-side,rgba(185,124,255,.28),transparent 60%);
  pointer-events:none;opacity:0;transition:opacity .35s ease}
.tile:hover{transform:translateY(-2px) scale(1.012);border-color:rgba(255,255,255,.28);
  box-shadow:var(--shadow-md), inset 0 1px 0 rgba(255,255,255,.12)}
.tile:hover:after{opacity:1}
.tile .emoji{font-size:2.2rem;line-height:1;filter:drop-shadow(0 2px 6px rgba(0,0,0,.35))}
.tile .title{font-weight:800;font-size:1.08rem;margin-top:8px;letter-spacing:.005em}
.tile .desc{color:rgba(236,241,255,.7);font-size:.85rem}
/* 退出小方块：保留原 z-index 与触发逻辑 */
#mitm-exit-f{display:block;width:100%;height:100%;margin:0;padding:0;border:0;position:relative;z-index:1}
.mitm-fab-wrap{position:fixed;right:max(6px,env(safe-area-inset-right));bottom:max(6px,env(safe-area-inset-bottom));
  left:auto;top:auto;z-index:2147483000;isolation:isolate;pointer-events:auto;
  width:44px;height:44px;box-sizing:border-box}
.mitm-fab{position:relative;right:auto;bottom:auto;
  width:100%;height:100%;min-width:44px;min-height:44px;border-radius:14px;
  background:rgba(48,64,98,.7);border:1px solid rgba(255,255,255,.22);
  box-shadow:0 8px 24px rgba(0,0,0,.55);-webkit-tap-highlight-color:transparent;
  appearance:none;-webkit-appearance:none;cursor:pointer;
  text-decoration:none;display:block;padding:0;margin:0;outline:0;color:transparent;
  font-size:0;-webkit-user-select:none;user-select:none;
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
a.mitm-fab-ghost{position:absolute;left:0;top:0;right:0;bottom:0;z-index:6;width:100%;height:100%;
  text-indent:150%;white-space:nowrap;overflow:hidden;opacity:0.1;background:transparent;cursor:pointer;
  touch-action:manipulation;-webkit-tap-highlight-color:rgba(255,255,255,.1)}
a.mitm-fab:visited,a.mitm-fab:link{color:transparent}
.mitm-fab-wrap:active .mitm-fab{transform:scale(.94);background:rgba(64,86,120,.92)}
@media (max-width:520px){
  .content{padding:14px 12px}
  .topbar{padding:10px 12px}
  .tile{padding:16px;min-height:118px;border-radius:18px}
  .tile .emoji{font-size:1.85rem}
}
@supports not (backdrop-filter:blur(1px)){
  /* 不支持 backdrop-filter 时退到不透明背景，保证可读 */
  .topbar{background:rgba(10,14,24,.95)}
  .card{background:rgba(20,26,40,.96)}
  .btn{background:rgba(255,255,255,.08)}
}
"""

# 音乐页专用：在 __shell 的 extra_head 中注入，锁死整页滚动，仅列表可滚（不依赖 :has 支持）
_MUSIC_LOCK_HEAD = r"""
<style>
html.mitm-music-locked,html.mitm-music-locked body{
  height:100%!important;max-height:100%!important;overflow:hidden!important;
  width:100%!important;margin:0;box-sizing:border-box!important;
}
html.mitm-music-locked{position:fixed;inset:0;}
html.mitm-music-locked .app{
  height:100%!important;max-height:100%!important;min-height:0!important;overflow:hidden!important;
  display:flex!important;flex-direction:column!important;box-sizing:border-box!important;
}
html.mitm-music-locked .content.mitm-music-page{
  flex:1 1 0!important;min-height:0!important;overflow:hidden!important;
  display:flex!important;flex-direction:column!important;padding:6px 10px 8px!important;max-height:none!important;
}
html.mitm-music-locked .music-layout{flex:1 1 0!important;min-height:0!important;overflow:hidden!important;display:flex!important;flex-direction:column!important}
html.mitm-music-locked .player{
  flex:1 1 0!important;min-height:0!important;overflow:hidden!important;height:100%!important;max-height:100%!important;
  display:flex!important;flex-direction:row!important;align-items:stretch!important;
}
html.mitm-music-locked .player-left{
  flex:0 0 clamp(256px,32vw,360px)!important;width:clamp(256px,32vw,360px)!important;max-width:100%!important;
  height:100%!important;min-height:0!important;overflow:hidden!important;position:relative!important;align-self:stretch!important;
}
html.mitm-music-locked .player-right,html.mitm-music-locked .playlist-scroll{min-width:0!important;min-height:0!important}
html.mitm-music-locked .player-right{
  flex:1 1 0!important;overflow:hidden!important;display:flex!important;flex-direction:column!important;height:100%!important;
}
html.mitm-music-locked .playlist-scroll{
  flex:1 1 0!important;overflow-y:auto!important;-webkit-overflow-scrolling:touch!important;overscroll-behavior:contain!important;
}
@media (max-width:900px){
  html.mitm-music-locked .player{flex-direction:column!important;overflow:hidden!important;min-height:0!important}
  html.mitm-music-locked .player-left{
    flex:0 0 auto!important;width:100%!important;max-height:50vh!important;overflow:hidden!important;
    height:auto!important;
  }
  html.mitm-music-locked .np-inner{gap:10px!important;padding:14px!important}
  html.mitm-music-locked .np-cover-wrap{width:min(72vw,240px)!important;max-height:26vh!important}
  html.mitm-music-locked .np-cover{max-width:100%!important;max-height:26vh!important;aspect-ratio:1/1!important}
  html.mitm-music-locked .player-right{flex:1 1 0!important;min-height:0!important;overflow:hidden!important}
}
</style>
<script>document.documentElement.classList.add("mitm-music-locked");</script>
"""

# 在 shell 各页注入：用 sendBeacon 记 pagehide / vis_hidden 等，与手工关 WebView、小方块时序对照（见 MITM_EXIT_TELEMETRY_LOG）
_EXIT_TEL_HTML = r"""
<script>
(function(){
  var ep;
  try{ ep = new URL("/__mitm-exit-telemetry", document.baseURI).href; }
  catch(e0){ ep = location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-exit-telemetry"; }
  window.mitmExitLog = function(ev){
    try{
      var u = ep + "?e=" + encodeURIComponent(ev) + "&t=" + Date.now() + "&p=" + encodeURIComponent(
        (location.pathname || "/") + (location.search || "")
      );
      if (navigator.sendBeacon) navigator.sendBeacon(u);
      else{ var g = new Image(); g.src = u; }
    }catch(x){}
  };
  addEventListener("pagehide", function(){ if (window.mitmExitLog) window.mitmExitLog("pagehide"); });
  addEventListener("beforeunload", function(){ if (window.mitmExitLog) window.mitmExitLog("beforeunload"); });
  document.addEventListener("visibilitychange", function(){
    if (document.hidden && window.mitmExitLog) window.mitmExitLog("vis_hidden");
  });
  addEventListener("pageshow", function(ev){
    if (!ev.persisted) return;
    try {
      if (sessionStorage.getItem("mitmFabTrap") !== "1") return;
      if (String(location.protocol || "").toLowerCase().indexOf("data") === 0) return;
      var eu = "";
      try { eu = new URL("/__mitm-exit", document.baseURI).href; } catch (x0) { return; }
      var tu = "";
      try { tu = new URL("/__mitm-trap?next=" + encodeURIComponent(eu), document.baseURI).href; } catch (x0b) { return; }
      try { if (window.mitmExitLog) window.mitmExitLog("mitm_retrap_bfcache"); } catch (x1) {}
      try { location.replace(tu); return; } catch (x2) {}
      if (window.mitmBuildDataExitPage) {
        try { location.replace(mitmBuildDataExitPage(eu)); return; } catch (x2b) {}
      }
      if (window.mitmGoToExit) mitmGoToExit("retrap_bfcache");
    } catch (e) {}
  });
})();
</script>
""".strip()


# 全站悬浮迷你音乐播放器：跨页持续播放（依赖 localStorage 状态 + /music_tracks 接口）
_MINI_PLAYER_HTML = r"""
<style>
.mitm-mini{position:fixed;top:14px;right:14px;left:auto;bottom:auto;z-index:2147481000;width:310px;max-width:calc(100vw - 28px);
  background:rgba(18,22,34,.5);color:#f4f7ff;border:1px solid rgba(255,255,255,.18);
  border-radius:18px;padding:10px 36px 10px 12px;display:none;
  box-shadow:0 16px 38px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.08);
  backdrop-filter:blur(28px) saturate(1.4);-webkit-backdrop-filter:blur(28px) saturate(1.4);
  font-size:.86rem;user-select:none;-webkit-user-select:none;cursor:grab;transition:transform .15s ease,box-shadow .15s ease}
.mitm-mini.show{display:block}
.mitm-mini.dragging{cursor:grabbing;transition:none;box-shadow:0 24px 56px rgba(0,0,0,.55),0 0 0 1px rgba(255,255,255,.22) inset}
.mitm-mini .row{display:flex;align-items:center;gap:10px}
.mitm-mini .cover{width:46px;height:46px;flex:0 0 46px;border-radius:10px;background:#222 center/cover;position:relative;overflow:hidden;cursor:inherit}
.mitm-mini .cover.no-cover{background:linear-gradient(145deg,#3a5694,#1c1e2e)}
.mitm-mini .cover.no-cover::after{content:"";position:absolute;inset:24%;background:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='white' fill-opacity='0.85' d='M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z'/></svg>") center/contain no-repeat}
.mitm-mini .info{flex:1;min-width:0;text-decoration:none;color:inherit;display:block}
.mitm-mini .title{font-weight:700;line-height:1.5;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mitm-mini .meta{color:rgba(236,241,255,.65);font-size:.78rem;line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mitm-mini .ctrls{display:flex;align-items:center;gap:4px}
.mitm-mini .ctrls button{min-width:32px;min-height:32px;border-radius:999px;border:1px solid rgba(255,255,255,.16);background:rgba(255,255,255,.08);color:#fff;font-size:.85rem;line-height:1;padding:0}
.mitm-mini .ctrls .play{min-width:38px;min-height:38px;background:#fff;color:#0f182b;border:none;font-size:1rem}
.mitm-mini .close{position:absolute;top:6px;right:8px;width:22px;height:22px;border-radius:999px;border:0;background:rgba(255,255,255,.1);color:#f4f7ff;font-size:.75rem;line-height:1;padding:0}
.mitm-mini .progress-bar{height:2px;border-radius:2px;background:rgba(255,255,255,.18);margin-top:8px;overflow:hidden}
.mitm-mini .progress-bar > div{height:100%;width:0;background:#9dd1ff;transition:width .15s linear}
.mitm-mini-toggle{position:fixed;top:14px;right:14px;z-index:2147481000;display:none;width:42px;height:42px;border-radius:999px;border:1px solid rgba(255,255,255,.22);background:rgba(18,22,34,.55);color:#fff;font-size:1.05rem;box-shadow:0 10px 24px rgba(0,0,0,.4);backdrop-filter:blur(20px) saturate(1.3);-webkit-backdrop-filter:blur(20px) saturate(1.3);align-items:center;justify-content:center;padding:0;cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;transition:box-shadow .15s ease}
.mitm-mini-toggle.show{display:flex}
.mitm-mini-toggle.dragging{cursor:grabbing;box-shadow:0 14px 30px rgba(0,0,0,.5)}
</style>
<div class="mitm-mini" id="mitm-mini" aria-hidden="true">
  <button type="button" class="close" id="mitm-mini-close" aria-label="关闭悬浮窗">×</button>
  <div class="row">
    <div class="cover" id="mitm-mini-cover"></div>
    <a class="info" id="mitm-mini-info" href="/music" title="打开完整播放器">
      <div class="title" id="mitm-mini-title">—</div>
      <div class="meta" id="mitm-mini-meta">—</div>
    </a>
    <div class="ctrls">
      <button type="button" id="mitm-mini-prev" title="上一首">⏮</button>
      <button type="button" class="play" id="mitm-mini-play" title="播放/暂停">▶</button>
      <button type="button" id="mitm-mini-next" title="下一首">⏭</button>
    </div>
  </div>
  <div class="progress-bar"><div id="mitm-mini-progress"></div></div>
</div>
<button type="button" class="mitm-mini-toggle" id="mitm-mini-toggle" title="打开悬浮播放">🎵</button>
<audio id="mitm-mini-audio" preload="metadata"></audio>
<script>
(function(){
  if (location.pathname === '/music') return;
  if (window.__mitmMiniInited) return;
  window.__mitmMiniInited = true;

  var mini = document.getElementById('mitm-mini');
  if (!mini) return;
  var audio = document.getElementById('mitm-mini-audio');
  var elCover = document.getElementById('mitm-mini-cover');
  var elTitle = document.getElementById('mitm-mini-title');
  var elMeta = document.getElementById('mitm-mini-meta');
  var elPlay = document.getElementById('mitm-mini-play');
  var elPrev = document.getElementById('mitm-mini-prev');
  var elNext = document.getElementById('mitm-mini-next');
  var elClose = document.getElementById('mitm-mini-close');
  var elToggle = document.getElementById('mitm-mini-toggle');
  var elProgress = document.getElementById('mitm-mini-progress');

  var STATE_KEY = 'mitm_music_state_v3';
  var TRACKS_KEY = 'mitm_music_tracks_v1';
  var HIDE_KEY = 'mitm_mini_hidden';
  var POS_KEY = 'mitm_mini_pos';
  var TOGGLE_POS_KEY = 'mitm_mini_toggle_pos';

  function clampPos(el, left, top){
    var pad = 8;
    var w = el.offsetWidth || (el===mini ? 310 : 42);
    var h = el.offsetHeight || (el===mini ? 70 : 42);
    left = Math.max(pad, Math.min(window.innerWidth - w - pad, left));
    top = Math.max(pad, Math.min(window.innerHeight - h - pad, top));
    return {left:left, top:top};
  }
  function applyPos(el, left, top){
    var p = clampPos(el, left, top);
    el.style.left = p.left + 'px';
    el.style.top = p.top + 'px';
    el.style.right = 'auto';
    el.style.bottom = 'auto';
  }
  function restorePos(el, key){
    try{
      var pos = JSON.parse(localStorage.getItem(key)||'null');
      if (pos && typeof pos.left === 'number' && typeof pos.top === 'number') applyPos(el, pos.left, pos.top);
    }catch(e){}
  }
  function makeDraggable(el, posKey, opts){
    opts = opts || {};
    var pressing = false, dragging = false, sx = 0, sy = 0, ox = 0, oy = 0, moved = false;
    function onDown(ev){
      if (!opts.allowButton){
        var t = ev.target;
        if (t && (t.closest('button') || t.closest('a'))) return;
      }
      pressing = true; dragging = false; moved = false;
      var rect = el.getBoundingClientRect();
      var p = ev.touches ? ev.touches[0] : ev;
      sx = p.clientX; sy = p.clientY; ox = rect.left; oy = rect.top;
      // 注意：不在此处 preventDefault，否则会吞掉 click（点击展开就失效）
    }
    function onMove(ev){
      if (!pressing) return;
      var p = ev.touches ? ev.touches[0] : ev;
      var dx = p.clientX - sx, dy = p.clientY - sy;
      if (!dragging){
        if (Math.abs(dx) + Math.abs(dy) > 4){
          dragging = true; moved = true;
          applyPos(el, ox, oy);
          el.classList.add('dragging');
        } else {
          return;
        }
      }
      applyPos(el, ox + dx, oy + dy);
      if (ev.cancelable && ev.touches) ev.preventDefault();
    }
    function onUp(){
      if (!pressing) return;
      pressing = false;
      el.classList.remove('dragging');
      if (dragging){
        dragging = false;
        try{ localStorage.setItem(posKey, JSON.stringify({left:el.offsetLeft, top:el.offsetTop})); }catch(e){}
      }
    }
    el.addEventListener('mousedown', onDown);
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    el.addEventListener('touchstart', onDown, {passive:true});
    document.addEventListener('touchmove', onMove, {passive:false});
    document.addEventListener('touchend', onUp);
    document.addEventListener('touchcancel', onUp);
    // 仅在真的发生过位移后抑制随后的 click（避免拖动结束误触按钮）
    el.addEventListener('click', function(e){
      if (moved){ e.preventDefault(); e.stopPropagation(); moved = false; }
    }, true);
  }
  window.addEventListener('resize', function(){
    if (mini.classList.contains('show')) applyPos(mini, mini.offsetLeft, mini.offsetTop);
    if (elToggle.classList.contains('show')) applyPos(elToggle, elToggle.offsetLeft, elToggle.offsetTop);
  });

  function loadState(){ try{return JSON.parse(localStorage.getItem(STATE_KEY)||'{}')||{};}catch(e){return {};} }
  function saveState(extra){
    try{
      var st = loadState();
      if (extra) for (var k in extra) st[k] = extra[k];
      st.i = curIdx; st.pos = audio.currentTime||0; st.vol = audio.volume;
      st.playing = !audio.paused;
      localStorage.setItem(STATE_KEY, JSON.stringify(st));
    }catch(e){}
  }
  function metaText(t){var p=[];if(t.artist)p.push(t.artist);if(t.album)p.push(t.album);if(t.year)p.push(t.year);return p.join(' · ');}

  var tracks = [], curIdx = 0;
  var st0 = loadState();
  if (typeof st0.vol === 'number') audio.volume = st0.vol;

  function fetchTracks(cb){
    try{
      var cached = sessionStorage.getItem(TRACKS_KEY);
      if (cached){ var arr = JSON.parse(cached); if (Array.isArray(arr) && arr.length){ cb(arr); return; } }
    }catch(e){}
    fetch('/music_tracks', {credentials:'include'}).then(function(r){return r.ok?r.json():[];}).then(function(arr){
      if (!Array.isArray(arr)) arr = [];
      try{ sessionStorage.setItem(TRACKS_KEY, JSON.stringify(arr)); }catch(e){}
      cb(arr);
    }).catch(function(){ cb([]); });
  }
  function showMini(){ mini.classList.add('show'); mini.setAttribute('aria-hidden','false'); elToggle.classList.remove('show'); try{ sessionStorage.removeItem(HIDE_KEY); }catch(e){} }
  function hideMini(){ mini.classList.remove('show'); mini.setAttribute('aria-hidden','true'); elToggle.classList.add('show'); try{ sessionStorage.setItem(HIDE_KEY,'1'); }catch(e){} }

  function loadTrack(i, autoplay){
    if (!tracks.length) return;
    if (i<0) i=tracks.length-1; if (i>=tracks.length) i=0;
    curIdx = i;
    var t = tracks[i];
    audio.src = t.src;
    elTitle.textContent = t.name || '未知标题';
    elMeta.textContent = metaText(t) || '未知专辑';
    if (t.cover){ elCover.style.backgroundImage = 'url('+t.cover+')'; elCover.classList.remove('no-cover'); }
    else { elCover.style.backgroundImage = ''; elCover.classList.add('no-cover'); }
    if (autoplay) audio.play().catch(function(){});
    saveState();
  }
  function nextTrack(){
    if (!tracks.length) return;
    var st2 = loadState(), shuffle = !!st2.shuffle, repeatMode = st2.repeat || 'all', i;
    if (shuffle){
      if (tracks.length<=1) i=curIdx;
      else { do { i=Math.floor(Math.random()*tracks.length); } while(i===curIdx); }
    } else {
      i = curIdx+1;
      if (i>=tracks.length){ if (repeatMode==='all') i=0; else { audio.pause(); return; } }
    }
    loadTrack(i, true);
  }
  function prevTrack(){
    if (!tracks.length) return;
    if (audio.currentTime>3){ audio.currentTime=0; return; }
    var i = curIdx-1; if (i<0) i=tracks.length-1;
    loadTrack(i, true);
  }
  function updatePlay(){ elPlay.textContent = audio.paused ? '▶' : '⏸'; }

  elPlay.addEventListener('click', function(){ if (audio.paused) audio.play().catch(function(){}); else audio.pause(); });
  elPrev.addEventListener('click', prevTrack);
  elNext.addEventListener('click', nextTrack);
  elClose.addEventListener('click', hideMini);
  elToggle.addEventListener('click', showMini);
  audio.addEventListener('play', function(){ updatePlay(); saveState({playing:true}); });
  audio.addEventListener('pause', function(){ updatePlay(); saveState({playing:false}); });
  audio.addEventListener('timeupdate', function(){
    if (isFinite(audio.duration) && audio.duration>0){
      elProgress.style.width = ((audio.currentTime/audio.duration)*100).toFixed(1)+'%';
    }
    if (Math.floor(audio.currentTime) % 5 === 0) saveState();
  });
  audio.addEventListener('ended', function(){
    var st2 = loadState();
    if (st2.repeat === 'one'){ audio.currentTime=0; audio.play().catch(function(){}); return; }
    nextTrack();
  });
  window.addEventListener('pagehide', function(){ saveState(); });
  window.addEventListener('beforeunload', function(){ saveState(); });

  var hidden = false;
  try{ hidden = sessionStorage.getItem(HIDE_KEY) === '1'; }catch(e){}

  fetchTracks(function(arr){
    tracks = arr || [];
    if (!tracks.length){ mini.style.display='none'; elToggle.style.display='none'; return; }
    var st2 = loadState();
    curIdx = (typeof st2.i === 'number' && st2.i >= 0 && st2.i < tracks.length) ? st2.i : 0;
    loadTrack(curIdx, false);
    if (typeof st2.pos === 'number' && st2.pos > 0){
      audio.addEventListener('loadedmetadata', function(){ try{ audio.currentTime = st2.pos; }catch(e){} }, {once:true});
    }
    if (st2.playing){
      var startPlay = function(){ try{ audio.play().catch(function(){}); }catch(e){} };
      if (audio.readyState >= 2) startPlay();
      else audio.addEventListener('loadeddata', startPlay, {once:true});
    }
    if (hidden) hideMini(); else showMini();
    restorePos(mini, POS_KEY);
    restorePos(elToggle, TOGGLE_POS_KEY);
    makeDraggable(mini, POS_KEY);
    makeDraggable(elToggle, TOGGLE_POS_KEY, {allowButton:true});
    updatePlay();
  });
})();
</script>
""".strip()


def _shell(title: str, body_html: str, *, extra_head: str = "", extra_body_end: str = "",
           raw: bool = False, show_splash_fab: bool = True, exit_telemetry: bool = True,
           mini_player: bool = True) -> bytes:
    if raw:
        wrap_open = ""
        wrap_close = ""
        extra_style = "<style>body{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}</style>"
    else:
        wrap_open = '<div class="app">'
        wrap_close = "</div>"
        extra_style = ""
    # 小方块：瞬间全黑后优先进同源 /__mitm-trap（服务端推满 pushState+定时补栈，比 data: 好压「返回」）；失败再 data:；主文档上 mitmGoToExit 兜底
    exit_js = (
        r"""<script>
function mitmGoToExit(tag){
  try{ if (window.mitmExitLog) window.mitmExitLog(tag || "mitm_go_exit"); }catch(_){ }
  var u, pathOk = false;
  try{
    u = new URL("/__mitm-exit", document.baseURI).href;
  }catch(x0){
    u = (location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-exit");
  }
  try{
    var p = (location && location.pathname) || "";
    if (p === "/__mitm-exit" || p.indexOf("/__mitm-exit") === 0) { pathOk = true; }
  }catch(x1){}
  if (pathOk) return;
  try{
    if (window.top && window.top !== window) {
      var tp = (window.top.location && window.top.location.pathname) || "";
      if (tp === "/__mitm-exit" || tp.indexOf("/__mitm-exit") === 0) return;
    }
  }catch(x2){}
  try{
    if (window.top) { window.top.location.replace(u); return; }
  }catch(a0){}
  try{ location.replace(u); }catch(b0){
    try{ location.href = u; }catch(c0){
      try{ if (window.top) window.top.location.href = u; }catch(d0){}
    }
  }
}
function mitmBuildDataExitPage(eu){
  var s = "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
    "<style>html,body{margin:0;padding:0;background:#000;height:100%;min-height:100vh;min-height:100dvh;overflow:hidden;touch-action:none}</style></head><body><scr" + "ipt>";
  s += "(function(){var EU=" + JSON.stringify(eu) + ";";
  s += "function oc(){try{window.close();}catch(e){}try{if(window.top)window.top.close();}catch(e2){}}";
  s += "oc();[0,30,100,300,800].forEach(function(t){setTimeout(oc,t);});";
  s += "var H0=location.href;";
  s += "function _bed(n){n=n||40;var a=0;while(a<n){try{history.pushState({a:a},document.title,H0);a++;}catch(e0){a=9999}}}";
  s += "_bed(64);setTimeout(function(){_bed(20);},0);";
  s += "window.addEventListener('popstate',function(){_bed(40);setTimeout(_bed,0,20);setTimeout(_bed,10,20);},false);";
  s += "window.addEventListener('pageshow',function(ev){if(ev.persisted){_bed(24);}},false);";
  s += "function go(){try{if(window.top&&window.top!==window)window.top.location.replace(EU);}catch(e3){}try{location.replace(EU);}catch(e4){try{location.href=EU;}catch(e5){}}}";
  s += "setTimeout(go,350);setTimeout(go,1000);setTimeout(go,2000);";
  s += "})();";
  s += "</scr" + "ipt></body></html>";
  return "data:text/html;charset=utf-8," + encodeURIComponent(s);
}
function mitmStillOnTrappedContent(){
  try {
    if (String(location.protocol || "").toLowerCase().indexOf("data") === 0) return false;
    var p = (location && location.pathname) || "";
    if (p.indexOf("__mitm-exit") >= 0) return false;
    if (p.indexOf("__mitm-trap") >= 0) return false;
  } catch (e) {}
  return true;
}
function mitmApplyBlackMax(){
  try{
    if (window.mitmExitLog) window.mitmExitLog("fab_exit_black_instant");
  }catch (e0) {}
  try{
    var r = document.body || document.documentElement;
    var o = document.getElementById("mitm-fab-black");
    if (!o) {
      o = document.createElement("div");
      o.id = "mitm-fab-black";
      o.setAttribute("aria-hidden", "true");
      o.setAttribute("style", "position:fixed;inset:0;background:#000;z-index:2147483646;pointer-events:auto;touch-action:none;");
      r.appendChild(o);
    }
    document.documentElement.style.cssText = (document.documentElement.style.cssText || "") + "background:#000;overflow:hidden;height:100%;max-height:100%;";
    if (document.body) {
      document.body.style.cssText = (document.body.style.cssText || "") + "margin:0;background:#000;min-height:100vh;min-height:100dvh;overflow:hidden;touch-action:none;-webkit-user-select:none;user-select:none;";
    }
  } catch (x) {}
}
function mitmFabExit(){
  if (window.__mitmFabExiting) {
    try{ if (window.mitmExitLog) window.mitmExitLog("fab_exit_skip_dup"); }catch(_){ }
    return;
  }
  window.__mitmFabExiting = true;
  setTimeout(function(){ try{ window.__mitmFabExiting = false; }catch(e){} }, 12000);
  var exitU;
  try{ exitU = new URL("/__mitm-exit", document.baseURI).href; }catch(ee) {
    exitU = (location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-exit");
  }
  try{ if (window.mitmExitLog) window.mitmExitLog("fab_exit_like_manual"); }catch(e0){}
  function tryClose(){
    try{ window.close(); }catch(e){}
    try{ self.close(); }catch(e){}
    try{ if(window.top) window.top.close(); }catch(e2){}
  }
  mitmApplyBlackMax();
  try{ sessionStorage.setItem("mitmFabTrap", "1"); }catch(eS){}
  tryClose();
  [0, 40, 100, 200, 300, 500, 800].forEach(function(t){ setTimeout(tryClose, t); });
  setTimeout(function(){
    try{ if (window.mitmExitLog) window.mitmExitLog("fab_exit_to_trap"); }catch(_){ }
    var tU;
    try{ tU = new URL("/__mitm-trap?next=" + encodeURIComponent(exitU), document.baseURI).href; }
    catch(et){ tU = (location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-trap?next=" + encodeURIComponent(exitU)); }
    try{
      if (window.top) { try{ window.top.location.replace(tU); return; }catch(te0){} }
    } catch (te) {}
    try{ location.replace(tU); return; }catch(x0){
    var d;
    try{ d = mitmBuildDataExitPage(exitU); }catch(x0b){ d = null; }
    if (d) {
      try{ if (window.top) { window.top.location.replace(d); return; } } catch (x1) {}
      try{ location.replace(d); return; } catch (x2) { try{ mitmGoToExit("fab_trap_data_both"); }catch(x3){} }
    } else { try{ mitmGoToExit("fab_trap_repl_fail"); }catch(x4){} }
    }
  }, 150);
  setTimeout(function(){
    if (mitmStillOnTrappedContent()) { mitmGoToExit("fab_fallback_600"); }
  }, 600);
  setTimeout(function(){
    if (mitmStillOnTrappedContent()) { mitmGoToExit("fab_fallback_1000"); }
  }, 1000);
  setTimeout(function(){
    if (mitmStillOnTrappedContent()) {
      try{ if (window.mitmExitLog) window.mitmExitLog("fab_last_punt"); }catch(_){ }
      var tU2;
      try{ tU2 = new URL("/__mitm-trap?next=" + encodeURIComponent(exitU), document.baseURI).href; }
      catch(et2){ tU2 = (location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-trap?next=" + encodeURIComponent(exitU)); }
      try{
        if (window.top) { try{ window.top.location.replace(tU2); return; }catch(_){} }
        location.replace(tU2); return;
      } catch (xx) {
        try{
          if (window.top) { try{ window.top.location.replace(mitmBuildDataExitPage(exitU)); return; }catch(_){} }
          location.replace(mitmBuildDataExitPage(exitU));
        } catch (xx2) { mitmGoToExit("fab_last_punt_goto"); }
      }
    }
  }, 1500);
}
var mitmOneTapExit = mitmFabExit;
</script>
"""
        if show_splash_fab
        else ""
    )
    # 无默认导航：由脚本走 mitmFabExit，避免一点击就换页打断与手动退出一致的 pagehide 顺序
    fab = (
        r"""
<div class="mitm-fab-wrap" id="mitm-fab-wrap" title="退出">
  <button type="button" class="mitm-fab" id="mitm-exit-btn" aria-label="退出" tabindex="-1"></button>
  <a id="mitm-exit-a" class="mitm-fab-ghost" href="/__mitm-exit" target="_top" rel="opener" aria-label="退出">x</a>
</div>
<script>
(function(){
  var U;
  try{ U = new URL("/__mitm-exit", document.baseURI).href; }
  catch(e0){ U = location.protocol + "//" + (location.host || "127.0.0.1") + "/__mitm-exit"; }
  var a = document.getElementById("mitm-exit-a");
  if (a) a.setAttribute("href", U);
  var w = document.getElementById("mitm-fab-wrap");
  if (w) w.addEventListener("touchstart", function(){
    try{ if (window.mitmExitLog) window.mitmExitLog("fab_touchstart"); } catch(z0) {}
    try{ if (a) a.setAttribute("href", U); } catch(z) {}
  }, {passive: true, capture: true});
  function go(e){
    if (e) { e.preventDefault(); e.stopPropagation(); }
    try{ if (window.mitmExitLog) window.mitmExitLog("fab_click"); } catch(z1) {}
    try{ if (window.mitmFabExit) window.mitmFabExit(); } catch(z2) {}
    return false;
  }
  if (w) w.addEventListener("click", go, true);
})();
</script>
"""
        if show_splash_fab
        else ""
    )
    probe = _EXIT_TEL_HTML if exit_telemetry else ""
    mini = _MINI_PLAYER_HTML if mini_player else ""
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{html.escape(title)}</title>
<style>{_BASE_CSS}</style>
{extra_style}
{exit_js}
{extra_head}
</head>
<body>
{wrap_open}
{body_html}
{wrap_close}
{extra_body_end}
{probe}
{fab}
{mini}
</body>
</html>"""
    return doc.encode("utf-8")


def _html_response(body: bytes, *, status: int = 200) -> Response:
    return Response.make(status, body, {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Length": str(len(body)),
    })


def _error_page(msg: str, *, title: str = "出错了", status: int | None = None) -> Response:
    body = _shell(title, f"""
<div class="topbar">
  <span class="brand">出错</span>
  <span class="spacer"></span>
  <a class="btn btn-ghost" href="/">返回首页</a>
</div>
<div class="content"><div class="card"><p style="color:#ff9aa2">{html.escape(msg)}</p></div></div>""")
    st = status if status is not None else _error_http_status()
    return _html_response(body, status=st)


def _json_embed(data) -> str:
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


# ---------------------------------------------------------------------------
# 通用 JS：缩放 / 平移 / 双击 / 旋转（供 PDF、图片查看器使用）
# ---------------------------------------------------------------------------

_VIEWER_JS = r"""
(function(){
  var stage = document.getElementById('stage');
  var inner = document.getElementById('stage-inner');
  if (!stage || !inner) return;
  var scale=1, tx=0, ty=0, rot=0, start=null, lastTap=0;
  var velX=0, velY=0, lastT=0, lastX=0, lastY=0, inertiaId=null;
  var onChangeCb = null;

  function apply(){ inner.style.transform = 'translate3d('+tx+'px,'+ty+'px,0) scale('+scale+') rotate('+rot+'deg)'; if(onChangeCb) onChangeCb({scale:scale,tx:tx,ty:ty,rot:rot}); }
  function setScale(s){ scale=Math.min(5,Math.max(0.3,s)); apply(); var l=document.getElementById('zoom-label'); if(l)l.textContent=Math.round(scale*100)+'%'; }
  function reset(){ if(inertiaId) cancelAnimationFrame(inertiaId); scale=1; tx=0; ty=0; rot=0; apply(); setScale(1); }
  window.mitmFit = reset;
  window.mitmZoomIn = function(){ setScale(scale*1.2); };
  window.mitmZoomOut = function(){ setScale(scale/1.2); };
  window.mitmRotate = function(){ rot=(rot+90)%360; apply(); };
  window.mitmGetView = function(){ return {scale:scale, tx:tx, ty:ty, rot:rot}; };
  window.mitmSetView = function(v){
    if(!v || typeof v.scale!=='number') return;
    if(inertiaId) cancelAnimationFrame(inertiaId);
    scale=Math.min(5,Math.max(0.3,v.scale));
    tx=v.tx||0; ty=v.ty||0; rot=v.rot||0;
    apply();
    var l=document.getElementById('zoom-label'); if(l) l.textContent=Math.round(scale*100)+'%';
  };
  window.mitmOnViewChange = function(cb){ onChangeCb = cb; };

  function dlen(ax,ay,bx,by){ var dx=ax-bx, dy=ay-by; return Math.sqrt(dx*dx+dy*dy); }
  function trackVel(){
    var t=performance.now();
    if(lastT){
      var dt=t-lastT; if(dt>0){ velX=(tx-lastX)/dt; velY=(ty-lastY)/dt; }
    }
    lastT=t; lastX=tx; lastY=ty;
  }
  function startInertia(){
    if (Math.abs(velX)<0.04 && Math.abs(velY)<0.04){ velX=0; velY=0; return; }
    function step(){
      tx+=velX*16; ty+=velY*16;
      velX*=0.9; velY*=0.9;
      apply();
      if (Math.abs(velX)<0.02 && Math.abs(velY)<0.02){ velX=0; velY=0; inertiaId=null; return; }
      inertiaId = requestAnimationFrame(step);
    }
    if(inertiaId) cancelAnimationFrame(inertiaId);
    inertiaId = requestAnimationFrame(step);
  }

  stage.addEventListener('touchstart', function(e){
    if(inertiaId){ cancelAnimationFrame(inertiaId); inertiaId=null; }
    velX=0; velY=0; lastT=0;
    if (e.touches.length===1){
      start={type:'pan',x:e.touches[0].clientX-tx,y:e.touches[0].clientY-ty,moved:false};
    } else if (e.touches.length===2){
      var a=e.touches[0],b=e.touches[1];
      start={type:'pinch',d0:dlen(a.clientX,a.clientY,b.clientX,b.clientY),s0:scale,
             cx:(a.clientX+b.clientX)/2-tx,cy:(a.clientY+b.clientY)/2-ty};
    }
  },{passive:true});
  stage.addEventListener('touchmove', function(e){
    if(!start) return;
    if (start.type==='pan' && e.touches.length===1){
      e.preventDefault();
      tx=e.touches[0].clientX-start.x;
      ty=e.touches[0].clientY-start.y;
      start.moved=true;
      apply();
      trackVel();
    } else if (start.type==='pinch' && e.touches.length===2){
      e.preventDefault();
      var a=e.touches[0],b=e.touches[1];
      var d=dlen(a.clientX,a.clientY,b.clientX,b.clientY);
      start.moved=true;
      setScale(start.s0*d/start.d0);
      tx=(a.clientX+b.clientX)/2-start.cx;
      ty=(a.clientY+b.clientY)/2-start.cy;
      apply();
    }
  },{passive:false});
  stage.addEventListener('touchend', function(e){
    if (e.touches.length===0){
      if (e.changedTouches.length===1 && start && !start.moved){
        var now=Date.now();
        if (now-lastTap<300){
          if (scale>1.01) reset(); else { scale=2; apply(); setScale(2); }
        }
        lastTap=now;
      } else if (start && start.type==='pan' && start.moved){
        startInertia();
      }
      start=null;
    }
  },{passive:true});
  // 鼠标拖动支持（桌面调试）
  var mDown=null;
  stage.addEventListener('mousedown', function(e){
    if(inertiaId){ cancelAnimationFrame(inertiaId); inertiaId=null; }
    mDown={x:e.clientX-tx, y:e.clientY-ty, moved:false};
  });
  document.addEventListener('mousemove', function(e){
    if(!mDown) return;
    tx=e.clientX-mDown.x; ty=e.clientY-mDown.y; mDown.moved=true;
    apply(); trackVel();
  });
  document.addEventListener('mouseup', function(){ if(mDown && mDown.moved) startInertia(); mDown=null; });
  // 鼠标滚轮缩放（桌面调试）
  stage.addEventListener('wheel', function(e){
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    setScale(scale * (e.deltaY<0 ? 1.1 : 1/1.1));
  }, {passive:false});
})();
"""

_VIEWER_CSS = r"""
.stage{flex:1 1 auto;min-height:0;overflow:hidden;position:relative;
  background:#06080c;touch-action:none}
.stage-inner{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  padding:10px;transform-origin:center center;will-change:transform}
.stage-inner img{display:block;max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;
  background:#000;border-radius:6px;box-shadow:0 10px 36px rgba(0,0,0,.45)}
.tools{position:fixed;left:max(10px,env(safe-area-inset-left));bottom:max(18px,env(safe-area-inset-bottom));
  z-index:60;display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:16px;
  background:rgba(22,28,38,.92);border:1px solid rgba(255,255,255,.08);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 6px 22px rgba(0,0,0,.4)}
.tools button{min-width:48px;min-height:44px;border-radius:12px;border:none;background:rgba(255,255,255,.1);color:var(--fg);font-size:1.1rem;font-weight:600}
.tools button:active{transform:scale(.96)}
.tools .zr{font-size:.78rem}
#zoom-label{text-align:center;color:var(--muted);font-size:.74rem;padding:2px 0}
.pager{position:fixed;right:max(10px,env(safe-area-inset-right));left:auto;top:auto;
  bottom:max(58px,env(safe-area-inset-bottom));transform:none;
  z-index:60;display:flex;align-items:center;gap:6px;padding:8px 10px;border-radius:16px;
  max-width:calc(100vw - 20px);
  background:rgba(22,28,38,.92);border:1px solid rgba(255,255,255,.08);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);box-shadow:0 6px 22px rgba(0,0,0,.4)}
.pager .btn-sm{min-height:40px;padding:6px 12px}
.pager input[type=number]{width:4rem;min-height:40px;padding:4px 8px;border-radius:10px;
  border:1px solid rgba(255,255,255,.12);background:rgba(0,0,0,.25);color:var(--fg);font-size:.95rem}
.pager form{display:flex;align-items:center;gap:4px;margin:0}
.disabled{opacity:.4;pointer-events:none}
"""


def _viewer_tools_html(include_rotate: bool = True) -> str:
    rot_btn = '<button type="button" onclick="mitmRotate()" title="旋转 90°">⟳</button>' if include_rotate else ''
    return f"""
<div class="tools" aria-label="工具">
  <button type="button" onclick="mitmZoomIn()" title="放大">+</button>
  <span id="zoom-label">100%</span>
  <button type="button" onclick="mitmZoomOut()" title="缩小">−</button>
  <button type="button" class="zr" onclick="mitmFit()" title="适应屏幕">适应</button>
  {rot_btn}
</div>"""


def _back_href(path: Path) -> str:
    parent = path.parent
    if parent == _share_root():
        return "/"
    return f"/browse?path={_q(parent)}"


# ---------------------------------------------------------------------------
# 首页
# ---------------------------------------------------------------------------

def _home_response(flow) -> Response:
    ctx = user_auth.get_user_ctx_from_flow(flow)
    root = _share_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    pdf_dir = root / _DIR_PDF
    video_dir = root / _DIR_VIDEO
    music_dir = root / _DIR_MUSIC

    def _count(p: Path, exts: set[str]) -> int:
        if not p.is_dir():
            return 0
        n = 0
        try:
            for x in p.rglob("*"):
                if x.is_file() and x.suffix.lower() in exts:
                    n += 1
        except OSError:
            pass
        return n

    n_pdf = _count(pdf_dir, {".pdf"})
    n_vid = _count(video_dir, _VIDEO_EXTS)
    n_music = _count(music_dir, _AUDIO_EXTS)
    n_upload = _count(_ensure_upload_dir(), _VIDEO_EXTS | _AUDIO_EXTS | _IMAGE_EXTS | _TEXT_EXTS | _ARCHIVE_EXTS | {".pdf"})
    n_priv = _count(_ensure_private_dir(), _VIDEO_EXTS | _AUDIO_EXTS | _IMAGE_EXTS | _TEXT_EXTS | _ARCHIVE_EXTS | {".pdf"})

    def _tile(href: str, emoji: str, title: str, desc: str) -> str:
        return (f'<a class="tile" href="{href}"><div class="emoji">{emoji}</div>'
                f'<div class="title">{html.escape(title)}</div>'
                f'<div class="desc">{html.escape(desc)}</div></a>')

    parts_t: list[str] = []
    if ctx is not None:
        if user_auth.feature_allowed(ctx, "fe_pdf"):
            parts_t.append(_tile(f'/browse?path={_q(_DIR_PDF)}', '📕', 'PDF 阅读', f'共 {n_pdf} 本'))
        if user_auth.feature_allowed(ctx, "fe_video"):
            parts_t.append(_tile(f'/browse?path={_q(_DIR_VIDEO)}', '🎬', '视频', f'共 {n_vid} 个'))
        if user_auth.feature_allowed(ctx, "fe_music"):
            parts_t.append(_tile('/music', '🎵', '音乐播放器', f'共 {n_music} 首'))
        if user_auth.feature_allowed(ctx, "fe_private") and user_auth.can_browse_private_dir(ctx):
            parts_t.append(_tile(f'/browse?path={_q(_DIR_PRIVATE)}', '🔐', '私密目录 ' + _DIR_PRIVATE, f'共 {n_priv} 个'))
        if user_auth.feature_allowed(ctx, "fe_upload"):
            parts_t.append(_tile('/upload', '📤', '上传到 ' + _DIR_UPLOAD, f'已存 {n_upload} 个文件（上传后可在 u/ 浏览）'))
        if user_auth.feature_allowed(ctx, "fe_browse"):
            parts_t.append(_tile('/browse', '📁', '全部文件', '文件浏览器'))
    grid_inner = "".join(parts_t) if parts_t else '<div class="card" style="grid-column:1/-1"><p class="muted" style="margin:0">当前账号未开启任何入口，请联系管理员。</p></div>'
    nav: list[str] = ['<span class="spacer"></span>']
    if ctx is not None:
        nav.append(f'<span class="muted" style="margin-right:8px">{html.escape(ctx.username)}</span>')
        if ctx.is_admin:
            nav.append('<a class="btn btn-ghost btn-sm" href="/__admin">管理</a>')
        nav.append('<a class="btn btn-ghost btn-sm" href="/__logout">退出</a>')
        if user_auth.feature_allowed(ctx, "fe_browse"):
            nav.insert(1, '<a class="btn btn-ghost btn-sm" href="/browse">文件浏览</a>')
    hero_greet = "欢迎回来"
    if ctx is not None:
        hero_greet = f"你好，<strong>{html.escape(ctx.username)}</strong>"
    body = f"""
<style>
.hero{{position:relative;padding:22px 24px;margin:0 0 18px;border-radius:var(--radius-lg);
  background:linear-gradient(135deg,rgba(95,161,255,.18),rgba(185,124,255,.16) 60%,rgba(255,122,168,.12));
  border:1px solid rgba(255,255,255,.16);
  box-shadow:var(--shadow-md), inset 0 1px 0 rgba(255,255,255,.1);
  backdrop-filter:blur(20px) saturate(1.4);-webkit-backdrop-filter:blur(20px) saturate(1.4);
  overflow:hidden}}
.hero:before{{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  background:radial-gradient(50% 60% at 0% 0%,rgba(255,255,255,.15),transparent 70%)}}
.hero h1{{margin:0;font-size:clamp(1.2rem,3vw,1.6rem);font-weight:800;letter-spacing:.005em}}
.hero .sub{{margin-top:6px;color:rgba(236,241,255,.72);font-size:.9rem;line-height:1.5;word-break:break-all}}
</style>
<div class="topbar">
  <span class="brand">VerPadProxy<small>{html.escape(str(root))}</small></span>
  {"".join(nav)}
</div>
<div class="content">
  <div class="hero">
    <h1>{hero_greet}</h1>
    <div class="sub">在受限网络环境里安全、私密地阅读你的本地内容。请选择下方入口。</div>
  </div>
  <div class="grid">
    {grid_inner}
  </div>
</div>"""
    return _html_response(_shell("VerPadProxy", body))


# ---------------------------------------------------------------------------
# 文件浏览器
# ---------------------------------------------------------------------------

def _breadcrumbs_html(cur: Path, ctx: user_auth.UserCtx | None) -> str:
    # 无全库浏览时，「根」链到首页，避免 403
    root_browse = "/browse"
    if ctx is not None and not user_auth.feature_allowed(ctx, "fe_browse"):
        root_browse = "/"
    parts: list[str] = [
        f'<a href="/">🏠 首页</a><span class="sep">/</span><a href="{html.escape(root_browse)}">根目录</a>',
    ]
    root = _share_root()
    if cur != root:
        accum = Path()
        for seg in _rel_of(cur).split("/"):
            if not seg:
                continue
            accum = accum / seg
            href = f"/browse?path={_q(str(accum).replace(chr(92), '/'))}"
            parts.append(f'<span class="sep">/</span><a href="{href}">{html.escape(seg)}</a>')
    return '<div class="breadcrumbs">' + "".join(parts) + "</div>"


def _sort_entries(entries: list[Path], sort: str) -> list[Path]:
    if sort == "size":
        return sorted(entries, key=lambda p: (0 if p.is_dir() else 1, -_safe_size(p)))
    if sort == "mtime":
        return sorted(entries, key=lambda p: (0 if p.is_dir() else 1, -_safe_mtime(p)))
    if sort == "type":
        return sorted(entries, key=lambda p: (0 if p.is_dir() else 1, _classify(p) if p.is_file() else "", p.name.lower()))
    return sorted(entries, key=lambda p: (0 if p.is_dir() else 1, p.name.lower()))


def _browse_response(flow) -> Response:
    ctx = user_auth.get_user_ctx_from_flow(flow)
    root = _share_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

    rel = _query_first(flow, "path", "dir", "mitm_path")
    sort = _query_first(flow, "sort") or "name"
    qtext = _query_first(flow, "q").lower()

    cur = _safe_child(root, rel)
    if cur is None:
        return _error_page("路径非法。")
    if not cur.exists():
        return _error_page("目录不存在。")
    if not cur.is_dir():
        return _dispatch_open(flow, cur)

    try:
        entries = list(cur.iterdir())
    except OSError:
        entries = []
    if qtext:
        entries = [e for e in entries if qtext in e.name.lower()]
    entries = _sort_entries(entries, sort)

    parts: list[str] = [
        f'<div class="topbar"><a class="btn btn-ghost btn-sm" href="/">🏠</a>'
        f'<span class="brand">文件浏览<small>{html.escape(str(root))}</small></span>'
        '<span class="spacer"></span>'
        '<a class="btn btn-ghost btn-sm" href="?sort=name">名</a>'
        '<a class="btn btn-ghost btn-sm" href="?sort=mtime">时间</a>'
        '<a class="btn btn-ghost btn-sm" href="?sort=size">大小</a>'
        '</div>',
        '<div class="content">',
        '<div class="card">',
        _breadcrumbs_html(cur, ctx),
        f"""
<form method="get" action="/browse" class="row" style="margin-top:10px">
  <input type="hidden" name="path" value="{html.escape(_rel_of(cur))}">
  <input type="hidden" name="sort" value="{html.escape(sort)}">
  <input class="input" name="q" value="{html.escape(qtext)}" placeholder="搜索当前目录..." style="flex:1;min-width:180px">
  <button class="btn btn-primary" type="submit">搜索</button>
  {('<a class="btn btn-ghost" href="/browse?path=' + quote(_rel_of(cur)) + '">清除</a>') if qtext else ''}
</form>""",
        '</div>',
        '<div class="card" style="padding:0;overflow:hidden">',
    ]
    if not entries:
        parts.append('<div class="empty">（空）</div>')
    else:
        rows = ['<table class="files"><thead><tr>'
                '<th>名称</th><th>大小</th><th>修改时间</th><th>操作</th>'
                '</tr></thead><tbody>']
        for p in entries:
            try:
                st = p.stat()
            except OSError:
                continue
            rel_q = _q(p)
            name_esc = html.escape(p.name)
            if p.is_dir():
                rows.append(
                    f'<tr><td class="name">{_icon_of("dir")} <a href="/browse?path={rel_q}">{name_esc}</a></td>'
                    f'<td class="size">—</td>'
                    f'<td class="mtime">{_fmt_mtime(st.st_mtime)}</td>'
                    f'<td class="ops"><a href="/browse?path={rel_q}">进入</a></td></tr>'
                )
                continue
            kind = _classify(p)
            ops = []
            if kind == "pdf":
                ops.append(f'<a href="/pdf?path={rel_q}">阅读</a>')
            elif kind in ("video", "audio"):
                ops.append(f'<a href="/video?path={rel_q}">播放</a>')
            elif kind == "image":
                ops.append(f'<a href="/image?path={rel_q}">查看</a>')
            elif kind == "text":
                ops.append(f'<a href="/text?path={rel_q}">查看</a>')
            tag_html = f'<span class="tag">{kind}</span>' if kind != "binary" else ""
            rows.append(
                f'<tr><td class="name">{_icon_of(kind)} '
                f'<a href="/open?path={rel_q}">{name_esc}</a> {tag_html}</td>'
                f'<td class="size">{_fmt_size(st.st_size)}</td>'
                f'<td class="mtime">{_fmt_mtime(st.st_mtime)}</td>'
                f'<td class="ops">{" ".join(ops)}</td></tr>'
            )
        rows.append("</tbody></table>")
        parts.append("".join(rows))
    parts.extend(["</div>", "</div>"])
    return _html_response(_shell("文件浏览器", "".join(parts)))


# ---------------------------------------------------------------------------
# PDF 渲染
# ---------------------------------------------------------------------------

def _fitz_importable() -> bool:
    try:
        import fitz  # noqa: F401
        return True
    except ImportError:
        return False


def _pdf_count_subprocess(pdf_path: Path) -> int | None:
    cmd = [_pymupdf_python_exe(), _PDF_RENDER_HELPER, "--count", str(pdf_path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120,
                           text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return max(0, int((r.stdout or "").strip()))
    except ValueError:
        return None


def _pdf_png_subprocess(pdf_path: Path, idx: int, scale: float) -> tuple[bytes | None, str]:
    cmd = [_pymupdf_python_exe(), _PDF_RENDER_HELPER, str(pdf_path), str(idx), str(scale)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, "子进程渲染超时"
    except OSError as e:
        return None, str(e)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        return None, err or f"退出码 {r.returncode}"
    if not r.stdout:
        return None, "子进程无 PNG 输出"
    return r.stdout, ""


def _pdf_count_pdfinfo(pdf_path: Path) -> int | None:
    """poppler 的 pdfinfo 输出 'Pages: N'。"""
    exe = _which("pdfinfo")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, str(pdf_path)], capture_output=True, timeout=30,
                           text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def _pdf_count_mutool(pdf_path: Path) -> int | None:
    """mutool info / mutool pages 输出页数。"""
    exe = _which("mutool")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "info", str(pdf_path)], capture_output=True, timeout=30,
                           text=True, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    for line in (r.stdout or "").splitlines():
        # 常见：'Pages: 123'
        if "Pages" in line and ":" in line:
            try:
                return int(line.split(":", 1)[1].strip().split()[0])
            except (ValueError, IndexError):
                continue
    return None


def _pdf_count(pdf_path: Path) -> tuple[int | None, str]:
    if _fitz_importable():
        try:
            import fitz
            doc = fitz.open(pdf_path)
            n = len(doc)
            doc.close()
            return n, ""
        except Exception as e:  # noqa: BLE001
            return None, str(e)
    n = _pdf_count_subprocess(pdf_path)
    if n is not None:
        return n, ""
    n = _pdf_count_pdfinfo(pdf_path)
    if n is not None:
        return n, ""
    n = _pdf_count_mutool(pdf_path)
    if n is not None:
        return n, ""
    return None, "需要以下任一工具：PyMuPDF / poppler(pdfinfo) / mupdf-tools(mutool)"


@lru_cache(maxsize=128)
def _cached_png_inproc(sp: str, mtime: float, idx: int, scale: float) -> bytes | None:
    if not _fitz_importable():
        return None
    p = Path(sp)
    try:
        if p.stat().st_mtime != mtime:
            return None
    except OSError:
        return None
    try:
        import fitz
        doc = fitz.open(p)
        if idx >= len(doc):
            idx = max(0, len(doc) - 1)
        page = doc.load_page(idx)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        png = pix.tobytes("png")
        doc.close()
        return png
    except Exception:  # noqa: BLE001
        return None


@lru_cache(maxsize=128)
def _cached_png_subproc(sp: str, mtime: float, idx: int, scale: float) -> bytes | None:
    p = Path(sp)
    try:
        if p.stat().st_mtime != mtime:
            return None
    except OSError:
        return None
    png, _err = _pdf_png_subprocess(p, idx, scale)
    return png


def _pdf_page_png_pdftoppm(pdf_path: Path, idx: int, scale: float) -> tuple[bytes | None, str]:
    """用 poppler 的 pdftoppm 渲染一页为 PNG。"""
    import tempfile
    exe = _which("pdftoppm")
    if not exe:
        return None, "pdftoppm 不可用"
    dpi = max(72, int(round(72.0 * scale)))
    page = idx + 1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            prefix = str(Path(tmp) / "p")
            r = subprocess.run(
                [exe, "-f", str(page), "-l", str(page), "-r", str(dpi),
                 "-png", str(pdf_path), prefix],
                capture_output=True, timeout=180,
            )
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                return None, err or f"pdftoppm 退出码 {r.returncode}"
            # pdftoppm 输出 prefix-NN.png（位数不定）
            pngs = sorted(Path(tmp).glob("p-*.png"))
            if not pngs:
                return None, "pdftoppm 未生成 PNG"
            return pngs[0].read_bytes(), ""
    except subprocess.TimeoutExpired:
        return None, "pdftoppm 渲染超时"
    except OSError as e:
        return None, f"pdftoppm 调用失败: {e}"


def _pdf_page_png_mutool(pdf_path: Path, idx: int, scale: float) -> tuple[bytes | None, str]:
    """用 mupdf-tools 的 mutool draw 渲染一页为 PNG。"""
    import tempfile
    exe = _which("mutool")
    if not exe:
        return None, "mutool 不可用"
    dpi = max(72, int(round(72.0 * scale)))
    page = idx + 1
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "p.png")
            r = subprocess.run(
                [exe, "draw", "-o", out, "-r", str(dpi), str(pdf_path), str(page)],
                capture_output=True, timeout=180,
            )
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
                return None, err or f"mutool 退出码 {r.returncode}"
            if not Path(out).is_file():
                return None, "mutool 未生成 PNG"
            return Path(out).read_bytes(), ""
    except subprocess.TimeoutExpired:
        return None, "mutool 渲染超时"
    except OSError as e:
        return None, f"mutool 调用失败: {e}"


@lru_cache(maxsize=128)
def _cached_png_pdftoppm(sp: str, mtime: float, idx: int, scale: float) -> bytes | None:
    p = Path(sp)
    try:
        if p.stat().st_mtime != mtime:
            return None
    except OSError:
        return None
    png, _ = _pdf_page_png_pdftoppm(p, idx, scale)
    return png


@lru_cache(maxsize=128)
def _cached_png_mutool(sp: str, mtime: float, idx: int, scale: float) -> bytes | None:
    p = Path(sp)
    try:
        if p.stat().st_mtime != mtime:
            return None
    except OSError:
        return None
    png, _ = _pdf_page_png_mutool(p, idx, scale)
    return png


def _pdf_page_png(pdf_path: Path, idx: int, scale: float) -> tuple[bytes | None, str]:
    if _reload_flag():
        _cached_png_inproc.cache_clear()
        _cached_png_subproc.cache_clear()
        _cached_png_pdftoppm.cache_clear()
        _cached_png_mutool.cache_clear()
    try:
        mt = pdf_path.stat().st_mtime
    except OSError:
        return None, "文件无法读取"
    sp = str(pdf_path.resolve())
    last_err = ""
    if _fitz_importable():
        png = _cached_png_inproc(sp, mt, idx, scale)
        if png is not None:
            return png, ""
    png = _cached_png_subproc(sp, mt, idx, scale)
    if png is not None:
        return png, ""
    # pymupdf 不可用时走外部命令
    png = _cached_png_pdftoppm(sp, mt, idx, scale)
    if png is not None:
        return png, ""
    png, err = _pdf_page_png_pdftoppm(pdf_path, idx, scale)
    if png is not None:
        return png, ""
    last_err = err or last_err
    png = _cached_png_mutool(sp, mt, idx, scale)
    if png is not None:
        return png, ""
    png, err = _pdf_page_png_mutool(pdf_path, idx, scale)
    if png is not None:
        return png, ""
    last_err = err or last_err
    # 子进程调 helper 的 PyMuPDF 路径的错误（若最前面出现过）
    _, pymupdf_err = _pdf_png_subprocess(pdf_path, idx, scale)
    if pymupdf_err:
        last_err = last_err or pymupdf_err
    return None, last_err or "没有可用的 PDF 渲染器（请装 poppler 或 mupdf-tools 或 PyMuPDF）"


_ASSETS_ROOT = _BASE / "assets"
_ASSETS_MIME = {
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".html": "text/html; charset=utf-8",
}


def _assets_response(url_path: str) -> Response:
    rel = url_path[len("/assets/"):].strip("/").replace("\\", "/")
    if not rel or ".." in rel.split("/"):
        return _error_page("非法资源路径。", status=400)
    target = (_ASSETS_ROOT / rel).resolve()
    try:
        target.relative_to(_ASSETS_ROOT.resolve())
    except ValueError:
        return _error_page("非法资源路径。", status=400)
    if not target.is_file():
        return _error_page("资源不存在。", status=404)
    try:
        data = target.read_bytes()
    except OSError:
        return _error_page("资源读取失败。", status=500)
    mime = _ASSETS_MIME.get(target.suffix.lower(), "application/octet-stream")
    headers = {
        "Content-Type": mime,
        "Content-Length": str(len(data)),
        "Cache-Control": "public, max-age=604800",
    }
    return Response.make(200, data, headers)


def _pdf_pdfjs_response(pdf_path: Path) -> Response:
    """用本地 pdf.js 在浏览器里渲染 PDF（不依赖 PyMuPDF）。"""
    rel = _rel_of(pdf_path)
    rel_q = quote(rel)
    file_href = f"/file?path={rel_q}"
    title = html.escape(pdf_path.name)
    css = _BASE_CSS + """
html,body{height:100%}
body{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh;margin:0;background:#000}
.stage{flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center;padding:16px;touch-action:pan-x pan-y pinch-zoom}
#pdf-canvas{display:block;max-width:100%;height:auto;background:#fff;box-shadow:0 4px 24px rgba(0,0,0,.6);border-radius:4px}
.pager{position:fixed;right:14px;bottom:14px;display:flex;gap:6px;align-items:center;background:rgba(20,20,20,.88);padding:6px 10px;border-radius:999px;box-shadow:0 4px 14px rgba(0,0,0,.5);color:#fff}
.pager button{padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.2);background:transparent;color:#fff;min-width:32px;font-size:1rem}
.pager button:disabled{opacity:.35}
.pager input{width:3.2rem;padding:4px 8px;border-radius:10px;border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.3);color:#fff;font-size:.95rem;text-align:center}
#loading,#error{color:#fff;padding:20px;text-align:center}
#error{color:#f88}
"""
    topbar = f"""
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="{_back_href(pdf_path)}">← 返回</a>
  <span class="brand" title="{title}">{title}</span>
  <span class="spacer"></span>
  <a class="btn btn-ghost btn-sm" href="{file_href}" target="_blank" rel="noopener">原始 PDF</a>
</div>"""
    # 单独放出变量，避免大段 f-string 嵌套
    body = f"""
<style>{css}</style>
{topbar}
<div class="stage" id="stage">
  <div id="loading">加载中…</div>
  <canvas id="pdf-canvas" style="display:none"></canvas>
  <div id="error" style="display:none"></div>
</div>
<div class="pager" id="pager" style="display:none">
  <button id="prev" title="上一页">‹</button>
  <input id="page-input" type="number" min="1" step="1" value="1">
  <span id="total">/ 0</span>
  <button id="next" title="下一页">›</button>
</div>
<script src="/assets/pdfjs/pdf.min.js"></script>
<script>
(function(){{
  if (typeof pdfjsLib === 'undefined') {{
    document.getElementById('loading').style.display='none';
    var e=document.getElementById('error');
    e.style.display='block';
    e.textContent='加载 pdf.js 失败，请确认 /assets/pdfjs/pdf.min.js 可访问。';
    return;
  }}
  pdfjsLib.GlobalWorkerOptions.workerSrc = '/assets/pdfjs/pdf.worker.min.js';

  var url = {json.dumps(file_href)};
  var canvas = document.getElementById('pdf-canvas');
  var ctx = canvas.getContext('2d');
  var pdfDoc = null;
  var currentPage = 1;
  var totalPages = 0;
  var rendering = false;
  var pending = null;

  function showError(msg){{
    document.getElementById('loading').style.display='none';
    var e=document.getElementById('error');
    e.style.display='block';
    e.textContent=msg;
  }}

  function renderPage(num){{
    if(!pdfDoc){{return;}}
    rendering=true;
    pdfDoc.getPage(num).then(function(page){{
      var dpr = Math.min(window.devicePixelRatio || 1, 2);
      var stage = document.getElementById('stage');
      var baseW = stage.clientWidth - 32;
      var base = page.getViewport({{scale:1}});
      var scale = (baseW / base.width) * dpr;
      if(scale <= 0) scale = 1.5;
      var viewport = page.getViewport({{scale:scale}});
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      canvas.style.width = (viewport.width/dpr) + 'px';
      canvas.style.height = (viewport.height/dpr) + 'px';
      page.render({{canvasContext:ctx, viewport:viewport}}).promise.then(function(){{
        rendering=false;
        if(pending!==null){{var n=pending;pending=null;renderPage(n);}}
      }}).catch(function(err){{rendering=false;showError('渲染失败: '+err);}});
      document.getElementById('page-input').value = num;
      currentPage = num;
    }}).catch(function(err){{rendering=false;showError('读取页失败: '+err);}});
  }}

  function goPage(num){{
    if(num<1||num>totalPages) return;
    if(rendering){{pending=num;return;}}
    renderPage(num);
  }}

  pdfjsLib.getDocument({{url:url, disableRange:false, disableStream:false}}).promise.then(function(pdf){{
    pdfDoc = pdf;
    totalPages = pdf.numPages;
    document.getElementById('loading').style.display='none';
    canvas.style.display='block';
    document.getElementById('pager').style.display='flex';
    document.getElementById('total').textContent = '/ ' + totalPages;
    document.getElementById('page-input').max = totalPages;
    renderPage(1);
  }}).catch(function(err){{
    showError('加载 PDF 失败: '+err);
  }});

  document.getElementById('prev').addEventListener('click', function(){{
    if(currentPage>1) goPage(currentPage-1);
  }});
  document.getElementById('next').addEventListener('click', function(){{
    if(currentPage<totalPages) goPage(currentPage+1);
  }});
  document.getElementById('page-input').addEventListener('change', function(){{
    var n = parseInt(this.value||'1',10);
    if(!isNaN(n)) goPage(n);
  }});
  window.addEventListener('keydown', function(e){{
    if(e.key==='ArrowLeft' || e.key==='PageUp') goPage(currentPage-1);
    else if(e.key==='ArrowRight' || e.key==='PageDown' || e.key===' ') goPage(currentPage+1);
  }});
}})();
</script>
"""
    return _html_response(_shell(f"PDF - {pdf_path.name}", body, raw=True))


def _pdf_reader_response(flow, pdf_path: Path) -> Response:
    total, err = _pdf_count(pdf_path)
    if total is None or total <= 0:
        # 栅格化链路全挂时的最终兜底：用本地 pdf.js（客户端浏览器若能撑住就能看）
        return _pdf_pdfjs_response(pdf_path)

    # 页码优先级：URL 显式 ?pdfpage / ?p / ?page > 用户上次阅读 > 1
    p_raw: int | None = None
    zb = _query_first(flow, "pdfpage", "mitm_page")
    if zb != "":
        try:
            p_raw = max(1, int(zb) + 1)
        except ValueError:
            p_raw = None
    if p_raw is None:
        v = _query_first(flow, "p", "page", "mitm_goto")
        if v != "":
            try:
                p_raw = max(1, int(v))
            except ValueError:
                p_raw = None
    if p_raw is None:
        ctx = user_auth.get_user_ctx_from_flow(flow)
        user = (getattr(ctx, "username", "") or "").strip() if ctx else ""
        rel_lookup = _rel_of(pdf_path)
        saved = _pdf_progress_get(user, rel_lookup) if user else None
        if isinstance(saved, int) and saved > 0:
            p_raw = saved
    if p_raw is None:
        p_raw = 1
    p_raw = max(1, min(total, p_raw))
    idx = p_raw - 1

    png, perr = _pdf_page_png(pdf_path, idx, _raster_scale())
    if png is None:
        return _error_page(f"渲染第 {p_raw} 页失败：{perr}")
    b64 = base64.b64encode(png).decode("ascii")

    rel = _rel_of(pdf_path)
    rel_obf = _obfuscate(rel)
    rel_q = quote(rel_obf)  # 已加密 → 直接 url-encode 后嵌入
    rel_stable_key = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]  # 给 localStorage 做稳定 key
    prev_href = f"/pdf?path={rel_q}&p={max(1, p_raw - 1)}"
    next_href = f"/pdf?path={rel_q}&p={min(total, p_raw + 1)}"

    css = _BASE_CSS + _VIEWER_CSS + """
body{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}
"""
    topbar = f"""
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="{_back_href(pdf_path)}">← 返回</a>
  <span class="brand" title="{html.escape(pdf_path.name)}">{html.escape(pdf_path.name)}</span>
  <span class="spacer"></span>
  <span class="muted">{p_raw} / {total}</span>
</div>"""
    stage = f"""
<div class="stage" id="stage">
  <div class="stage-inner" id="stage-inner">
    <img id="pdf-page-img" alt="第{p_raw}页" src="data:image/png;base64,{b64}" draggable="false" decoding="async" loading="eager">
  </div>
</div>"""
    pager = f"""
<div class="pager" aria-label="翻页">
  <a id="pdf-prev" class="btn btn-sm {'disabled' if p_raw <= 1 else ''}" href="{prev_href}">‹</a>
  <form id="pdf-pager-form" method="get" action="/pdf">
    <input type="hidden" name="path" value="{html.escape(rel_obf)}">
    <input id="pdf-page-input" type="number" min="1" max="{total}" name="p" value="{p_raw}" inputmode="numeric" required>
    <button type="submit" class="btn btn-primary btn-sm">转</button>
  </form>
  <a id="pdf-next" class="btn btn-sm {'disabled' if p_raw >= total else ''}" href="{next_href}">›</a>
</div>"""
    tools = _viewer_tools_html(include_rotate=True)
    rel_js = json.dumps(rel_obf, ensure_ascii=False)
    rel_key_js = json.dumps(rel_stable_key)
    body = f'<style>{css}</style>{topbar}{stage}{pager}{tools}'
    nav_script = (
        "<script>(function(){"
        "var img=document.getElementById('pdf-page-img');"
        "var pageInput=document.getElementById('pdf-page-input');"
        "var prevA=document.getElementById('pdf-prev');"
        "var nextA=document.getElementById('pdf-next');"
        "var pagerForm=document.getElementById('pdf-pager-form');"
        "var totalLabel=document.querySelector('.topbar .muted');"
        f"var P_REL={rel_js};var P_REL_KEY={rel_key_js};var TOTAL={total};var cur=parseInt(pageInput.value,10)||1;"
        "var VIEW_KEY='mitm_pdf_view:'+P_REL_KEY;"
        "function pngUrl(p){return '/pdf.png?path='+encodeURIComponent(P_REL)+'&p='+p;}"
        "function pageHref(p){return '/pdf?path='+encodeURIComponent(P_REL)+'&p='+p;}"
        "function setDis(el,b){if(!el)return;if(b)el.classList.add('disabled');else el.classList.remove('disabled');}"
        "var cache={};"
        "function preload(p){if(p<1||p>TOTAL||cache[p])return;var i=new Image();i.decoding='async';i.src=pngUrl(p);cache[p]=i;}"
        "var saveTimer=null;"
        "function saveProgress(p){clearTimeout(saveTimer);saveTimer=setTimeout(function(){"
        "  try{fetch('/pdf_progress',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:P_REL,page:p})}).catch(function(){});}catch(e){}"
        "},700);}"
        "var viewSaveTimer=null;"
        "function saveView(){clearTimeout(viewSaveTimer);viewSaveTimer=setTimeout(function(){"
        "  try{var v=window.mitmGetView&&window.mitmGetView();if(v)localStorage.setItem(VIEW_KEY,JSON.stringify(v));}catch(e){}"
        "},250);}"
        "function restoreView(){"
        "  try{var v=JSON.parse(localStorage.getItem(VIEW_KEY)||'null');if(v&&window.mitmSetView)window.mitmSetView(v);}catch(e){}"
        "}"
        "function goto(p,opts){p=Math.max(1,Math.min(TOTAL,p));if(p===cur&&!(opts&&opts.force))return;cur=p;"
        "img.decoding='async';img.src=pngUrl(p);pageInput.value=String(p);"
        "if(totalLabel)totalLabel.textContent=p+' / '+TOTAL;"
        "setDis(prevA,p<=1);setDis(nextA,p>=TOTAL);"
        "prevA.setAttribute('href',pageHref(p-1));nextA.setAttribute('href',pageHref(p+1));"
        # 不再 mitmFit 重置：跨页保留缩放/位移/旋转
        "if(!(opts&&opts.skipPush)){try{history.pushState({page:p},'',pageHref(p));}catch(e){}}"
        "preload(p+1);preload(p-1);"
        "if(!(opts&&opts.skipSave))saveProgress(p);}"
        "function clickHandler(e){e.preventDefault();var h=this.getAttribute('href')||'';var m=/[?&]p=(\\d+)/.exec(h);"
        "if(m)goto(parseInt(m[1],10));}"
        "if(prevA)prevA.addEventListener('click',clickHandler);"
        "if(nextA)nextA.addEventListener('click',clickHandler);"
        "if(pagerForm)pagerForm.addEventListener('submit',function(e){e.preventDefault();goto(parseInt(pageInput.value,10)||1);});"
        "window.addEventListener('popstate',function(ev){var p=(ev.state&&ev.state.page)||cur;var m=/[?&]p=(\\d+)/.exec(location.search);if(m)p=parseInt(m[1],10);goto(p,{skipPush:true,force:true});});"
        "document.addEventListener('keydown',function(e){if(e.target&&/(input|textarea|select)/i.test(e.target.tagName))return;"
        "if(e.key==='ArrowLeft'||e.key==='PageUp'){e.preventDefault();goto(cur-1);}"
        "else if(e.key==='ArrowRight'||e.key==='PageDown'||e.key===' '){e.preventDefault();goto(cur+1);}});"
        "if(window.mitmOnViewChange)window.mitmOnViewChange(saveView);"
        "restoreView();"
        "preload(cur+1);preload(cur-1);"
        # 首次进入也写一次记忆，覆盖刚才用 ?p 进来的情况
        "saveProgress(cur);"
        "})();</script>"
    )
    script = f'<script>{_VIEWER_JS}</script>{nav_script}'
    return _html_response(_shell(f"PDF - {pdf_path.name}", body, extra_body_end=script, raw=True))


# ---------------------------------------------------------------------------
# 视频播放器（字幕 + 倍速 + 尺寸预设）
# ---------------------------------------------------------------------------

def _find_subtitles(video: Path) -> list[Path]:
    """查找同目录下与视频同前缀的 .srt/.vtt 字幕。"""
    stem = video.stem
    out: list[Path] = []
    try:
        for f in video.parent.iterdir():
            if not f.is_file():
                continue
            if f.suffix.lower() not in _SUB_EXTS:
                continue
            fname = f.stem
            if fname == stem or fname.startswith(stem + "."):
                out.append(f)
    except OSError:
        return []
    out.sort(key=lambda p: p.name.lower())
    return out


def _subtitle_lang_label(path: Path) -> tuple[str, str]:
    """从文件名 (movie.zh.srt / movie.en.srt) 推断 srclang / label。"""
    stem = path.stem
    if "." in stem:
        code = stem.rsplit(".", 1)[1].lower()
        mapping = {
            "zh": ("zh", "中文"), "chs": ("zh", "简中"), "cht": ("zh-Hant", "繁中"),
            "cn": ("zh", "中文"), "sc": ("zh", "简中"), "tc": ("zh-Hant", "繁中"),
            "en": ("en", "English"), "eng": ("en", "English"),
            "ja": ("ja", "日本語"), "jp": ("ja", "日本語"),
            "ko": ("ko", "한국어"),
        }
        if code in mapping:
            return mapping[code]
        return code, code
    return "und", path.stem


_SRT_TS_RE = re.compile(r"(\d\d:\d\d:\d\d),(\d{3})")


def _srt_to_vtt(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _SRT_TS_RE.sub(r"\1.\2", text)
    return "WEBVTT\n\n" + text


def _ass_time_to_vtt(ts: str) -> str:
    """ASS: H:MM:SS.cs（厘秒，2 位）→ VTT: HH:MM:SS.mmm。"""
    parts = ts.strip().split(":")
    if len(parts) != 3:
        return "00:00:00.000"
    h_str, m_str, s_cs = parts
    if "." in s_cs:
        s_str, cs_str = s_cs.split(".", 1)
    else:
        s_str, cs_str = s_cs, "0"
    try:
        h = int(h_str); m = int(m_str); s = int(s_str)
        cs = int((cs_str + "00")[:2])  # 总是取到两位厘秒
        ms = cs * 10  # 厘秒转毫秒
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
    except ValueError:
        return "00:00:00.000"


def _ass_strip_inline(text: str) -> str:
    # 去掉 ASS 的 {\...} 样式覆盖与硬换行 \N / \n
    text = re.sub(r"\{[^}]*\}", "", text)
    text = text.replace(r"\N", "\n").replace(r"\n", "\n").replace(r"\h", " ")
    return text


def _ass_to_vtt(text: str) -> str:
    """简版 ASS/SSA → WebVTT。识别 [Events] 中的 Dialogue 行。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = ["WEBVTT", ""]
    for raw in text.split("\n"):
        line = raw.strip()
        if not line.startswith("Dialogue:"):
            continue
        # Dialogue: Layer, Start, End, Style, Name, ML, MR, MV, Effect, Text
        # Text 字段可能含逗号；用 maxsplit=9 把前 9 个字段切开，剩下整段作为 Text。
        payload = line[len("Dialogue:"):].lstrip()
        fields = payload.split(",", 9)
        if len(fields) < 10:
            continue
        start = _ass_time_to_vtt(fields[1])
        end = _ass_time_to_vtt(fields[2])
        body = _ass_strip_inline(fields[9]).strip()
        if not body:
            continue
        out.append(f"{start} --> {end}")
        out.append(body)
        out.append("")
    return "\n".join(out) + "\n"


def _decode_subtitle_bytes(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _convert_text_to_vtt(text: str, suffix: str) -> str:
    suffix = (suffix or "").lower()
    stripped = text.lstrip()
    if stripped.startswith("WEBVTT"):
        return text
    if suffix in (".ass", ".ssa") or "[Events]" in text:
        return _ass_to_vtt(text)
    if suffix == ".srt" or _SRT_TS_RE.search(text):
        return _srt_to_vtt(text)
    return "WEBVTT\n\n" + text


def _load_subtitle_as_vtt(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    text = _decode_subtitle_bytes(raw)
    text = _convert_text_to_vtt(text, path.suffix)
    return text.encode("utf-8")


def _subtitle_response(flow) -> Response:
    rel = _query_first(flow, "path")
    if not rel:
        return _error_page("缺少参数。")
    root = _share_root()
    p = _safe_child(root, rel)
    if p is None or not p.is_file() or p.suffix.lower() not in _SUB_EXTS:
        return _error_page("字幕文件不存在。")
    data = _load_subtitle_as_vtt(p)
    if data is None:
        return _error_page("字幕读取失败。")
    headers = {
        "Content-Type": "text/vtt; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Length": str(len(data)),
    }
    if flow.request.method.upper() == "HEAD":
        return Response.make(200, b"", headers)
    return Response.make(200, data, headers)


_TRANS_CACHE_DIR_DEFAULT = _BASE / "cache" / "hls"
_TRANS_JOBS: dict[str, dict] = {}
_TRANS_LOCK = threading.Lock()
# HLS 段长：默认 2 秒（首屏快、buffer 颗粒细），改大数值会推后初次播放
_HLS_SEG_DURATION = float(os.environ.get("MITM_TRANS_SEG", "") or "2.0")
# 编码器：默认 libx264；可选 h264_v4l2m2m / h264_mediacodec / h264_omx 走硬编
_TRANS_VENC = (os.environ.get("MITM_TRANS_VENC", "") or "libx264").strip()
# 编码 preset：默认 ultrafast（编码速度最快 → 始终跑在播放前面，不会卡）
_TRANS_PRESET = (os.environ.get("MITM_TRANS_PRESET", "") or "ultrafast").strip()
# crf：默认 23（配合更小分辨率画质够用、文件不大）
_TRANS_CRF = (os.environ.get("MITM_TRANS_CRF", "") or "23").strip()
# 限制峰值码率（kbps），WiFi 与 CPU 双保险；为空则不限
_TRANS_MAXRATE = (os.environ.get("MITM_TRANS_MAXRATE", "") or "1500k").strip()
_TRANS_BUFSIZE = (os.environ.get("MITM_TRANS_BUFSIZE", "") or "3000k").strip()
# 输出最大高度（默认 540；想更清晰 → 720/1080；想更顺 → 360/480）
_TRANS_HEIGHT = (os.environ.get("MITM_TRANS_HEIGHT", "") or "540").strip()
# 音频码率（默认 96kbps；语音类视频甚至可降到 64k）
_TRANS_ABR = (os.environ.get("MITM_TRANS_ABR", "") or "96k").strip()
# 并发上限 + 引用计数 + 后台清理线程
_MAX_CONCURRENT_TRANS = max(1, _env_int("MITM_MAX_TRANS", 2))
_TRANS_VIEWERS: dict[str, dict[str, float]] = {}  # key -> {sid: last_ts}
_TRANS_VIEWERS_LOCK = threading.Lock()
_REAPER_STARTED = False
_VIEWER_TIMEOUT_SEC = 30.0  # 心跳超过该秒数视为离开
_REAPER_INTERVAL_SEC = 5.0


def _trans_active_jobs() -> int:
    n = 0
    for kk, job in list(_TRANS_JOBS.items()):
        proc = job.get("proc") if job else None
        if proc is not None and proc.poll() is None:
            n += 1
    return n


def _trans_kill_job(key: str, *, timeout: float = 3.0) -> None:
    job = _TRANS_JOBS.get(key)
    if not job:
        return
    proc = job.get("proc")
    if proc is None:
        with _TRANS_LOCK:
            _TRANS_JOBS.pop(key, None)
        return
    try:
        if proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except OSError:
                pass
    finally:
        with _TRANS_LOCK:
            _TRANS_JOBS.pop(key, None)


def _trans_viewers_count(key: str) -> int:
    with _TRANS_VIEWERS_LOCK:
        sids = _TRANS_VIEWERS.get(key) or {}
        now = time.time()
        # 顺手清掉超时的会话
        for sid, ts in list(sids.items()):
            if now - ts > _VIEWER_TIMEOUT_SEC:
                sids.pop(sid, None)
        if sids:
            _TRANS_VIEWERS[key] = sids
            return len(sids)
        _TRANS_VIEWERS.pop(key, None)
        return 0


def _trans_viewer_open(key: str, sid: str) -> None:
    if not key or not sid:
        return
    with _TRANS_VIEWERS_LOCK:
        d = _TRANS_VIEWERS.setdefault(key, {})
        d[sid] = time.time()


def _trans_viewer_close(key: str, sid: str) -> None:
    if not key or not sid:
        return
    with _TRANS_VIEWERS_LOCK:
        d = _TRANS_VIEWERS.get(key) or {}
        d.pop(sid, None)
        if not d:
            _TRANS_VIEWERS.pop(key, None)
        else:
            _TRANS_VIEWERS[key] = d


def _trans_reaper_loop() -> None:
    """每 5s 巡检：无活跃观众且未完成的转码任务终止；保留缓存以便复播。"""
    while True:
        try:
            time.sleep(_REAPER_INTERVAL_SEC)
            for key in list(_TRANS_JOBS.keys()):
                job = _TRANS_JOBS.get(key)
                if not job:
                    continue
                proc = job.get("proc")
                if proc is None:
                    continue
                if proc.poll() is not None:
                    continue  # 已结束，状态轮询会清理
                if _trans_viewers_count(key) > 0:
                    continue
                # 没人看了 → 给点缓冲（让用户切页时不要立刻杀）
                gone_since = job.get("no_viewer_since")
                if gone_since is None:
                    job["no_viewer_since"] = time.time()
                    continue
                if time.time() - gone_since >= _VIEWER_TIMEOUT_SEC:
                    _trans_kill_job(key)
            # 重新进入有观众则清掉退出计时
            for key, job in list(_TRANS_JOBS.items()):
                if _trans_viewers_count(key) > 0 and "no_viewer_since" in job:
                    job.pop("no_viewer_since", None)
        except Exception:  # noqa: BLE001
            # reaper 永不退出
            time.sleep(2)


def _start_reaper_once() -> None:
    global _REAPER_STARTED
    if _REAPER_STARTED:
        return
    _REAPER_STARTED = True
    t = threading.Thread(target=_trans_reaper_loop, name="mitm-trans-reaper", daemon=True)
    t.start()


def _trans_dur_file(p: Path) -> Path:
    return _trans_dir(p) / "duration"


def _ffprobe_duration_cached(p: Path) -> float:
    f = _trans_dur_file(p)
    if f.is_file():
        try:
            v = float(f.read_text().strip())
            if v > 0:
                return v
        except (OSError, ValueError):
            pass
    d = _ffprobe_duration(p) or 0.0
    if d > 0:
        try:
            f.write_text(f"{d:.6f}")
        except OSError:
            pass
    return d


def _trans_total_segments(p: Path) -> int:
    d = _ffprobe_duration_cached(p)
    if d <= 0:
        return 0
    n = int(d / _HLS_SEG_DURATION)
    if d - n * _HLS_SEG_DURATION > 0.001:
        n += 1
    return n


def _trans_virtual_m3u8(p: Path) -> str:
    """生成虚拟全长 m3u8，让 hls.js 知道总时长，从而支持任意 seek。"""
    d = _ffprobe_duration_cached(p)
    if d <= 0:
        return ""
    n = _trans_total_segments(p)
    seg_dur = _HLS_SEG_DURATION
    target_dur = int(seg_dur) + 1
    out = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_dur}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i in range(n):
        if i == n - 1:
            seg_len = max(0.1, d - i * seg_dur)
        else:
            seg_len = seg_dur
        out.append(f"#EXTINF:{seg_len:.3f},")
        out.append(f"seg_{i:05d}.ts")
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"


def _trans_cache_dir() -> Path:
    raw = (os.environ.get("MITM_TRANS_CACHE_DIR", "") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return _TRANS_CACHE_DIR_DEFAULT


def _trans_encode_sig() -> str:
    """编码参数指纹：参数变了就强制重转（旧缓存自动失效）。"""
    return (f"{_TRANS_VENC}|{_TRANS_PRESET}|{_TRANS_CRF}|{_TRANS_HEIGHT}|"
            f"{_TRANS_MAXRATE}|{_TRANS_ABR}|seg{_HLS_SEG_DURATION}")


def _trans_key(p: Path) -> str:
    try:
        st = p.stat()
        sig = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}|{_trans_encode_sig()}"
    except OSError:
        sig = f"{p}|{_trans_encode_sig()}"
    return hashlib.sha1(sig.encode("utf-8", errors="replace")).hexdigest()


def _trans_dir(p: Path) -> Path:
    """每个视频一个独立子目录：cache/hls/{key}/"""
    base = _trans_cache_dir() / _trans_key(p)
    base.mkdir(parents=True, exist_ok=True)
    # 落一份源文件路径，供 /hls/{key}/* 反查
    ref = base / "source.ref"
    try:
        if not ref.is_file():
            ref.write_text(str(p.resolve()), encoding="utf-8")
    except OSError:
        pass
    return base


def _trans_playlist(p: Path) -> Path:
    return _trans_dir(p) / "index.m3u8"


def _trans_progress_file(p: Path) -> Path:
    return _trans_dir(p) / "ffmpeg.progress"


def _trans_finished_marker(p: Path) -> Path:
    return _trans_dir(p) / "DONE"


def _ffprobe_duration(p: Path) -> float | None:
    exe = _which("ffprobe")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(p)],
            capture_output=True, timeout=15, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


def _pick_burn_image_sub(p: Path) -> int:
    """选择第一个图像字幕（PGS 等）的 stream 索引；没有就返回 -1。"""
    subs = _probe_subtitles(p)
    for s in subs:
        if s.get("kind") == "image":
            return s["idx"]
    return -1


def _trans_start(p: Path, start_seg: int = 0) -> dict:
    """启动 ffmpeg。从 start_seg 段开始切片（原视频时间 = start_seg * SEG_DURATION）。"""
    _start_reaper_once()
    key = _trans_key(p)
    out_dir = _trans_dir(p)
    done = _trans_finished_marker(p)
    total_segs = _trans_total_segments(p)
    if total_segs == 0:
        return {"error": "无法读取视频时长（ffprobe 失败）"}
    if start_seg < 0:
        start_seg = 0
    if start_seg >= total_segs:
        start_seg = max(0, total_segs - 1)

    target_seg_file = out_dir / f"seg_{start_seg:05d}.ts"

    # 缓存命中：直接 ready，不启动 ffmpeg
    if done.is_file() or (total_segs > 0 and _trans_count_ts(p) >= total_segs):
        try:
            done.touch()
        except OSError:
            pass
        return {"ready": True, "playlist": f"/hls/{key}/index.m3u8", "progress": 100,
                "cache_hit": True, "start_seg": start_seg}

    with _TRANS_LOCK:
        job = _TRANS_JOBS.get(key)
        if job is not None:
            proc = job.get("proc")
            existing_start = int(job.get("start_seg", -1))
            if proc is not None and proc.poll() is None:
                # 同一起点的任务正在跑 → 不打断
                if existing_start == start_seg:
                    return {"running": True, "playlist": f"/hls/{key}/index.m3u8",
                            "progress": int(job.get("progress", 0)),
                            "start_seg": start_seg}
                # 不同起点 → 立即终止重启
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
                except OSError:
                    pass
            _TRANS_JOBS.pop(key, None)

        # 并发上限：超出就排队（前端会重试 status）
        if _trans_active_jobs() >= _MAX_CONCURRENT_TRANS:
            return {"queued": True, "playlist": f"/hls/{key}/index.m3u8",
                    "progress": 0, "start_seg": start_seg,
                    "msg": f"已有 {_MAX_CONCURRENT_TRANS} 个转码任务在跑，正在排队…"}

        exe = _which("ffmpeg")
        if not exe:
            return {"error": "未安装 ffmpeg。请在 Termux 执行：pkg install -y ffmpeg"}

        # 仅在从 0 开始时清旧 done 标记 / 进度文件（保留已生成的 ts 段，避免重转）
        progress_path = _trans_progress_file(p)
        if progress_path.exists():
            try:
                progress_path.unlink()
            except OSError:
                pass
        if start_seg == 0 and done.exists():
            try:
                done.unlink()
            except OSError:
                pass

        seg_pattern = str(out_dir / "seg_%05d.ts")
        run_dir = out_dir / "_run"
        run_dir.mkdir(parents=True, exist_ok=True)
        run_playlist = str(run_dir / f"r_{start_seg:05d}.m3u8")
        offset = start_seg * _HLS_SEG_DURATION
        cmd = [exe, "-y"]
        if start_seg > 0:
            cmd += ["-ss", f"{offset:.3f}"]
        # 图像字幕（PGS/HDMV）只能烧录到画面里，否则播放器无法显示
        burn_sub_idx = _pick_burn_image_sub(p)
        kf_dur = max(1, int(_HLS_SEG_DURATION))
        gop = max(24, int(_HLS_SEG_DURATION * 24))
        if _TRANS_VENC == "libx264":
            venc_args = ["-c:v", "libx264", "-preset", _TRANS_PRESET, "-tune", "fastdecode",
                         "-crf", _TRANS_CRF, "-profile:v", "main", "-level", "3.1",
                         "-x264-params", "no-scenecut=1:rc-lookahead=10",
                         "-threads", "0"]
            if _TRANS_MAXRATE and _TRANS_BUFSIZE:
                venc_args += ["-maxrate", _TRANS_MAXRATE, "-bufsize", _TRANS_BUFSIZE]
        else:
            venc_args = ["-c:v", _TRANS_VENC, "-b:v", _TRANS_MAXRATE or "1500k",
                         "-maxrate", _TRANS_MAXRATE or "1500k",
                         "-bufsize", _TRANS_BUFSIZE or "3000k"]
        common_tail = [
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-force_key_frames", f"expr:gte(t,n_forced*{kf_dur})",
            "-c:a", "aac", "-b:a", _TRANS_ABR, "-ac", "2", "-ar", "44100",
            "-vsync", "cfr",
        ]
        if burn_sub_idx >= 0:
            cmd += [
                "-fflags", "+genpts",
                "-i", str(p),
                "-filter_complex",
                (f"[0:v:0]scale=-2:'min({_TRANS_HEIGHT},ih)',format=yuv420p[v0];"
                 f"[0:s:{burn_sub_idx}]copy[s0];"
                 f"[v0][s0]overlay[v]"),
                "-map", "[v]", "-map", "0:a:0?",
                *venc_args,
                *common_tail,
            ]
        else:
            cmd += [
                "-fflags", "+genpts",
                "-i", str(p),
                "-map", "0:v:0", "-map", "0:a:0?",
                *venc_args,
                "-vf", f"scale=-2:'min({_TRANS_HEIGHT},ih)'",
                "-pix_fmt", "yuv420p",
                *common_tail,
            ]
        if start_seg > 0:
            # 让段内 PTS 对齐到原始时间，避免 hls.js 段间衔接错位
            cmd += ["-output_ts_offset", f"{offset:.3f}"]
        cmd += [
            "-f", "hls",
            "-hls_time", str(int(_HLS_SEG_DURATION)),
            "-hls_list_size", "0",
            "-hls_flags", "independent_segments+temp_file",
            "-hls_segment_filename", seg_pattern,
            "-start_number", str(start_seg),
            "-progress", str(progress_path),
            "-loglevel", "error",
            run_playlist,
        ]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError as e:
            return {"error": f"启动 ffmpeg 失败: {e}"}

        _TRANS_JOBS[key] = {
            "proc": proc,
            "started": time.time(),
            "out_dir": out_dir,
            "duration": _ffprobe_duration_cached(p),
            "progress_path": progress_path,
            "progress": 0,
            "progress_seg": start_seg - 1,
            "start_seg": start_seg,
        }
    return {"running": True, "playlist": f"/hls/{key}/index.m3u8",
            "progress": 0, "start_seg": start_seg}


def _trans_count_ts(p: Path) -> int:
    out_dir = _trans_dir(p)
    try:
        return sum(1 for _ in out_dir.glob("seg_*.ts"))
    except OSError:
        return 0


def _trans_status(p: Path) -> dict:
    key = _trans_key(p)
    done = _trans_finished_marker(p)
    total = _trans_total_segments(p)
    seg_count = _trans_count_ts(p)
    cache_pct = int((seg_count / total) * 100) if total > 0 else 0

    job = _TRANS_JOBS.get(key)
    if job is None:
        if done.is_file() or (total > 0 and seg_count >= total):
            try:
                done.touch()
            except OSError:
                pass
            return {"ready": True, "playlist": f"/hls/{key}/index.m3u8", "progress": 100,
                    "cache_pct": 100, "viewers": _trans_viewers_count(key)}
        # 没有任务：自动启动（受并发上限保护）
        info = _trans_start(p, 0)
        info.setdefault("cache_pct", cache_pct)
        info["viewers"] = _trans_viewers_count(key)
        return info

    proc = job.get("proc")
    if proc is None:
        return _trans_start(p, int(job.get("start_seg", 0)))
    ret = proc.poll()
    duration = float(job.get("duration") or 0.0)
    start_seg = int(job.get("start_seg", 0))
    progress_path = job.get("progress_path")
    current_sec = 0.0
    if progress_path and Path(progress_path).is_file():
        try:
            with open(progress_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-30:]
            for line in lines:
                if line.startswith("out_time_ms="):
                    try:
                        current_sec = float(line.split("=", 1)[1].strip()) / 1_000_000.0
                    except ValueError:
                        pass
        except OSError:
            pass
    abs_sec = current_sec + start_seg * _HLS_SEG_DURATION
    pct = 0
    if duration > 0:
        pct = int(min(99, max(0, abs_sec / duration * 100)))
    job["progress"] = pct
    job["progress_seg"] = int(abs_sec / _HLS_SEG_DURATION)

    common = {"cache_pct": cache_pct, "viewers": _trans_viewers_count(key)}
    if ret is None:
        return {"running": True, "playlist": f"/hls/{key}/index.m3u8",
                "progress": pct, "start_seg": start_seg, "current_seg": job["progress_seg"],
                **common}

    if ret == 0:
        with _TRANS_LOCK:
            _TRANS_JOBS.pop(key, None)
        if total > 0 and _trans_count_ts(p) >= total:
            try:
                done.touch()
            except OSError:
                pass
            return {"ready": True, "playlist": f"/hls/{key}/index.m3u8", "progress": 100,
                    "cache_pct": 100, "viewers": _trans_viewers_count(key)}
        return {"running": True, "playlist": f"/hls/{key}/index.m3u8",
                "progress": pct, "start_seg": start_seg, **common}

    err = ""
    try:
        if proc.stderr:
            err = proc.stderr.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    with _TRANS_LOCK:
        _TRANS_JOBS.pop(key, None)
    return {"error": f"ffmpeg 退出码 {ret}: {err.strip()[:300]}", **common}


_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "text", "webvtt", "vtt"}
_IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvb_subtitle", "xsub"}


@lru_cache(maxsize=512)
def _probe_subtitles_cached(path_str: str, mtime_ns: int) -> tuple[tuple, ...]:
    """ffprobe 列出 mkv 内所有字幕轨道（按 path+mtime 缓存为元组结果）。"""
    _ = mtime_ns  # 仅用于缓存失效
    exe = _which("ffprobe")
    if not exe:
        return ()
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_streams",
             "-select_streams", "s", path_str],
            capture_output=True, timeout=15, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if r.returncode != 0:
        return ()
    try:
        data = json.loads(r.stdout or "{}")
    except (ValueError, TypeError):
        return ()
    out: list[tuple] = []
    for i, s in enumerate(data.get("streams", []) or []):
        codec = (s.get("codec_name") or "").lower()
        if not codec:
            continue
        kind = "text" if codec in _TEXT_SUB_CODECS else (
            "image" if codec in _IMAGE_SUB_CODECS else "unknown")
        tags = s.get("tags") or {}
        out.append((
            i, codec, kind,
            (tags.get("language") or "und").lower(),
            tags.get("title") or "",
        ))
    return tuple(out)


def _probe_subtitles(p: Path) -> list[dict]:
    """对外 API：返回 list[dict]，命中 lru 缓存时无开销。"""
    try:
        st = p.stat()
        mtime_ns = st.st_mtime_ns
    except OSError:
        mtime_ns = 0
    raw = _probe_subtitles_cached(str(p), mtime_ns)
    return [{"idx": r[0], "codec": r[1], "kind": r[2], "lang": r[3], "title": r[4]} for r in raw]


_SUB_EXTRACT_INFLIGHT: set[str] = set()
_SUB_EXTRACT_LOCK = threading.Lock()


def _prefetch_internal_subs_async(p: Path) -> None:
    """视频页打开时后台预抽所有内封文本字幕到磁盘，select 切换时秒载。"""
    try:
        key = _trans_key(p)
    except OSError:
        return
    with _SUB_EXTRACT_LOCK:
        if key in _SUB_EXTRACT_INFLIGHT:
            return
        _SUB_EXTRACT_INFLIGHT.add(key)

    def _worker():
        try:
            exe = _which("ffmpeg")
            if not exe:
                return
            for sub in _probe_subtitles(p):
                if sub.get("kind") != "text":
                    continue
                idx = int(sub.get("idx", 0))
                cache_file = _subtitle_cache_path(p, idx)
                if cache_file.is_file() and cache_file.stat().st_size > 10:
                    continue
                vtt: bytes | None = None
                for fmt in ("webvtt", "srt", "ass"):
                    out, _err = _ffmpeg_extract_sub(p, idx, fmt, exe)
                    if not out or len(out) < 10:
                        continue
                    try:
                        text = _decode_subtitle_bytes(out)
                    except Exception:  # noqa: BLE001
                        continue
                    if fmt == "webvtt" and text.lstrip().startswith("WEBVTT"):
                        vtt = text.encode("utf-8"); break
                    if fmt == "srt":
                        vtt = _srt_to_vtt(text).encode("utf-8"); break
                    if fmt == "ass":
                        vtt = _ass_to_vtt(text).encode("utf-8"); break
                if vtt:
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(vtt)
                    except OSError:
                        pass
        finally:
            with _SUB_EXTRACT_LOCK:
                _SUB_EXTRACT_INFLIGHT.discard(key)

    threading.Thread(target=_worker, name=f"mitm-sub-prefetch-{key[:8]}", daemon=True).start()


def _ffmpeg_extract_sub(p: Path, idx: int, fmt: str, exe: str) -> tuple[bytes, str]:
    """以指定容器格式 (webvtt/srt/ass) 抽取内封字幕。返回 (stdout, stderr)。"""
    try:
        r = subprocess.run(
            [exe, "-y", "-i", str(p),
             "-map", f"0:s:{idx}",
             "-c:s", fmt,
             "-f", fmt, "-loglevel", "error", "-"],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return b"", str(e)
    if r.returncode != 0:
        return b"", (r.stderr or b"").decode("utf-8", errors="replace")
    return (r.stdout or b""), ""


def _subtitle_cache_path(p: Path, idx: int) -> Path:
    base = _trans_dir(p)
    return base / f"sub_{idx}.vtt"


def _subtitle_internal_response(flow) -> Response:
    """从 mkv 抽取内封文本字幕并转为 WebVTT 流式返回。多重兜底 + 磁盘缓存。
    
    支持的查询参数：
      idx       sub-stream 真实索引（与 _probe_subtitles 的 idx 对齐）
      debug=1   返回每一步 ffmpeg 的诊断 JSON（不写缓存）
      force=1   忽略已有的缓存重新抽取一次
    """
    p = _resolve_path_from_query(flow)
    if p is None:
        return _error_page("文件不存在。")
    try:
        idx = int(_query_first(flow, "idx") or "0")
    except ValueError:
        idx = 0
    debug = (_query_first(flow, "debug") or "").strip() in ("1", "true", "yes")
    force = (_query_first(flow, "force") or "").strip() in ("1", "true", "yes")

    cache_file = _subtitle_cache_path(p, idx)
    if not debug and not force and cache_file.is_file():
        try:
            data = cache_file.read_bytes()
            if data:
                return Response.make(200, data, {
                    "Content-Type": "text/vtt; charset=utf-8",
                    "Content-Length": str(len(data)),
                    "Cache-Control": "no-store",
                    "Access-Control-Allow-Origin": "*",
                })
        except OSError:
            pass

    exe = _which("ffmpeg")
    if not exe:
        return _error_page("未安装 ffmpeg。", status=500)

    # 兜底链：webvtt 直出 → srt 抽出再转 → ass 抽出再转 → mov_text 抽出再转
    diag: list[dict] = []
    vtt_bytes: bytes | None = None

    def _try(fmt: str, convert):
        nonlocal vtt_bytes
        out, err = _ffmpeg_extract_sub(p, idx, fmt, exe)
        rec = {"fmt": fmt, "stdout_bytes": len(out), "stderr": (err or "").strip()[:300]}
        diag.append(rec)
        if not out or len(out) < 10:
            return
        try:
            text = _decode_subtitle_bytes(out)
        except Exception as e:  # noqa: BLE001
            rec["decode_err"] = repr(e)
            return
        try:
            converted = convert(text)
        except Exception as e:  # noqa: BLE001
            rec["convert_err"] = repr(e)
            return
        if not converted or len(converted.strip()) < len("WEBVTT"):
            rec["convert_err"] = "empty after convert"
            return
        vtt_bytes = converted.encode("utf-8")
        rec["ok"] = True

    _try("webvtt", lambda t: t if t.lstrip().startswith("WEBVTT") else "WEBVTT\n\n" + t)
    if not vtt_bytes:
        _try("srt", _srt_to_vtt)
    if not vtt_bytes:
        _try("ass", _ass_to_vtt)
    # 极端兜底：用 codec copy 拷出原始流然后 Python 自己识别
    if not vtt_bytes:
        try:
            r = subprocess.run(
                [exe, "-y", "-i", str(p), "-map", f"0:s:{idx}",
                 "-c:s", "copy", "-f", "matroska", "-loglevel", "error", "-"],
                capture_output=True, timeout=180,
            )
            raw = r.stdout or b""
            stderr_txt = (r.stderr or b"").decode("utf-8", errors="replace").strip()[:300]
            diag.append({"fmt": "copy_mkv", "stdout_bytes": len(raw), "stderr": stderr_txt})
        except (OSError, subprocess.TimeoutExpired) as e:
            diag.append({"fmt": "copy_mkv", "error": repr(e)})

    if debug:
        body = json.dumps({
            "src": str(p),
            "idx": idx,
            "subtitle_streams": _probe_subtitles(p),
            "fallbacks": diag,
            "ok": vtt_bytes is not None,
            "preview": (vtt_bytes[:400].decode("utf-8", errors="replace") if vtt_bytes else ""),
        }, ensure_ascii=False, indent=2).encode("utf-8")
        return Response.make(200, body, {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
        })

    if not vtt_bytes:
        last_err = ""
        for r in diag:
            if r.get("stderr"):
                last_err = r["stderr"]
        msg = last_err or "字幕抽取无输出"
        return _error_page(f"内封字幕抽取失败：{msg}", status=500)

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(vtt_bytes)
    except OSError:
        pass

    return Response.make(200, vtt_bytes, {
        "Content-Type": "text/vtt; charset=utf-8",
        "Content-Length": str(len(vtt_bytes)),
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
    })


def _trans_debug_response(flow) -> Response:
    p = _resolve_path_from_query(flow)
    if p is None:
        return Response.make(404, b'{"error":"not found"}',
                             {"Content-Type": "application/json"})
    key = _trans_key(p)
    out_dir = _trans_dir(p)
    info = {
        "key": key,
        "src": str(p),
        "duration": _ffprobe_duration_cached(p),
        "total_segs": _trans_total_segments(p),
        "out_dir": str(out_dir),
        "ts_count": _trans_count_ts(p),
        "done": _trans_finished_marker(p).is_file(),
    }
    job = _TRANS_JOBS.get(key)
    if job:
        proc = job.get("proc")
        info["job"] = {
            "pid": proc.pid if proc else None,
            "running": (proc is not None and proc.poll() is None),
            "start_seg": job.get("start_seg"),
            "progress": job.get("progress"),
            "progress_seg": job.get("progress_seg"),
        }
    else:
        info["job"] = None
    body = json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8")
    return Response.make(200, body, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
    })


def _video_trans_jump_response(flow) -> Response:
    p = _resolve_path_from_query(flow)
    if p is None:
        return Response.make(404, b'{"error":"not found"}',
                             {"Content-Type": "application/json"})
    seg_raw = _query_first(flow, "seg")
    try:
        target_seg = max(0, int(seg_raw))
    except (TypeError, ValueError):
        target_seg = 0
    out_dir = _trans_dir(p)
    target_file = out_dir / f"seg_{target_seg:05d}.ts"
    if target_file.is_file():
        info = {"ready": True, "seg": target_seg}
    else:
        info = _trans_start(p, target_seg)
        info["seg"] = target_seg
    body = json.dumps(info, ensure_ascii=False).encode("utf-8")
    return Response.make(200, body, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
    })


def _video_trans_status_response(flow) -> Response:
    p = _resolve_path_from_query(flow)
    if p is None:
        return Response.make(404, json.dumps({"error": "文件不存在"}).encode("utf-8"),
                             {"Content-Type": "application/json", "Cache-Control": "no-store"})
    info = _trans_status(p)
    body = json.dumps(info, ensure_ascii=False).encode("utf-8")
    return Response.make(200, body,
                         {"Content-Type": "application/json; charset=utf-8",
                          "Cache-Control": "no-store"})


def _video_trans_clear_response(flow) -> Response:
    """用户离开视频页 → 引用计数 -1。缓存保留供下次使用。
    
    若管理员显式带 ?purge=1，则真正删除缓存目录与转码进程。
    """
    p = _resolve_path_from_query(flow)
    if p is None:
        return Response.make(404, b'{"error":"not found"}',
                             {"Content-Type": "application/json"})
    key = _trans_key(p)
    sid = (_query_first(flow, "sid") or "").strip()

    purge = (_query_first(flow, "purge") or "").strip() in ("1", "true", "yes")
    if purge:
        ctx = user_auth.get_user_ctx_from_flow(flow)
        if not ctx or not getattr(ctx, "is_admin", False):
            return Response.make(403, b'{"error":"admin required"}',
                                 {"Content-Type": "application/json"})
        _trans_kill_job(key)
        with _TRANS_VIEWERS_LOCK:
            _TRANS_VIEWERS.pop(key, None)
        import shutil as _sh
        try:
            _sh.rmtree(_trans_dir(p), ignore_errors=True)
        except OSError:
            pass
        return Response.make(200, b'{"cleared":true}',
                             {"Content-Type": "application/json; charset=utf-8",
                              "Cache-Control": "no-store",
                              "Access-Control-Allow-Origin": "*"})

    # 普通用户：仅释放本会话的观众身份
    if sid:
        _trans_viewer_close(key, sid)
    return Response.make(200, b'{"left":true}',
                         {"Content-Type": "application/json; charset=utf-8",
                          "Cache-Control": "no-store",
                          "Access-Control-Allow-Origin": "*"})


def _video_trans_session_response(flow) -> Response:
    """会话心跳：action=open|beat|close & sid=xxx。"""
    p = _resolve_path_from_query(flow)
    if p is None:
        return Response.make(404, b'{"error":"not found"}',
                             {"Content-Type": "application/json"})
    key = _trans_key(p)
    sid = (_query_first(flow, "sid") or "").strip()
    action = (_query_first(flow, "action") or "beat").strip()
    if not sid:
        return Response.make(400, b'{"error":"sid"}',
                             {"Content-Type": "application/json"})
    if action == "close":
        _trans_viewer_close(key, sid)
    else:
        _trans_viewer_open(key, sid)
        _start_reaper_once()
    body = json.dumps({"viewers": _trans_viewers_count(key), "ok": True}).encode("utf-8")
    return Response.make(200, body, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
    })


def _hls_response(url_path: str) -> Response:
    """
    /hls/{key}/{filename}
    - index.m3u8: 动态生成虚拟全长 m3u8（含 EXT-X-ENDLIST，hls.js 可任意 seek）
    - seg_*.ts:   文件存在 → 返回；不存在 → 触发对应位置转码并等待生成
    """
    rel = url_path[len("/hls/"):].strip("/").replace("\\", "/")
    parts = rel.split("/")
    if len(parts) != 2:
        return _error_page("非法 HLS 路径。", status=400)
    key, fname = parts
    if not re.match(r"^[0-9a-f]{40}$", key):
        return _error_page("非法 key。", status=400)
    out_dir = _trans_cache_dir() / key
    out_dir.mkdir(parents=True, exist_ok=True)

    if fname == "index.m3u8":
        # 通过 key 反查源文件路径：从一个被命名为 source.ref 的文件
        src_ref = out_dir / "source.ref"
        if not src_ref.is_file():
            return Response.make(404, b"", {"Cache-Control": "no-store"})
        try:
            src_path_str = src_ref.read_text(encoding="utf-8").strip()
        except OSError:
            return Response.make(500, b"", {"Cache-Control": "no-store"})
        src_path = Path(src_path_str)
        if not src_path.is_file():
            return Response.make(404, b"", {"Cache-Control": "no-store"})
        m3u8 = _trans_virtual_m3u8(src_path)
        if not m3u8:
            return Response.make(503, b"", {"Cache-Control": "no-store"})
        data = m3u8.encode("utf-8")
        # 当整片已 DONE，m3u8 内容稳定 → 浏览器可强缓存；否则保持 no-store
        is_done = (out_dir / "DONE").is_file()
        cache_hdr = "public, max-age=3600, immutable" if is_done else "no-store"
        return Response.make(200, data, {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Content-Length": str(len(data)),
            "Cache-Control": cache_hdr,
            "Access-Control-Allow-Origin": "*",
        })

    seg_match = re.match(r"^seg_(\d{5})\.ts$", fname)
    if not seg_match:
        return _error_page("非法文件名。", status=400)
    target = out_dir / fname
    seg_idx = int(seg_match.group(1))

    if not target.is_file():
        # 触发从该段开始的转码
        src_ref = out_dir / "source.ref"
        if src_ref.is_file():
            try:
                src_path = Path(src_ref.read_text(encoding="utf-8").strip())
                if src_path.is_file():
                    _trans_start(src_path, seg_idx)
            except OSError:
                pass
        # 最多等 30 秒
        for _ in range(300):
            if target.is_file():
                break
            time.sleep(0.1)
        if not target.is_file():
            return Response.make(503, b"", {"Cache-Control": "no-store",
                                            "Retry-After": "1"})
    try:
        st = target.stat()
        data = target.read_bytes()
    except OSError:
        return Response.make(500, b"", {"Cache-Control": "no-store"})
    # 段一旦写完就不变，加强缓存：浏览器/上游 CDN 复用，多用户极致省 IO
    etag = f'W/"{st.st_size:x}-{int(st.st_mtime):x}"'
    return Response.make(200, data, {
        "Content-Type": "video/mp2t",
        "Content-Length": str(len(data)),
        "Cache-Control": "public, max-age=86400, immutable",
        "ETag": etag,
        "Access-Control-Allow-Origin": "*",
    })


@lru_cache(maxsize=1024)
def _video_probe_compat_cached(path_str: str, suffix: str, mtime_ns: int) -> tuple[bool, str]:
    """ffprobe 检测浏览器是否能直接播；按 path+mtime 缓存。"""
    _ = mtime_ns
    exe = _which("ffprobe")
    if not exe:
        return True, ""
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_streams", path_str],
            capture_output=True, timeout=15, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return True, ""
    if r.returncode != 0:
        return True, ""
    try:
        data = json.loads(r.stdout or "{}")
    except (ValueError, TypeError):
        return True, ""
    vcodec = ""
    pix_fmt = ""
    acodec = ""
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and not vcodec:
            vcodec = (s.get("codec_name") or "").lower()
            pix_fmt = (s.get("pix_fmt") or "").lower()
        elif s.get("codec_type") == "audio" and not acodec:
            acodec = (s.get("codec_name") or "").lower()
    incompat = []
    if vcodec in {"hevc", "h265"}:
        incompat.append(f"视频 {vcodec}")
    if "10le" in pix_fmt or "10be" in pix_fmt or pix_fmt.endswith("p10"):
        incompat.append(f"色深 10bit ({pix_fmt})")
    if vcodec in {"vp9", "av1"} and suffix != ".webm":
        incompat.append(f"视频 {vcodec} (容器不匹配)")
    if acodec in {"dts", "eac3", "truehd"}:
        incompat.append(f"音频 {acodec}")
    if not incompat:
        return True, f"{vcodec} / {pix_fmt} / {acodec}"
    return False, " + ".join(incompat)


def _video_probe_compat(path: Path) -> tuple[bool, str]:
    """对外 API：自动按 mtime 失效缓存。"""
    try:
        st = path.stat()
        mtime_ns = st.st_mtime_ns
    except OSError:
        mtime_ns = 0
    return _video_probe_compat_cached(str(path), path.suffix.lower(), mtime_ns)


def _video_response(flow, path: Path) -> Response:
    rel = _rel_of(path)
    rel_q = _q(path)
    src = f"/file?path={rel_q}"
    # 后台预抽所有内封文本字幕到磁盘（不阻塞页面渲染）：用户切换字幕时秒载
    _prefetch_internal_subs_async(path)
    # 外挂字幕
    ext_subs = _find_subtitles(path)
    track_tags: list[str] = []
    track_count = 0
    sub_listing: list[dict] = []  # 给前端字幕选单用：[{label, lang, type, src}]
    for s in ext_subs:
        lang, label = _subtitle_lang_label(s)
        default = " default" if track_count == 0 else ""
        track_id = f"sub_{track_count}"
        track_tags.append(
            f'<track id="{track_id}" kind="subtitles" label="{html.escape(label)}" '
            f'srclang="{html.escape(lang)}" src="/subtitle?path={_q(s)}"{default}>'
        )
        sub_listing.append({
            "id": track_id, "kind": "external", "label": label, "lang": lang,
            "src": f"/subtitle?path={_q(s)}",
        })
        track_count += 1
    # 内封文本字幕（subrip / ass / mov_text 等）→ 转成 VTT 提供
    # 关键：必须用 _probe_subtitles 给出的 sub-stream 真实索引（含图像字幕在内的全局位置），
    # 因为 ffmpeg `0:s:N` 是「全部字幕流（图+文）」里的第 N 个；前端不能只用「文本字幕的第 N 个」。
    int_subs = _probe_subtitles(path)
    text_seq = 0
    image_subs: list[dict] = []
    for sub in int_subs:
        kind = sub.get("kind") or "unknown"
        codec = sub.get("codec") or ""
        lang = sub.get("lang") or "und"
        title = sub.get("title") or ""
        real_idx = int(sub.get("idx", 0))
        if kind == "text":
            label_text = title or lang or f"内封 {text_seq + 1}"
            default = " default" if track_count == 0 else ""
            track_id = f"sub_{track_count}"
            track_tags.append(
                f'<track id="{track_id}" kind="subtitles" label="{html.escape(label_text)} (内封)" '
                f'srclang="{html.escape(lang)}" '
                f'src="/subtitle_internal?path={rel_q}&amp;idx={real_idx}"{default}>'
            )
            sub_listing.append({
                "id": track_id, "kind": "internal-text", "label": label_text,
                "lang": lang, "codec": codec, "stream_idx": real_idx,
                "src": f"/subtitle_internal?path={rel_q}&idx={real_idx}",
            })
            track_count += 1
            text_seq += 1
        elif kind == "image":
            image_subs.append({
                "stream_idx": real_idx, "lang": lang, "codec": codec, "title": title,
            })
        else:
            sub_listing.append({
                "id": "", "kind": "internal-unknown", "label": title or codec or "未知",
                "lang": lang, "codec": codec, "stream_idx": real_idx, "src": "",
            })
    # 图像字幕信息也告诉前端
    for s in image_subs:
        sub_listing.append({
            "id": "", "kind": "internal-image", "label": s["title"] or s["codec"] or "图像字幕",
            "lang": s["lang"], "codec": s["codec"], "stream_idx": s["stream_idx"], "src": "",
        })
    tracks = "".join(track_tags)

    mime = _guess_mime(path)
    ext = path.suffix.lower()
    can_html5_container = ext in {".mp4", ".webm", ".m4v", ".mov", ".ogv"}
    compat_ok, compat_detail = _video_probe_compat(path)
    needs_transcode = (not can_html5_container) or (not compat_ok)
    trans_key = _trans_key(path)
    hls_url = f"/hls/{trans_key}/index.m3u8"
    if needs_transcode:
        # 确保 cache 目录与 source.ref 已建好
        _trans_dir(path)

    # 顶栏副标信息
    if needs_transcode:
        sub_msg = "实时转码 + HLS 边转边播"
    elif compat_detail:
        sub_msg = f"编码：{compat_detail}"
    else:
        sub_msg = ""

    css = r"""
.app:has(.mitm-video-page){min-height:100dvh;max-height:100dvh;overflow:hidden;box-sizing:border-box}
.content.mitm-video-page{display:flex;flex-direction:column;min-height:0;flex:1 1 auto;padding-top:6px;padding-bottom:8px}
.mitm-video-page{flex:1 1 auto;min-height:0;max-height:calc(100dvh - 50px);overflow-y:auto;overflow-x:hidden;
  -webkit-overflow-scrolling:touch;overscroll-behavior:contain;box-sizing:border-box}
@supports not selector(:has(*)){.mitm-video-page{max-height:calc(100dvh - 50px);overflow-y:auto;overflow-x:hidden}}
.video-stage{display:flex;flex-direction:column;align-items:center;padding:12px 12px 18px;box-sizing:border-box}
.video-wrap{width:min(100%,1200px);display:flex;flex-direction:column;align-items:center;flex:0 1 auto;min-height:0}
#vp-stage{position:relative;touch-action:pan-y;width:100%;min-height:80px;overflow:visible;z-index:1}
#vp-pan{display:block;transform-origin:center center;will-change:transform}
.video-wrap video{width:100%;height:auto;max-height:min(62dvh,70vh);background:#000;border-radius:14px;box-shadow:0 10px 36px rgba(0,0,0,.45)}
.video-wrap.size-sm video{max-width:55vw;max-height:min(42dvh,50vh)}
.video-wrap.size-md video{max-width:78vw;max-height:min(55dvh,62vh)}
.video-wrap.size-lg video{max-width:94vw;max-height:min(62dvh,70vh)}
.video-wrap.align-left{align-items:flex-start}
.video-wrap.align-right{align-items:flex-end}
#burst-hint{max-height:0;opacity:0;overflow:hidden;transition:max-height .18s,opacity .18s,margin .18s;pointer-events:none;
  text-align:center;font-size:.7rem;font-weight:600;color:#6b8ab5;line-height:1.35;
  width:100%;max-width:100%;box-sizing:border-box;padding:0 4px;flex-shrink:0;margin:0}
#burst-hint.on{max-height:2.2em;opacity:1;margin:4px 0 0}
.video-hint{margin-top:4px;font-size:.78rem;color:var(--muted);text-align:center;line-height:1.5}
.video-controls{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;justify-content:center;position:relative;z-index:5}
.video-controls select{min-height:36px;padding:4px 10px;border-radius:8px;background:rgba(0,0,0,.25);border:1px solid var(--line);color:var(--fg)}
.video-controls .sep{width:1px;height:22px;background:var(--line);margin:0 4px}
.sub-panel{margin-top:10px;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:rgba(255,255,255,.04);font-size:.78rem;line-height:1.5;width:100%;max-width:880px;box-sizing:border-box}
.sub-panel .head{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.sub-panel .h{font-weight:700;color:var(--fg)}
.sub-panel ul{list-style:none;padding:0;margin:8px 0 0}
.sub-panel li{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:4px 0;border-top:1px solid rgba(255,255,255,.05)}
.sub-panel .tag{display:inline-block;padding:1px 6px;border-radius:6px;font-size:.7rem;background:rgba(124,193,255,.18);color:#9dd1ff}
.sub-panel .tag.image{background:rgba(255,170,80,.18);color:#ffce82}
.sub-panel .tag.unk{background:rgba(255,255,255,.1);color:#ccc}
.sub-panel a{color:#9dd1ff}
.sub-panel button{min-height:30px;padding:0 12px;border-radius:8px;border:1px solid var(--line);background:rgba(255,255,255,.07);color:var(--fg);font-size:.78rem}
.sub-panel button:active{transform:scale(.97)}
.sub-panel select{min-height:32px;padding:2px 10px;border-radius:8px;background:rgba(0,0,0,.25);border:1px solid var(--line);color:var(--fg);font-size:.82rem;flex:1;min-width:160px;max-width:420px}
.sub-debug{margin-top:8px;padding:8px;background:#0a0d13;color:#bcd;font:.72rem ui-monospace,Menlo,Consolas,monospace;border-radius:6px;white-space:pre-wrap;max-height:240px;overflow:auto;display:none}
.sub-debug.show{display:block}
.sub-overlay{position:absolute;left:0;right:0;bottom:9%;text-align:center;color:#fff;font-size:clamp(1rem,2.6vw,1.55rem);font-weight:600;line-height:1.45;padding:0 5%;pointer-events:none;z-index:4;white-space:pre-line;
  text-shadow:0 0 2px #000,0 1px 0 #000,1px 0 0 #000,-1px 0 0 #000,0 -1px 0 #000,0 0 4px rgba(0,0,0,.85)}
.sub-overlay span{display:inline-block;background:rgba(0,0,0,.45);padding:1px 8px;border-radius:4px}
"""

    sub_msg_html = f'<span class="muted" style="font-size:.72rem">{html.escape(sub_msg)}</span>' if sub_msg else ""
    hls_script = '<script src="/assets/hlsjs/hls.min.js"></script>' if needs_transcode else ""
    body = f"""
{hls_script}
<style>{css}
#trans-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.78);z-index:30;display:none;align-items:center;justify-content:center}}
#trans-overlay.show{{display:flex}}
.trans-card{{background:#1f2937;color:#e5e7eb;border-radius:14px;padding:18px 22px;max-width:480px;width:90%;text-align:center;box-shadow:0 14px 40px rgba(0,0,0,.55)}}
.trans-bar{{height:8px;background:rgba(255,255,255,.12);border-radius:6px;overflow:hidden;margin-top:10px}}
.trans-bar > i{{display:block;height:100%;width:0;background:linear-gradient(90deg,#5ad,#7c5);transition:width .4s}}
.trans-msg{{margin-top:10px;font-size:.85rem;color:#bcd}}
.trans-err{{color:#fca}}
</style>
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="{_back_href(path)}">← 返回</a>
  <span class="brand" title="{html.escape(path.name)}">{html.escape(path.name)}</span>
  <span class="spacer"></span>
  {sub_msg_html}
</div>
<div class="content video-stage mitm-video-page">
  <div id="video-wrap" class="video-wrap size-lg align-center">
    <div id="vp-stage">
      <div id="vp-pan">
    <video id="mitm-video" controls playsinline preload="metadata" crossorigin="anonymous">
      {tracks}
    </video>
      </div>
    </div>
    <div id="trans-overlay">
      <div class="trans-card">
        <div id="trans-title" style="font-size:1rem;font-weight:600">视频不兼容浏览器，正在转码…</div>
        <div class="trans-bar"><i id="trans-fill"></i></div>
        <div class="trans-msg" id="trans-msg">准备中…（首次较慢，转完会缓存，下次秒开）</div>
      </div>
    </div>
    <div id="burst-hint" aria-live="polite" role="status"></div>
    <p class="video-hint"><strong>未放大时</strong>单指在画面上<strong>上下滑</strong>可滚到尺寸/倍速等设置；<strong>放大后</strong>单指可拖动画面。<strong>双指</strong>捏合缩放。长按约 0.45 秒为临时倍速，见下方两栏。</p>
    <div class="video-controls">
      <span class="muted">尺寸</span>
      <button type="button" class="btn btn-ghost btn-sm" data-size="sm">小</button>
      <button type="button" class="btn btn-ghost btn-sm" data-size="md">中</button>
      <button type="button" class="btn btn-primary btn-sm" data-size="lg">大</button>
      <span class="sep"></span>
      <span class="muted">位置</span>
      <button type="button" class="btn btn-ghost btn-sm" data-align="left">左</button>
      <button type="button" class="btn btn-primary btn-sm" data-align="center">中</button>
      <button type="button" class="btn btn-ghost btn-sm" data-align="right">右</button>
      <span class="sep"></span>
      <span class="muted">常速</span>
      <select id="rate" title="与播放器条一致，含 2～4 倍直选">
        <option>0.5</option><option>0.75</option><option selected>1.0</option>
        <option>1.25</option><option>1.5</option><option>1.75</option>
        <option>2.0</option><option>2.5</option><option>3.0</option>
        <option>3.5</option><option>4.0</option>
      </select>
      <span class="sep"></span>
      <span class="muted" title="仅长按加速时使用">长按</span>
      <select id="burst-rate" title="长按时目标倍速（2～4 倍）">
        <option value="2.0" selected>2.0</option>
        <option value="2.5">2.5</option>
        <option value="3.0">3.0</option>
        <option value="3.5">3.5</option>
        <option value="4.0">4.0</option>
      </select>
    </div>
    <div class="sub-panel" id="sub-panel">
      <div class="head">
        <span class="h">字幕</span>
        <select id="sub-select" title="选择当前字幕版本，或关闭字幕"></select>
        <span class="muted" style="margin-left:6px">字号</span>
        <select id="sub-size" title="调整字幕字号">
          <option value="0.85">小</option>
          <option value="1" selected>中</option>
          <option value="1.2">大</option>
          <option value="1.5">特大</option>
        </select>
      </div>
      <div class="muted" id="sub-summary" style="margin-top:6px">检测中…</div>
      <ul id="sub-list"></ul>
    </div>
  </div>
</div>
<script>
(function(){{
  function dlen(ax,ay,bx,by){{ var dx=ax-bx,dy=ay-by; return Math.sqrt(dx*dx+dy*dy); }}
  function clampB(x){{ return Math.max(2, Math.min(4, x)); }}
  var wrap=document.getElementById('video-wrap'), v=document.getElementById('mitm-video');
  var stage=document.getElementById('vp-stage'), pan=document.getElementById('vp-pan');
  var hintEl=document.getElementById('burst-hint');
  var sc=1, tx=0, ty=0, pinch0=null, longT=null, longActive=false, mouseT=null, mouseArmed=false;
  var lpX=0, lpY=0, onePan0=null;
  function applyP(){{ if(pan) pan.style.transform='translate('+tx+'px,'+ty+'px) scale('+sc+')'; }}
  function syncStageTA(){{ if(!stage) return; if(sc>1.01) stage.style.touchAction='none'; else stage.style.touchAction='pan-y'; }}
  function rateFromUI(){{ var r=document.getElementById('rate'); return parseFloat(r && r.value)||1.0; }}
  function burstFromUI(){{ var r=document.getElementById('burst-rate');
    var t=parseFloat(r && r.value);
    if (!isFinite(t)) return 2.0; return clampB(t);
  }}
  function showBurstHint(br){{ if(!hintEl) return; hintEl.textContent='长按倍速 '+(Math.round(br*10)/10)+'× 中…'; hintEl.classList.add('on'); }}
  function hideBurstHint(){{ if(hintEl) hintEl.classList.remove('on'); }}
  function endLong(){{
    clearTimeout(longT); longT=null; clearTimeout(mouseT); mouseT=null; mouseArmed=false;
    hideBurstHint();
    if (longActive && v) {{ v.playbackRate=rateFromUI(); longActive=false; }}
  }}
  function fireLongBurst(){{
    if (!v) return;
    var br = burstFromUI();
    v.playbackRate = br;
    longActive = true;
    showBurstHint(br);
  }}
  if (stage && v) {{
    stage.style.touchAction = 'pan-y';
    stage.addEventListener('touchstart', function(e){{
      onePan0 = null;
      if (e.touches.length>=2) {{ if(stage) stage.style.touchAction='none'; endLong(); }}
      if (e.touches.length===1) {{
        lpX = e.touches[0].clientX; lpY = e.touches[0].clientY;
        endLong();
        if (sc <= 1.01) longT=setTimeout(fireLongBurst, 450);
        if (sc > 1.01) {{
          onePan0 = {{ sx: e.touches[0].clientX, sy: e.touches[0].clientY, tx0: tx, ty0: ty }};
          if (stage) stage.style.touchAction = 'none';
        }} else if (stage) stage.style.touchAction = 'pan-y';
      }}
      if (e.touches.length===2) {{
        var a=e.touches[0],b=e.touches[1];
        pinch0={{
          d0: Math.max(1e-3, dlen(a.clientX,a.clientY,b.clientX,b.clientY)),
          sc0: sc, tx0: tx, ty0: ty,
          cx: (a.clientX+b.clientX)/2, cy: (a.clientY+b.clientY)/2
        }};
      }}
    }}, {{passive:true}});
    stage.addEventListener('touchmove', function(e){{
      if (e.touches.length===1 && onePan0) {{
        e.preventDefault();
        var t0=e.touches[0];
        tx = onePan0.tx0 + t0.clientX - onePan0.sx;
        ty = onePan0.ty0 + t0.clientY - onePan0.sy;
        applyP();
        return;
      }}
      if (e.touches.length===1 && longT) {{
        var t2=e.touches[0];
        var ddx=t2.clientX-lpX, ddy=t2.clientY-lpY;
        if (ddx*ddx+ddy*ddy>400) endLong();
      }}
      if (e.touches.length===2 && pinch0) {{
        e.preventDefault();
        var a=e.touches[0],b=e.touches[1];
        var d=dlen(a.clientX,a.clientY,b.clientX,b.clientY);
        sc=Math.min(3, Math.max(0.35, pinch0.sc0 * d / pinch0.d0));
        var cx=(a.clientX+b.clientX)/2, cy=(a.clientY+b.clientY)/2;
        tx=pinch0.tx0 + (cx - pinch0.cx) * 1.15;
        ty=pinch0.ty0 + (cy - pinch0.cy) * 1.15;
        applyP();
        syncStageTA();
      }}
    }}, {{passive:false}});
    var endPinch=function(e){{
      if (e.touches.length<2) pinch0=null;
    }};
    stage.addEventListener('touchend', function(e){{
      endLong(); endPinch(e);
      if (e.touches.length===0) onePan0=null;
      if (e.touches.length<2) pinch0=null;
      if (e.touches.length===0) syncStageTA();
    }});
    stage.addEventListener('touchcancel', function(e){{ onePan0=null; endLong(); pinch0=null; syncStageTA(); }});
    // 鼠标长按
    var winUp = function(){{ if (mouseT) {{ clearTimeout(mouseT); mouseT=null; }} endLong(); window.removeEventListener('mouseup', winUp, true); }};
    stage.addEventListener('mousedown', function(e){{
      if (e.button!==0) return;
      if (e.target && e.target.closest && e.target.closest('input,select,button,a')) return;
      endLong();
      mouseArmed=true;
      mouseT=setTimeout(function(){{ if (mouseArmed) fireLongBurst(); }}, 450);
      window.addEventListener('mouseup', winUp, true);
    }});
    stage.addEventListener('mouseleave', function(){{ if (mouseT) {{ endLong(); }} }});
  }}
  wrap.querySelectorAll('[data-size]').forEach(function(b){{
    b.addEventListener('click', function(){{
      wrap.classList.remove('size-sm','size-md','size-lg');
      wrap.classList.add('size-'+b.getAttribute('data-size'));
      wrap.querySelectorAll('[data-size]').forEach(function(x){{x.classList.remove('btn-primary');x.classList.add('btn-ghost');}});
      b.classList.remove('btn-ghost'); b.classList.add('btn-primary');
    }});
  }});
  wrap.querySelectorAll('[data-align]').forEach(function(b){{
    b.addEventListener('click', function(){{
      wrap.classList.remove('align-left','align-center','align-right');
      wrap.classList.add('align-'+b.getAttribute('data-align'));
      wrap.querySelectorAll('[data-align]').forEach(function(x){{x.classList.remove('btn-primary');x.classList.add('btn-ghost');}});
      b.classList.remove('btn-ghost'); b.classList.add('btn-primary');
    }});
  }});
  var rate=document.getElementById('rate');
  var burstSel=document.getElementById('burst-rate');
  rate.addEventListener('change', function(){{ if (!longActive && v) v.playbackRate=rateFromUI(); }});
  burstSel.addEventListener('change', function(){{
    if (longActive && v) {{ v.playbackRate=burstFromUI(); showBurstHint(burstFromUI()); }}
  }});
  applyP();
  if (v) v.playbackRate = rateFromUI();

  var ORIGINAL_SRC = {json.dumps(src)};
  var ORIGINAL_MIME = {json.dumps(mime)};
  var NEEDS_TRANS = {('true' if needs_transcode else 'false')};
  var TRANS_PATH = {json.dumps(_obfuscate(rel))};
  var HLS_URL = {json.dumps(hls_url)};
  var overlay = document.getElementById('trans-overlay');
  var fillEl = document.getElementById('trans-fill');
  var msgEl = document.getElementById('trans-msg');
  var titleEl = document.getElementById('trans-title');
  var hls = null;

  function setProgress(pct, msg){{
    if (fillEl) fillEl.style.width = (pct||0) + '%';
    if (msgEl) msgEl.textContent = msg || (pct + '%');
  }}
  function showOverlay(){{ if (overlay) overlay.classList.add('show'); }}
  function hideOverlay(){{ if (overlay) overlay.classList.remove('show'); }}

  function loadDirect(url, mime){{
    if (!v) return;
    v.querySelectorAll('source').forEach(function(s){{ s.remove(); }});
    var src = document.createElement('source');
    src.src = url;
    if (mime) src.type = mime;
    v.insertBefore(src, v.firstChild);
    v.load();
  }}

  function loadHls(url){{
    if (!v) return;
    if (window.Hls && Hls.isSupported()){{
      hls = new Hls({{
        lowLatencyMode: false,
        enableWorker: true,
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
        maxBufferSize: 60 * 1024 * 1024,
        backBufferLength: 10,
        liveSyncDuration: 4,
        startFragPrefetch: true,
        progressive: true,
        autoStartLoad: true,
        startLevel: -1,
        nudgeOffset: 0.2,
        nudgeMaxRetry: 5,
        maxBufferHole: 0.5,
        highBufferWatchdogPeriod: 2,
        manifestLoadingMaxRetry: 10,
        manifestLoadingRetryDelay: 600,
        manifestLoadingTimeOut: 20000,
        levelLoadingMaxRetry: 10,
        levelLoadingRetryDelay: 600,
        levelLoadingTimeOut: 20000,
        fragLoadingMaxRetry: 8,
        fragLoadingRetryDelay: 600,
        fragLoadingTimeOut: 30000
      }});
      hls.loadSource(url);
      hls.attachMedia(v);
      hls.on(Hls.Events.MANIFEST_PARSED, function(){{
        v.play && v.play().catch(function(){{}});
      }});
      hls.on(Hls.Events.ERROR, function(_e, data){{
        if (!data || !data.fatal) return;
        // 网络/媒体错误自动重试，避免直接黑屏
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR){{
          try {{ hls.startLoad(); }} catch(_){{}}
        }} else if (data.type === Hls.ErrorTypes.MEDIA_ERROR){{
          try {{ hls.recoverMediaError(); }} catch(_){{}}
        }} else {{
          if (msgEl) {{ msgEl.classList.add('trans-err'); msgEl.textContent = 'HLS 错误：'+data.type+'/'+data.details; }}
        }}
      }});
    }} else if (v.canPlayType && v.canPlayType('application/vnd.apple.mpegurl')) {{
      v.src = url;
      v.play && v.play().catch(function(){{}});
    }} else {{
      if (msgEl) {{ msgEl.classList.add('trans-err'); msgEl.textContent = '当前浏览器不支持 HLS。'; }}
    }}
  }}

  // 会话 ID（页面级），后端按 (path, sid) 计观众，最后一个走才会停 ffmpeg
  var SID = (function(){{
    try{{ return Math.random().toString(36).slice(2) + Date.now().toString(36); }}catch(e){{ return 'sid'; }}
  }})();
  function sessionUrl(action){{
    return '/video_trans_session?path=' + encodeURIComponent(TRANS_PATH) +
           '&sid=' + encodeURIComponent(SID) + '&action=' + encodeURIComponent(action);
  }}
  function sessionBeat(action, useBeacon){{
    var url = sessionUrl(action);
    try{{
      if (useBeacon && navigator.sendBeacon){{ navigator.sendBeacon(url, ''); return; }}
      fetch(url, {{cache:'no-store', keepalive:true}}).catch(function(){{}});
    }}catch(e){{}}
  }}
  function clearTransCache(useBeacon){{
    // 兼容旧函数：现在等价于「会话离开」，缓存保留下次秒开
    var url = '/video_trans_clear?path=' + encodeURIComponent(TRANS_PATH) + '&sid=' + encodeURIComponent(SID);
    try {{
      if (useBeacon && navigator.sendBeacon){{ navigator.sendBeacon(url, ''); return; }}
      fetch(url, {{cache:'no-store', keepalive:true}}).catch(function(){{}});
    }} catch(e) {{}}
  }}

  var SEG_DURATION = 4.0;
  var lastJumpSeg = -1;
  var jumpTimer = null;
  function scheduleJump(){{
    if (!NEEDS_TRANS || !v) return;
    if (jumpTimer) clearTimeout(jumpTimer);
    jumpTimer = setTimeout(function(){{
      try {{
        var t = v.currentTime || 0;
        var seg = Math.floor(t / SEG_DURATION);
        if (seg < 0) seg = 0;
        if (seg === lastJumpSeg) return;
        lastJumpSeg = seg;
        fetch('/video_trans_jump?path=' + encodeURIComponent(TRANS_PATH) + '&seg=' + seg,
              {{cache:'no-store'}}).catch(function(){{}});
      }} catch(e) {{}}
    }}, 200);
  }}

  function pollProgress(){{
    fetch('/video_trans_status?path=' + encodeURIComponent(TRANS_PATH), {{cache:'no-store'}})
      .then(function(r){{ return r.json(); }})
      .then(function(s){{
        if (s.error){{
          if (titleEl) titleEl.textContent = '转码失败';
          if (msgEl) {{ msgEl.classList.add('trans-err'); msgEl.textContent = s.error; }}
          return;
        }}
        if (s.ready){{
          var hint = (s.cache_hit ? '已缓存，直接读取' : '已全部转完');
          setProgress(100, hint);
          setTimeout(hideOverlay, 800);
          return;
        }}
        if (s.queued){{
          setProgress(s.progress || 0, s.msg || '排队中…等待空闲转码槽');
          setTimeout(pollProgress, 2000);
          return;
        }}
        var cache = (s.cache_pct != null) ? s.cache_pct : (s.progress || 0);
        var viewers = (s.viewers != null) ? s.viewers : 1;
        setProgress(s.progress || 0,
          '已转码 ' + (s.progress || 0) + '% · 缓存 ' + cache + '% · 观众 ' + viewers + ' 人（边转边播）');
        setTimeout(pollProgress, 2500);
      }})
      .catch(function(){{ setTimeout(pollProgress, 5000); }});
  }}

  function startTranscodeAndPlay(){{
    showOverlay();
    setProgress(0, '正在启动转码并准备首段…');
    fetch('/video_trans_status?path=' + encodeURIComponent(TRANS_PATH), {{cache:'no-store'}})
      .then(function(r){{ return r.json(); }})
      .then(function(s){{
        if (s.error){{
          if (titleEl) titleEl.textContent = '转码失败';
          if (msgEl) {{ msgEl.classList.add('trans-err'); msgEl.textContent = s.error; }}
          return;
        }}
        // 一旦后端开始转码，就让 hls.js 去抓 m3u8（hls.js 自身会重试）
        loadHls(HLS_URL);
        v.addEventListener('playing', function(){{ hideOverlay(); }}, {{once:true}});
        v.addEventListener('canplay', function(){{ hideOverlay(); }}, {{once:true}});
        pollProgress();
      }})
      .catch(function(err){{
        if (msgEl) {{ msgEl.classList.add('trans-err'); msgEl.textContent = '通讯失败：'+err; }}
      }});
  }}

  if (NEEDS_TRANS){{
    sessionBeat('open', false);
    var beatTimer = setInterval(function(){{ sessionBeat('beat', false); }}, 10000);
    startTranscodeAndPlay();
    if (v) {{
      v.addEventListener('ended', function(){{ sessionBeat('close', false); }});
      v.addEventListener('seeking', scheduleJump);
    }}
    window.addEventListener('pagehide', function(){{ sessionBeat('close', true); clearInterval(beatTimer); }});
    window.addEventListener('beforeunload', function(){{ sessionBeat('close', true); }});
  }} else {{
    loadDirect(ORIGINAL_SRC, ORIGINAL_MIME);
  }}

  // --- 字幕：JS 自绘浮层（不依赖 WebView 原生 track 渲染）+ 选单 / 测试 / 诊断 ---
  var SUBS = {json.dumps(sub_listing)};
  var TRANS_KEY = {json.dumps(trans_key)};
  var SUB_PREF_KEY = 'mitm_sub_pref:' + TRANS_KEY;
  var subPanel = document.getElementById('sub-panel');
  var subSummary = document.getElementById('sub-summary');
  var subList = document.getElementById('sub-list');
  var subSelect = document.getElementById('sub-select');
  var subSize = document.getElementById('sub-size');

  // 在 vp-pan 内放一个绝对定位 overlay 显示字幕
  // 关键：放在 vp-pan 内，video 元素之后；这样视频被 pinch 缩放/平移时，
  // 字幕作为同一变换容器的子元素，会一起缩放与移动，永远贴合画面。
  var subOverlay = document.createElement('div');
  subOverlay.className = 'sub-overlay';
  subOverlay.id = 'sub-overlay';
  if (pan) pan.appendChild(subOverlay); else if (stage) stage.appendChild(subOverlay);
  function setOverlayText(t){{
    subOverlay.innerHTML = t ? ('<span>' + t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>') + '</span>') : '';
  }}

  // VTT 解析（兼容简单内联标签 <i> <b> <c.xx>）
  function parseVTTTimestamp(s){{
    var ps = s.split(':');
    if (ps.length === 3) return parseInt(ps[0],10)*3600 + parseInt(ps[1],10)*60 + parseFloat(ps[2]);
    if (ps.length === 2) return parseInt(ps[0],10)*60 + parseFloat(ps[1]);
    return parseFloat(s);
  }}
  function parseVTT(text){{
    var lines = text.replace(/\\r/g, '').split('\\n');
    var cues = [];
    var i = 0;
    while (i < lines.length){{
      var line = lines[i];
      var m = line.match(/(\\d{{1,2}}:)?\\d{{1,2}}:\\d{{2}}[.,]\\d{{1,3}}\\s*-->\\s*(\\d{{1,2}}:)?\\d{{1,2}}:\\d{{2}}[.,]\\d{{1,3}}/);
      if (m){{
        // 整行重新分两个时间戳
        var arrow = line.replace(/,/g, '.').split('-->');
        var st = parseVTTTimestamp(arrow[0].trim());
        var en = parseVTTTimestamp(arrow[1].trim().split(/\\s/)[0]);
        i++;
        var bodyLines = [];
        while (i < lines.length && lines[i].trim() !== ''){{
          bodyLines.push(lines[i]);
          i++;
        }}
        var body = bodyLines.join('\\n').replace(/<\\/?[a-zA-Z][^>]*>/g,'').trim();
        if (isFinite(st) && isFinite(en) && body) cues.push({{s:st, e:en, t:body}});
      }}
      i++;
    }}
    return cues;
  }}

  var activeCues = [];
  var lastScanFrom = 0;
  function updateOverlay(){{
    if (!v) return;
    if (!activeCues || !activeCues.length){{ setOverlayText(''); return; }}
    var t = v.currentTime;
    var n = activeCues.length;
    // 跳过完全早于 t 的 cue（线性向前推进，复杂度 O(1)/帧均摊）
    var start = lastScanFrom;
    if (start >= n || t < activeCues[start].s) start = 0;
    while (start < n && activeCues[start].e <= t) start++;
    lastScanFrom = Math.max(0, start - 1);
    // 收集所有 [s, e) 包住 t 的 cue（双语字幕常以同时段两条 cue 出现）
    var lines = [];
    var seen = {{}};
    for (var i = start; i < n; i++){{
      var c = activeCues[i];
      if (c.s > t) break;
      if (t >= c.s && t < c.e){{
        var txt = c.t;
        if (!seen[txt]){{ seen[txt] = 1; lines.push(txt); }}
      }}
    }}
    setOverlayText(lines.join('\\n'));
  }}
  if (v){{
    v.addEventListener('timeupdate', updateOverlay);
    v.addEventListener('seeking', updateOverlay);
    v.addEventListener('seeked', updateOverlay);
  }}

  function loadSub(idx, force){{
    activeCues = []; lastScanFrom = 0; setOverlayText('');
    // 关掉所有原生 track，避免双重显示
    if (v){{ for (var i=0; i<v.textTracks.length; i++) v.textTracks[i].mode='disabled'; }}
    if (idx < 0 || !SUBS[idx]) return;
    var s = SUBS[idx];
    if (!s.src) return;
    var url = s.src + (s.src.indexOf('?')>=0?'&':'?') + (force ? 'force=1&' : '') + 't=' + Date.now();
    fetch(url, {{cache:'no-store', credentials:'include'}})
      .then(function(r){{
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      }})
      .then(function(txt){{
        var cues = parseVTT(txt);
        cues.sort(function(a,b){{ return a.s - b.s; }});
        activeCues = cues; lastScanFrom = 0;
        if (!activeCues.length){{
          try{{ console.warn('[mitm-sub] 字幕解析无 cue:', (txt||'').slice(0, 200)); }}catch(e){{}}
        }} else {{
          updateOverlay();
          try{{ localStorage.setItem(SUB_PREF_KEY, JSON.stringify({{label:s.label, lang:s.lang, kind:s.kind}})); }}catch(e){{}}
        }}
      }})
      .catch(function(err){{
        try{{ console.warn('[mitm-sub] 加载失败:', err); }}catch(e){{}}
      }});
  }}
  function kindLabel(k){{
    if (k==='external') return '外挂';
    if (k==='internal-text') return '内封·文本';
    if (k==='internal-image') return '内封·图像（已自动烧录）';
    return '未知类型';
  }}
  function kindClass(k){{
    if (k==='internal-image') return 'tag image';
    if (k==='internal-unknown') return 'tag unk';
    return 'tag';
  }}
  // 字号：用 CSS 变量控制，pinch 缩放时自动跟 pan 一起缩放
  var SUB_SIZE_KEY = 'mitm_sub_size:' + TRANS_KEY;
  function applySubSize(v){{
    var f = parseFloat(v) || 1;
    subOverlay.style.fontSize = 'calc(clamp(1rem,2.6vw,1.55rem) * ' + f + ')';
    try{{ localStorage.setItem(SUB_SIZE_KEY, String(f)); }}catch(e){{}}
  }}
  if (subSize){{
    try{{
      var saved = localStorage.getItem(SUB_SIZE_KEY);
      if (saved) subSize.value = String(parseFloat(saved));
    }}catch(e){{}}
    applySubSize(subSize.value);
    subSize.addEventListener('change', function(){{ applySubSize(subSize.value); }});
  }}

  function preferLangScore(s){{
    var lang = (s.lang || '').toLowerCase();
    var label = (s.label || '').toLowerCase();
    var score = 0;
    if (/^zh|^chi|^chn|^cmn|^yue/.test(lang)) score += 100;
    if (/中文|简中|繁中|简体|繁体|chs|cht/.test(label)) score += 80;
    if (lang === 'und' || lang === '') score -= 20;
    if (s.kind === 'external') score += 10;  // 外挂通常更准
    return score;
  }}
  function pickInitialIdx(){{
    var textIdxs = [];
    SUBS.forEach(function(s, i){{ if (s.kind==='external' || s.kind==='internal-text') textIdxs.push(i); }});
    if (!textIdxs.length) return -1;
    // 1) 用户上次偏好
    try {{
      var pref = JSON.parse(localStorage.getItem(SUB_PREF_KEY)||'null');
      if (pref){{
        for (var k=0;k<textIdxs.length;k++){{
          var ii = textIdxs[k], ss = SUBS[ii];
          if ((pref.label && pref.label === ss.label) ||
              (pref.lang && ss.lang && pref.lang === ss.lang && pref.kind === ss.kind)){{
            return ii;
          }}
        }}
      }}
    }} catch(e) {{}}
    // 2) 中文优先 → 评分最高
    var best = textIdxs[0], bestScore = -Infinity;
    textIdxs.forEach(function(ii){{
      var sc = preferLangScore(SUBS[ii]);
      if (sc > bestScore){{ bestScore = sc; best = ii; }}
    }});
    return best;
  }}

  function renderList(){{
    if (!SUBS || !SUBS.length){{
      subSummary.textContent = '未检测到任何字幕（外挂或内封）';
      subList.innerHTML = '';
      subSelect.innerHTML = '<option value="">-- 无可用字幕 --</option>';
      return;
    }}
    var textOnly = SUBS.filter(function(s){{return s.kind==='external'||s.kind==='internal-text';}});
    var imgOnly  = SUBS.filter(function(s){{return s.kind==='internal-image';}});
    var bits = [];
    if (textOnly.length) bits.push('文本字幕 ' + textOnly.length + ' 条');
    if (imgOnly.length)  bits.push('图像字幕 ' + imgOnly.length + ' 条（自动烧到画面）');
    subSummary.textContent = bits.join(' · ');
    var html = '';
    SUBS.forEach(function(s, i){{
      var label = s.label || '(无名)';
      var lang  = s.lang ? ' [' + s.lang + ']' : '';
      var codec = s.codec ? '<span class="muted" style="margin-left:6px">' + s.codec + '</span>' : '';
      html += '<li><span class="' + kindClass(s.kind) + '">' + kindLabel(s.kind) + '</span>'
            + '<strong>' + label + '</strong><span class="muted">' + lang + '</span>' + codec + '</li>';
    }});
    subList.innerHTML = html;
    var opts = '<option value="">-- 关闭字幕 --</option>';
    SUBS.forEach(function(s, i){{
      if (s.kind==='external' || s.kind==='internal-text'){{
        opts += '<option value="' + i + '">' + label_for_opt(s) + '</option>';
      }}
    }});
    subSelect.innerHTML = opts;
    function label_for_opt(s){{
      var l = s.label || '';
      if (s.lang) l += ' [' + s.lang + ']';
      return l + ' (' + (s.kind==='external' ? '外挂' : '内封') + ')';
    }}
    var initIdx = pickInitialIdx();
    if (initIdx >= 0){{
      subSelect.value = String(initIdx);
      loadSub(initIdx, false);
    }}
  }}
  if (subSelect) subSelect.addEventListener('change', function(){{
    var pickIdx = subSelect.value === '' ? -1 : parseInt(subSelect.value, 10);
    loadSub(pickIdx, false);
  }});
  renderList();
}})();
</script>"""
    return _html_response(_shell(f"视频 - {path.name}", body))


# ---------------------------------------------------------------------------
# 图片查看器
# ---------------------------------------------------------------------------

def _image_response(flow, path: Path) -> Response:
    src = f"/file?path={_q(path)}"
    css = _BASE_CSS + _VIEWER_CSS
    body = f"""
<style>{css}</style>
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="{_back_href(path)}">← 返回</a>
  <span class="brand" title="{html.escape(path.name)}">{html.escape(path.name)}</span>
  <span class="spacer"></span>
</div>
<div class="stage" id="stage"><div class="stage-inner" id="stage-inner">
  <img alt="{html.escape(path.name)}" src="{src}" draggable="false">
</div></div>
{_viewer_tools_html(include_rotate=True)}"""
    script = f'<script>{_VIEWER_JS}</script>'
    return _html_response(_shell(f"图片 - {path.name}", body, extra_body_end=script, raw=True))


# ---------------------------------------------------------------------------
# 文本查看器
# ---------------------------------------------------------------------------

def _text_response(flow, path: Path) -> Response:
    try:
        size = path.stat().st_size
    except OSError:
        return _error_page("读取失败。")
    if size > _max_inline_bytes():
        return _error_page(f"文件过大（{_fmt_size(size)}），无法在页面内联显示；可尝试调大 MITM_MAX_INLINE_BYTES。")
    try:
        raw = path.read_bytes()
    except OSError:
        return _error_page("文件读取失败。")
    text: str = ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    body = f"""
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="{_back_href(path)}">← 返回</a>
  <span class="brand" title="{html.escape(path.name)}">{html.escape(path.name)}</span>
  <span class="spacer"></span>
</div>
<div class="content">
  <div class="card" style="padding:0;overflow:hidden">
    <pre style="margin:0;padding:14px;white-space:pre-wrap;word-break:break-word;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.9rem;color:#d0d8e6;background:#0a0d13">{html.escape(text)}</pre>
  </div>
</div>"""
    return _html_response(_shell(f"文本 - {path.name}", body))


# ---------------------------------------------------------------------------
# 音乐播放器
# ---------------------------------------------------------------------------

def _find_cover(track: Path) -> Path | None:
    for name in _COVER_NAMES:
        p = track.parent / name
        if p.is_file():
            return p
    return None


def _music_text(v: object) -> str:
    """把音频标签值规范成可展示文本。"""
    if v is None:
        return ""
    if isinstance(v, (list, tuple, set)):
        for item in v:
            s = _music_text(item)
            if s:
                return s
        return ""
    if hasattr(v, "text"):
        return _music_text(getattr(v, "text"))
    if hasattr(v, "value"):
        return _music_text(getattr(v, "value"))
    s = str(v).replace("\x00", "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _music_track_no(v: object) -> str:
    s = _music_text(v)
    if not s:
        return ""
    m = re.search(r"\d{1,3}", s)
    return m.group(0) if m else ""


def _music_year(v: object) -> str:
    s = _music_text(v)
    if not s:
        return ""
    m = re.search(r"(19|20)\d{2}", s)
    return m.group(0) if m else ""


def _music_tag_pick(tags: object, keys: tuple[str, ...], *, parse_track: bool = False, parse_year: bool = False) -> str:
    if not tags:
        return ""
    key_low = tuple(k.lower() for k in keys)
    try:
        pairs = [(str(k).lower(), tags[k]) for k in tags.keys()]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""

    def _to_val(obj: object) -> str:
        if parse_track:
            return _music_track_no(obj)
        if parse_year:
            return _music_year(obj)
        return _music_text(obj)

    # 先走精确键名，再走后缀匹配，兼容 mp4/flac/id3 的差异
    for want in key_low:
        for k, v in pairs:
            if k == want:
                s = _to_val(v)
                if s:
                    return s
    for want in key_low:
        for k, v in pairs:
            if k.endswith(want):
                s = _to_val(v)
                if s:
                    return s
    return ""


@lru_cache(maxsize=4096)
def _music_meta_cached(path_s: str, mtime_ns: int) -> dict[str, str]:
    """多策略读取音频元数据：mutagen -> ffprobe。"""
    _ = mtime_ns  # 用于缓存失效
    p = Path(path_s)
    out = {"title": "", "artist": "", "album": "", "year": "", "track_no": ""}

    # 1) mutagen（可选依赖，命中率高）
    try:
        import mutagen  # type: ignore

        a_easy = mutagen.File(path_s, easy=True)
        tags_easy = getattr(a_easy, "tags", None) if a_easy else None
        if tags_easy:
            out["title"] = _music_tag_pick(tags_easy, ("title",))
            out["artist"] = _music_tag_pick(tags_easy, ("artist", "albumartist", "performer"))
            out["album"] = _music_tag_pick(tags_easy, ("album",))
            out["track_no"] = _music_tag_pick(tags_easy, ("tracknumber", "track"), parse_track=True)
            out["year"] = _music_tag_pick(tags_easy, ("date", "year"), parse_year=True)

        if not out["title"] or not out["artist"] or not out["album"]:
            a_raw = mutagen.File(path_s)
            tags_raw = getattr(a_raw, "tags", None) if a_raw else None
            if tags_raw:
                out["title"] = out["title"] or _music_tag_pick(tags_raw, ("title", "tit2", "©nam"))
                out["artist"] = out["artist"] or _music_tag_pick(tags_raw, ("artist", "tpe1", "©art", "aart", "album_artist"))
                out["album"] = out["album"] or _music_tag_pick(tags_raw, ("album", "talb", "©alb"))
                out["track_no"] = out["track_no"] or _music_tag_pick(tags_raw, ("tracknumber", "trkn", "trck", "track"), parse_track=True)
                out["year"] = out["year"] or _music_tag_pick(tags_raw, ("date", "year", "tdrc", "©day"), parse_year=True)
    except Exception:  # noqa: BLE001
        pass

    # 2) ffprobe（当 mutagen 缺失或信息不完整时兜底）
    if not out["title"] or not out["artist"] or not out["album"] or not out["track_no"]:
        try:
            exe = _which("ffprobe")
            if exe:
                cmd = [
                    exe, "-v", "error", "-print_format", "json",
                    "-show_entries", "format_tags=title,artist,album,album_artist,date,year,track,tracknumber",
                    "-show_entries", "stream_tags=title,artist,album,track,tracknumber",
                    str(p),
                ]
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
                if r.returncode == 0 and r.stdout:
                    data = json.loads(r.stdout or "{}")
                    tags = (data.get("format") or {}).get("tags") or {}
                    streams = data.get("streams") or []
                    st_tags = {}
                    if streams and isinstance(streams, list):
                        st_tags = (streams[0] or {}).get("tags") or {}

                    def _pick(map_obj: dict, *keys: str) -> str:
                        for k in keys:
                            for real_k, real_v in map_obj.items():
                                if str(real_k).lower() == k.lower():
                                    return _music_text(real_v)
                        return ""

                    out["title"] = out["title"] or _pick(tags, "title") or _pick(st_tags, "title")
                    out["artist"] = out["artist"] or _pick(tags, "artist", "album_artist") or _pick(st_tags, "artist")
                    out["album"] = out["album"] or _pick(tags, "album") or _pick(st_tags, "album")
                    out["track_no"] = out["track_no"] or _music_track_no(_pick(tags, "track", "tracknumber") or _pick(st_tags, "track", "tracknumber"))
                    out["year"] = out["year"] or _music_year(_pick(tags, "date", "year"))
        except Exception:  # noqa: BLE001
            pass

    return out


def _guess_music_name(stem: str) -> tuple[str, str]:
    """从文件名猜测 (标题, 曲序号)。"""
    s = re.sub(r"_\d{8,}$", "", stem).strip()
    no = ""
    m = re.match(r"^\s*(\d{1,3})[\s._\-]+(.+)$", s)
    if m:
        no = m.group(1)
        s = m.group(2).strip()
    # 兼容 "歌手 - 标题" 命名
    if " - " in s:
        left, right = s.split(" - ", 1)
        if right.strip() and len(left.strip()) <= 40:
            s = right.strip()
    return s or stem, no


def _guess_music_artist_album(root_music: Path, f: Path) -> tuple[str, str]:
    album = re.sub(r"_\d{8,}$", "", f.parent.name).strip()
    artist = ""
    top = f.parent
    while top.parent != root_music and top.parent != _share_root():
        top = top.parent
        if top == top.parent:
            break
    if " - " in top.name:
        artist = top.name.split(" - ", 1)[0].strip()
    return artist, album


# 整目录的 tracks 列表缓存：按 (root, mtime, file_count) 失效；避免每次开页都跑 mutagen
_TRACKS_CACHE: dict = {}
_TRACKS_CACHE_LOCK = threading.Lock()


def _collect_music_tracks(root_music: Path) -> list[dict]:
    if not root_music.is_dir():
        return []
    # 用根目录 mtime + 子项数量做粗粒度缓存键。新增/删除/重命名会刷新；只改文件内容不会，
    # 但内容改了 _trans_key 会失效不影响这层。
    try:
        st = root_music.stat()
        cache_key = (str(root_music.resolve()), st.st_mtime_ns)
    except OSError:
        cache_key = (str(root_music), 0)
    with _TRACKS_CACHE_LOCK:
        cached = _TRACKS_CACHE.get(cache_key)
        if cached is not None:
            return cached
    tracks: list[dict] = []
    try:
        for f in sorted(root_music.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in _AUDIO_EXTS:
                continue
            cover = _find_cover(f)
            rel = _rel_of(f)
            title_guess, no_guess = _guess_music_name(f.stem)
            artist_guess, album_guess = _guess_music_artist_album(root_music, f)
            meta = _music_meta_cached(str(f), f.stat().st_mtime_ns)
            title = meta.get("title") or title_guess
            artist = meta.get("artist") or artist_guess
            album = meta.get("album") or album_guess
            track_no = meta.get("track_no") or no_guess
            year = meta.get("year") or ""
            tracks.append({
                "name": title,
                "artist": artist,
                "album": album,
                "year": year,
                "track_no": track_no,
                "src": f"/file?path={_q(f)}",
                "cover": f"/file?path={_q(cover)}" if cover else "",
                "rel": rel,
            })
    except OSError:
        return []
    with _TRACKS_CACHE_LOCK:
        _TRACKS_CACHE[cache_key] = tracks
    return tracks


def _music_tracks_response(flow) -> Response:
    """悬浮迷你播放器使用的曲目 JSON。无音乐权限直接返回空数组。"""
    ctx = user_auth.get_user_ctx_from_flow(flow)
    if not user_auth.feature_allowed(ctx, "fe_music"):
        body = b"[]"
    else:
        root = _share_root()
        music_dir = root / _DIR_MUSIC
        tracks = _collect_music_tracks(music_dir)
        body = json.dumps(tracks, ensure_ascii=False).encode("utf-8")
    return Response.make(200, body, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Length": str(len(body)),
    })


def _music_response(flow) -> Response:
    root = _share_root()
    music_dir = root / _DIR_MUSIC
    tracks = _collect_music_tracks(music_dir)
    tracks_json = _json_embed(tracks)

    css = r"""
:root{--mitm-c1:rgb(28,30,46);--mitm-c2:rgb(58,86,148);--mitm-c3:rgb(214,116,160)}
.app:has(.mitm-music-page){min-height:100dvh;max-height:100dvh;overflow:hidden;box-sizing:border-box}
@supports not selector(:has(*)){.app{min-height:100dvh}}
.content.mitm-music-page{display:flex;flex-direction:column;min-height:0;flex:1 1 auto;padding-top:8px;padding-bottom:10px}
.mitm-music-page{flex:1 1 auto;min-height:0;max-height:calc(100dvh - 50px);overflow:hidden;box-sizing:border-box;display:flex;flex-direction:column}
.music-layout{flex:1;min-width:0;min-height:0;max-width:1280px;margin:0 auto;width:100%;display:flex;flex-direction:column;overflow:hidden}
.player{display:flex;flex-direction:row;align-items:stretch;gap:0;flex:1;min-width:0;min-height:0;
  border-radius:14px;overflow:hidden;border:1px solid var(--line);background:var(--card)}
.player-left{flex:0 0 clamp(280px,32vw,360px);min-width:0;width:clamp(280px,32vw,360px);max-width:100%;
  height:100%;min-height:0;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-start;align-items:center;
  position:relative;flex-shrink:0}
.player-right{flex:1;min-width:0;min-height:0;display:flex;flex-direction:column;overflow:hidden;
  background:rgba(12,16,24,.4);border-left:1px solid var(--line)}
.playlist-head{padding:10px 14px;font-size:.82rem;font-weight:600;color:var(--muted);
  border-bottom:1px solid var(--line);flex-shrink:0;background:rgba(0,0,0,.1)}
.playlist-scroll{flex:1;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}
@media (max-width:900px){
  .app:has(.mitm-music-page){max-height:none;overflow:auto}
  .mitm-music-page{max-height:none;overflow:visible;flex:1 0 auto}
  .player{flex-direction:column;min-height:min(50dvh,400px);max-height:none;flex:1 0 auto}
  .player-left{flex:0 0 auto;width:100%;max-width:100%;height:auto}
  .player-right{min-height:min(50dvh,480px);max-height:55dvh;border-left:none;border-top:1px solid var(--line)}
}
.np-card{width:100%;max-width:100%;min-width:0;padding:0;overflow:hidden;border:none;box-shadow:none;background:transparent;margin:0}
.np-inner{padding:18px;display:flex;flex-direction:column;gap:12px;align-items:center;text-align:center;width:100%;max-width:100%;min-width:0;box-sizing:border-box}
.np-cover{position:relative;width:min(72%,260px);max-width:100%;aspect-ratio:1/1;border-radius:20px;background:#222 center/cover;background-size:cover;background-repeat:no-repeat;background-position:center;box-shadow:0 10px 36px rgba(0,0,0,.55);
  transform:scale(.96);filter:brightness(.92);transition:transform .45s cubic-bezier(.2,.8,.2,1),filter .35s ease,box-shadow .35s ease;flex:0 0 auto}
.np-cover::before{content:"";display:block;padding-bottom:100%}
html.mitm-music-playing .np-cover{transform:scale(1);filter:brightness(1);box-shadow:0 16px 44px rgba(0,0,0,.6),0 0 36px var(--mitm-c3)}
.np-cover.no-cover,.immersive-cover.no-cover{background:linear-gradient(145deg,var(--mitm-c2,#3a5694),var(--mitm-c1,#1c1e2e))}
.np-cover.no-cover::after,.immersive-cover.no-cover::after{content:"";position:absolute;inset:24%;background:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'><path fill='white' fill-opacity='0.88' d='M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z'/></svg>") center/contain no-repeat;filter:drop-shadow(0 6px 18px rgba(0,0,0,.4))}
.np-title{font-size:1.05rem;font-weight:700;line-height:1.7;max-width:100%;padding:4px 4px 6px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.np-meta{color:var(--muted);font-size:.9rem;line-height:1.7;max-width:100%;padding:0 4px 4px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.seek-row{position:relative;width:100%;display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:center;color:var(--muted);font-size:.78rem;font-variant-numeric:tabular-nums;min-width:0}
.seek-wrap{position:relative;min-width:0}
.seek{--mitm-prog:0%;appearance:none;-webkit-appearance:none;width:100%;height:22px;background:transparent;cursor:pointer;display:block}
.seek::-webkit-slider-runnable-track{height:5px;border-radius:999px;background:linear-gradient(90deg,#9dd1ff var(--mitm-prog),rgba(255,255,255,.2) var(--mitm-prog))}
.seek::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:#fff;margin-top:-4.5px;box-shadow:0 4px 14px rgba(0,0,0,.5);transition:transform .15s ease}
.seek:active::-webkit-slider-thumb,.seek:hover::-webkit-slider-thumb{transform:scale(1.18)}
.seek::-moz-range-track{height:5px;border-radius:999px;background:rgba(255,255,255,.22)}
.seek::-moz-range-progress{height:5px;border-radius:999px;background:#9dd1ff}
.seek::-moz-range-thumb{width:14px;height:14px;border:0;border-radius:50%;background:#fff}
.seek-tip{position:absolute;bottom:30px;left:0;transform:translateX(-50%);padding:3px 8px;border-radius:999px;background:rgba(20,24,36,.92);color:#f4f7ff;font-size:.74rem;pointer-events:none;opacity:0;transition:opacity .12s ease;white-space:nowrap;font-variant-numeric:tabular-nums;z-index:5}
.seek-tip.show{opacity:1}

.controls{display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap;width:100%}
.controls button{min-width:46px;min-height:42px;padding:0 10px;border-radius:999px;border:1px solid var(--line);background:rgba(255,255,255,.08);color:var(--fg);font-weight:700;font-size:1rem;line-height:1}
.controls .play{min-width:64px;font-size:1.3rem;background:linear-gradient(180deg,#4f8fff,#3a7ae8);border-color:transparent;color:#fff}
.controls .mode.on{background:rgba(124,193,255,.2);color:#9dd1ff;border-color:rgba(124,193,255,.3)}
.controls .text-btn{font-size:.84rem;font-weight:700;padding:0 12px}
.volume{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.8rem;width:100%;min-width:0}
.volume .seek{flex:1}
.track{display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer}
.track:hover{background:rgba(255,255,255,.04)}
.track.active{background:rgba(124,193,255,.12);color:#9dd1ff}
.track .idx{min-width:32px;color:var(--muted);font-size:.8rem;text-align:right}
.track .thumb{width:36px;height:36px;flex:0 0 36px;border-radius:8px;background:linear-gradient(145deg,#27314a,#15192a) center/cover}
.track .info{flex:1;min-width:0}
.track .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track .meta{color:var(--muted);font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

html.mitm-immersive-open,html.mitm-immersive-open body{overflow:hidden!important}
.immersive-layer{position:fixed;inset:0;z-index:2147482000;display:none;opacity:0;transition:opacity .28s ease;background:#06070c;color:#f4f7ff}
.immersive-layer.show{display:block;opacity:1}
.immersive-bg{position:absolute;inset:-12%;z-index:0;
  background:
    radial-gradient(60% 60% at 18% 22%, var(--mitm-c3) 0%, transparent 70%),
    radial-gradient(55% 55% at 82% 18%, var(--mitm-c2) 0%, transparent 70%),
    radial-gradient(70% 70% at 50% 92%, var(--mitm-c1) 0%, transparent 75%),
    linear-gradient(180deg,#080a13,#05060c);
  filter:saturate(1.25) blur(8px);
  animation:mitmMesh 18s ease-in-out infinite alternate}
.immersive-bg:after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(7,9,14,.05),rgba(7,9,14,.55))}
@keyframes mitmMesh{
  0%{transform:translate3d(-2%,-1%,0) scale(1)}
  50%{transform:translate3d(2%,1%,0) scale(1.05)}
  100%{transform:translate3d(-1%,2%,0) scale(1.02)}
}
.immersive-shell{position:relative;z-index:2;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;
  padding:max(56px,env(safe-area-inset-top)) 18px max(22px,env(safe-area-inset-bottom));gap:14px;overflow-y:auto;-webkit-overflow-scrolling:touch}
.immersive-top{position:absolute;left:0;right:0;top:max(10px,env(safe-area-inset-top));display:flex;justify-content:flex-end;padding:0 12px;z-index:3}
.immersive-exit{min-height:36px;padding:0 14px;border-radius:999px;border:1px solid rgba(255,255,255,.28);background:rgba(20,24,36,.55);color:#f4f7ff;font-size:.86rem}
/* 同时用 vw/vh/px 三重 min() 锁正方形：保证不会超过屏幕高度的 42% */
.immersive-cover{position:relative;width:min(72vw,42vh,360px);max-width:100%;aspect-ratio:1/1;border-radius:30px;background:#1f2536 center/cover;background-size:cover;background-repeat:no-repeat;background-position:center;
  box-shadow:0 28px 72px rgba(0,0,0,.56);transform:scale(.94);filter:brightness(.85);
  transition:transform .55s cubic-bezier(.2,.8,.2,1),filter .35s ease,box-shadow .35s ease;flex:0 0 auto}
/* 兼容不支持 aspect-ratio 的 WebView，用 padding-bottom 撑出 1:1 */
.immersive-cover::before{content:"";display:block;padding-bottom:100%}
html.mitm-music-playing .immersive-cover{transform:scale(1);filter:brightness(1);
  box-shadow:0 32px 84px rgba(0,0,0,.6),0 0 80px var(--mitm-c3)}
/* flex:0 0 auto + 显式高度，避免 immersive-shell(overflow:auto) 收缩子项导致字被裁 */
.immersive-title{flex:0 0 auto;max-width:min(86vw,560px);width:min(86vw,560px);font-size:clamp(1.1rem,3.2vw,1.7rem);font-weight:800;line-height:1.5;text-align:center;color:#fff;padding:10px 8px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;box-sizing:border-box;min-height:calc(clamp(1.1rem,3.2vw,1.7rem) * 1.5 + 20px)}
.immersive-meta{flex:0 0 auto;max-width:min(86vw,560px);width:min(86vw,560px);color:rgba(236,241,255,.78);font-size:.95rem;line-height:1.5;text-align:center;padding:6px 8px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;box-sizing:border-box;min-height:calc(.95rem * 1.5 + 12px)}
.immersive-progress{flex:0 0 auto;position:relative;width:min(86vw,560px);display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;color:rgba(236,241,255,.86);font-variant-numeric:tabular-nums;font-size:.84rem;min-width:0;padding:4px 0}
.immersive-controls{flex:0 0 auto;display:flex;flex-wrap:wrap;justify-content:center;gap:10px}
.immersive-controls button{min-width:50px;min-height:44px;padding:0 10px;border-radius:999px;border:1px solid rgba(255,255,255,.28);background:rgba(255,255,255,.08);color:#fff;font-weight:700;line-height:1}
.immersive-controls .play{min-width:70px;min-height:54px;font-size:1.28rem;background:linear-gradient(180deg,#fff,#dfe8ff);color:#0f182b;border-color:transparent}
.immersive-controls .mode.on{background:rgba(255,255,255,.22);color:#fff}
.immersive-controls .text-btn{font-size:.86rem;font-weight:700;padding:0 12px}
.immersive-volume{flex:0 0 auto;width:min(86vw,560px);display:flex;align-items:center;gap:10px;color:rgba(236,241,255,.78);min-width:0}
.immersive-volume .seek{flex:1}
.immersive-hint{flex:0 0 auto;font-size:.76rem;color:rgba(236,241,255,.55);padding-bottom:4px}

/* 跳转模态 */
.jump-modal{position:fixed;inset:0;z-index:2147482600;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.55);padding:20px}
.jump-modal.show{display:flex}
.jump-card{width:min(360px,100%);background:#181c2a;border:1px solid rgba(255,255,255,.18);border-radius:18px;padding:18px;color:#f4f7ff;display:flex;flex-direction:column;gap:14px;box-shadow:0 24px 60px rgba(0,0,0,.5)}
.jump-card .jump-h{font-size:1rem;font-weight:700}
.jump-grid{display:flex;gap:10px;align-items:flex-end;justify-content:center}
.jump-cell{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0}
.jump-cell input{width:100%;height:54px;text-align:center;border-radius:12px;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.06);color:#fff;font-size:1.55rem;font-weight:800;font-variant-numeric:tabular-nums;outline:none;padding:0;letter-spacing:.04em}
.jump-cell input:focus{border-color:#9dd1ff;background:rgba(255,255,255,.1)}
.jump-cell span{font-size:.76rem;color:rgba(236,241,255,.6)}
.jump-sep{font-size:1.4rem;color:rgba(236,241,255,.6);padding-bottom:22px;font-weight:700}
.jump-row{display:flex;justify-content:flex-end;gap:8px}
.jump-btn{min-height:38px;padding:0 16px;border-radius:999px;border:1px solid rgba(255,255,255,.22);background:rgba(255,255,255,.08);color:#f4f7ff;font-weight:700;font-size:.9rem}
.jump-btn.primary{background:#9dd1ff;color:#0f182b;border-color:transparent}
"""
    js = r"""
(function(){
  var tracks = JSON.parse(document.getElementById('mitm-tracks').textContent||'[]');
  var audio = document.getElementById('audio');

  // 普通模式
  var elTitle = document.getElementById('np-title');
  var elMeta = document.getElementById('np-meta');
  var elCover = document.getElementById('np-cover');
  var elPlaylist = document.getElementById('playlist');
  var elSeek = document.getElementById('seek');
  var elCur = document.getElementById('cur-time');
  var elDur = document.getElementById('dur-time');
  var elTip = document.getElementById('seek-tip');
  var elPlay = document.getElementById('btn-play');
  var elPrev = document.getElementById('btn-prev');
  var elNext = document.getElementById('btn-next');
  var elJump = document.getElementById('btn-jump');
  var elShuffle = document.getElementById('btn-mode-shuffle');
  var elRepeat = document.getElementById('btn-mode-repeat');
  var elVol = document.getElementById('vol');
  var elImmEnter = document.getElementById('btn-immersive');

  // 沉浸模式
  var elImm = document.getElementById('immersive-layer');
  var elImmCover = document.getElementById('im-cover');
  var elImmTitle = document.getElementById('im-title');
  var elImmMeta = document.getElementById('im-meta');
  var elImmSeek = document.getElementById('im-seek');
  var elImmCur = document.getElementById('im-cur-time');
  var elImmDur = document.getElementById('im-dur-time');
  var elImmTip = document.getElementById('im-seek-tip');
  var elImmPlay = document.getElementById('im-btn-play');
  var elImmPrev = document.getElementById('im-btn-prev');
  var elImmNext = document.getElementById('im-btn-next');
  var elImmJump = document.getElementById('im-btn-jump');
  var elImmShuffle = document.getElementById('im-btn-shuffle');
  var elImmRepeat = document.getElementById('im-btn-repeat');
  var elImmVol = document.getElementById('im-vol');
  var elImmExit = document.getElementById('btn-immersive-exit');

  var STATE_KEY = 'mitm_music_state_v3';
  function loadState(){ try{return JSON.parse(localStorage.getItem(STATE_KEY)||'{}')||{};}catch(e){return {};} }
  function saveState(){
    try{ localStorage.setItem(STATE_KEY, JSON.stringify({i:curIdx,pos:audio.currentTime||0,vol:audio.volume,shuffle:shuffle,repeat:repeatMode,playing:!audio.paused})); }catch(e){}
  }
  function escape(s){
    return (s||'').replace(/[&<>"']/g,function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];
    });
  }
  function fmtTime(s){
    if(!isFinite(s) || s<0) return '0:00';
    var t=Math.floor(s), h=Math.floor(t/3600), m=Math.floor((t%3600)/60), x=t%60;
    var pad=function(n){return n<10?'0'+n:''+n;};
    return h>0 ? (h+':'+pad(m)+':'+pad(x)) : (m+':'+pad(x));
  }
  function parseTimeStr(s){
    if (typeof s !== 'string') return NaN;
    var t = s.trim(); if (!t) return NaN;
    var ps = t.split(':').map(function(x){return parseFloat(x.replace(/[^0-9.]/g,''));});
    if (ps.some(isNaN)) return NaN;
    if (ps.length===1) return ps[0];
    if (ps.length===2) return ps[0]*60 + ps[1];
    if (ps.length===3) return ps[0]*3600 + ps[1]*60 + ps[2];
    return NaN;
  }
  function metaText(t){
    var p=[]; if(t.artist) p.push(t.artist); if(t.album) p.push(t.album); if(t.year) p.push(t.year);
    return p.join(' · ');
  }

  var st = loadState();
  var curIdx = (typeof st.i==='number' && st.i>=0 && st.i<tracks.length) ? st.i : 0;
  var shuffle = !!st.shuffle;
  var repeatMode = st.repeat || 'all';
  audio.volume = (typeof st.vol==='number') ? st.vol : 0.8;
  elVol.value = Math.round(audio.volume*100);
  elImmVol.value = Math.round(audio.volume*100);

  function updateModeButtons(){
    function setMode(s,r){
      s.classList.toggle('on', shuffle);
      r.classList.remove('on'); r.textContent='🔁';
      if(repeatMode==='all'){ r.classList.add('on'); }
      else if(repeatMode==='one'){ r.classList.add('on'); r.textContent='🔂'; }
    }
    setMode(elShuffle, elRepeat);
    setMode(elImmShuffle, elImmRepeat);
  }
  function updatePlayButtons(){
    var txt = audio.paused ? '▶' : '⏸';
    elPlay.textContent = txt; elImmPlay.textContent = txt;
    document.documentElement.classList.toggle('mitm-music-playing', !audio.paused);
  }
  function setVolume(v){
    var nv = Math.max(0, Math.min(1, v));
    audio.volume = nv;
    var v100 = Math.round(nv*100);
    elVol.value = v100; elImmVol.value = v100;
    saveState();
  }

  // 懒加载观察者：把封面 URL 放在 data-cover，等滚到视口内才设置 background-image
  // 避免一次性发起数十/数百个图片请求把 WebView 卡死。
  var thumbObserver = null;
  function ensureThumbObserver(){
    if (thumbObserver || !('IntersectionObserver' in window)) return;
    thumbObserver = new IntersectionObserver(function(entries){
      entries.forEach(function(en){
        if (!en.isIntersecting) return;
        var el = en.target;
        if (el.dataset.loaded === '1') return;
        var url = el.dataset.cover || '';
        if (url) el.style.backgroundImage = 'url(' + url + ')';
        el.dataset.loaded = '1';
        thumbObserver.unobserve(el);
      });
    }, {root: elPlaylist.parentNode || null, rootMargin: '200px 0px', threshold: 0.01});
  }

  function renderPlaylist(){
    var html='';
    tracks.forEach(function(t,i){
      var meta = metaText(t);
      if (t.track_no) meta = '#'+t.track_no + (meta ? ' · ' + meta : '');
      var coverAttr = t.cover ? ' data-cover="'+t.cover+'"' : '';
      html += '<div class="track'+(i===curIdx?' active':'')+'" data-i="'+i+'">'
           +  '<div class="idx">'+(i+1)+'</div>'
           +  '<div class="thumb"'+coverAttr+'></div>'
           +  '<div class="info"><div class="name">'+escape(t.name)+'</div>'
           +  '<div class="meta">'+escape(meta||' ')+'</div></div>'
           +  '</div>';
    });
    elPlaylist.innerHTML = html;
    ensureThumbObserver();
    elPlaylist.querySelectorAll('.track').forEach(function(el){
      el.addEventListener('click', function(){ playIndex(parseInt(el.getAttribute('data-i')||'0', 10)); });
      var thumb = el.querySelector('.thumb');
      if (thumb && thumb.dataset.cover){
        if (thumbObserver) thumbObserver.observe(thumb);
        else thumb.style.backgroundImage = 'url(' + thumb.dataset.cover + ')';
      }
    });
    // 当前激活行立即加载封面（无需等滚动）
    var act = elPlaylist.querySelector('.track.active .thumb');
    if (act && act.dataset.cover && act.dataset.loaded !== '1'){
      act.style.backgroundImage = 'url(' + act.dataset.cover + ')';
      act.dataset.loaded = '1';
      if (thumbObserver) thumbObserver.unobserve(act);
    }
  }

  // 封面取色（同源 /file?path=... 可读 canvas）
  var paletteCache = {};
  function rgb2css(rgb){ return 'rgb('+rgb[0]+','+rgb[1]+','+rgb[2]+')'; }
  function applyPalette(palette){
    var def = [[28,30,46],[58,86,148],[214,116,160]];
    var pal = (palette && palette.length) ? palette.slice() : def;
    while (pal.length < 3) pal.push(pal[pal.length-1] || def[pal.length] || def[0]);
    var root = document.documentElement;
    root.style.setProperty('--mitm-c1', rgb2css(pal[0]));
    root.style.setProperty('--mitm-c2', rgb2css(pal[1]));
    root.style.setProperty('--mitm-c3', rgb2css(pal[2]));
  }
  function extractPalette(url, cb){
    if (!url){ cb(null); return; }
    if (paletteCache[url]) { cb(paletteCache[url]); return; }
    var img = new Image();
    img.crossOrigin = 'anonymous';
    img.referrerPolicy = 'no-referrer';
    img.onload = function(){
      try{
        var w=40, h=40, c=document.createElement('canvas');
        c.width=w; c.height=h;
        var ctx=c.getContext('2d');
        ctx.drawImage(img,0,0,w,h);
        var data=ctx.getImageData(0,0,w,h).data, buckets={};
        for (var i=0;i<data.length;i+=4){
          var r=data[i],g=data[i+1],b=data[i+2],a=data[i+3];
          if (a<200) continue;
          var mx=Math.max(r,g,b), mn=Math.min(r,g,b);
          if (mx-mn<16) continue;
          if (mx<50 || mn>235) continue;
          var k=(r>>5)+'_'+(g>>5)+'_'+(b>>5);
          var bk = buckets[k] || (buckets[k] = {r:0,g:0,b:0,n:0});
          bk.r+=r; bk.g+=g; bk.b+=b; bk.n++;
        }
        var arr=[]; for (var key in buckets) if (buckets.hasOwnProperty(key)) arr.push(buckets[key]);
        arr.sort(function(a,b){return b.n-a.n;});
        var pal = arr.slice(0,3).map(function(bk){ return [Math.round(bk.r/bk.n), Math.round(bk.g/bk.n), Math.round(bk.b/bk.n)]; });
        if (!pal.length){ cb(null); return; }
        paletteCache[url] = pal;
        cb(pal);
      }catch(e){ cb(null); }
    };
    img.onerror = function(){ cb(null); };
    img.src = url;
  }

  // 仅切换 .active 类，不重建 DOM，不重新触发懒加载
  function _setActiveRow(i){
    var rows = elPlaylist.querySelectorAll('.track');
    for (var k = 0; k < rows.length; k++){
      var r = rows[k];
      if (parseInt(r.getAttribute('data-i')||'-1', 10) === i) {
        r.classList.add('active');
        // 当前激活行立即载入封面（如果之前还没载入）
        var thumb = r.querySelector('.thumb');
        if (thumb && thumb.dataset.cover && thumb.dataset.loaded !== '1') {
          thumb.style.backgroundImage = 'url(' + thumb.dataset.cover + ')';
          thumb.dataset.loaded = '1';
          if (thumbObserver) thumbObserver.unobserve(thumb);
        }
      } else {
        r.classList.remove('active');
      }
    }
  }

  function loadTrack(i, autoplay){
    if (!tracks.length) return;
    if (i<0) i=tracks.length-1;
    if (i>=tracks.length) i=0;
    curIdx = i;
    var t = tracks[i];

    // 1) 优先：立刻让音频开始走，让播放从设置 src 起步
    audio.src = t.src;
    if (autoplay) {
      var p = audio.play();
      if (p && p.catch) p.catch(function(){});
    }

    // 2) 标题/副信息（轻量 DOM 更新）
    var m = metaText(t);
    elTitle.textContent = t.name || '未知标题';
    elMeta.textContent = m || '未知专辑';
    elImmTitle.textContent = t.name || '未知标题';
    elImmMeta.textContent = m || '未知专辑';
    document.title = (t.name||'音乐') + ' - 音乐';

    // 3) 封面（只更新当前播放的 2 张大图 + ambient，不动歌单 DOM）
    if (t.cover){
      elCover.style.backgroundImage = 'url('+t.cover+')';
      elImmCover.style.backgroundImage = 'url('+t.cover+')';
      elCover.classList.remove('no-cover');
      elImmCover.classList.remove('no-cover');
    } else {
      elCover.style.backgroundImage = '';
      elImmCover.style.backgroundImage = '';
      elCover.classList.add('no-cover');
      elImmCover.classList.add('no-cover');
    }

    // 4) 进度条/时间复位
    elSeek.value=0; elImmSeek.value=0;
    elCur.textContent='0:00'; elImmCur.textContent='0:00';
    elDur.textContent='0:00'; elImmDur.textContent='0:00';
    elSeek.style.setProperty('--mitm-prog','0%');
    elImmSeek.style.setProperty('--mitm-prog','0%');

    // 5) 仅切换激活行高亮（不重建歌单 DOM）
    _setActiveRow(i);

    // 6) 后置：调色 + MediaSession（这些慢，不阻塞播放）
    setTimeout(function(){ extractPalette(t.cover, applyPalette); }, 0);
    if ('mediaSession' in navigator){
      try{
        navigator.mediaSession.metadata = new MediaMetadata({
          title:t.name||'', artist:t.artist||'', album:t.album||'',
          artwork: t.cover ? [{src:t.cover, sizes:'512x512', type:'image/jpeg'}] : []
        });
        navigator.mediaSession.setActionHandler('previoustrack', prev);
        navigator.mediaSession.setActionHandler('nexttrack', next);
        navigator.mediaSession.setActionHandler('play', function(){ audio.play(); });
        navigator.mediaSession.setActionHandler('pause', function(){ audio.pause(); });
      }catch(e){}
    }
    saveState();
  }

  function playIndex(i){ loadTrack(i, true); }
  function next(){
    if (!tracks.length) return;
    var i;
    if (shuffle){
      if (tracks.length<=1) i=curIdx;
      else { do { i = Math.floor(Math.random()*tracks.length); } while(i===curIdx); }
    } else {
      i = curIdx+1;
      if (i>=tracks.length){ if (repeatMode==='all') i=0; else { audio.pause(); return; } }
    }
    loadTrack(i, true);
  }
  function prev(){
    if (!tracks.length) return;
    if (audio.currentTime>3){ audio.currentTime=0; return; }
    var i = curIdx-1;
    if (i<0) i=tracks.length-1;
    loadTrack(i, true);
  }

  function syncProgress(){
    elCur.textContent = fmtTime(audio.currentTime);
    elImmCur.textContent = fmtTime(audio.currentTime);
    if (isFinite(audio.duration) && audio.duration>0){
      var d = fmtTime(audio.duration);
      var pct = (audio.currentTime/audio.duration);
      elDur.textContent = d; elImmDur.textContent = d;
      elSeek.value = Math.round(pct*1000);
      elImmSeek.value = Math.round(pct*1000);
      var pcts = (pct*100).toFixed(2)+'%';
      elSeek.style.setProperty('--mitm-prog', pcts);
      elImmSeek.style.setProperty('--mitm-prog', pcts);
    }
  }

  // 进度条交互：拖动时间预览 + 释放时跳转
  function bindSeek(seek, tip){
    function ratioFromEvent(ev){
      var rect = seek.getBoundingClientRect();
      var x = (ev.touches && ev.touches[0]) ? ev.touches[0].clientX : ev.clientX;
      return Math.max(0, Math.min(1, (x - rect.left) / rect.width));
    }
    function showTipAt(ratio){
      if (!tip || !isFinite(audio.duration)) return;
      var rect = seek.getBoundingClientRect();
      tip.textContent = fmtTime(ratio * audio.duration);
      tip.style.left = (rect.width * ratio) + 'px';
      tip.classList.add('show');
    }
    function hideTip(){ if (tip) tip.classList.remove('show'); }
    seek.addEventListener('input', function(){
      var pct = parseInt(seek.value,10)/1000;
      seek.style.setProperty('--mitm-prog', (pct*100).toFixed(2)+'%');
      showTipAt(pct);
    });
    seek.addEventListener('change', function(){
      if (isFinite(audio.duration)) audio.currentTime = (parseInt(seek.value,10)/1000) * audio.duration;
      hideTip();
    });
    seek.addEventListener('mousedown', function(ev){ showTipAt(ratioFromEvent(ev)); });
    seek.addEventListener('mousemove', function(ev){ if (ev.buttons) showTipAt(ratioFromEvent(ev)); });
    seek.addEventListener('mouseup', hideTip);
    seek.addEventListener('mouseleave', hideTip);
    seek.addEventListener('touchstart', function(ev){ showTipAt(ratioFromEvent(ev)); }, {passive:true});
    seek.addEventListener('touchmove', function(ev){ showTipAt(ratioFromEvent(ev)); }, {passive:true});
    seek.addEventListener('touchend', hideTip);
    seek.addEventListener('touchcancel', hideTip);
  }
  bindSeek(elSeek, elTip);
  bindSeek(elImmSeek, elImmTip);

  // 跳转：自定义模态（三段数字框，全屏沉浸下也能弹出）
  var elJumpModal = document.getElementById('jump-modal');
  var elJumpH = document.getElementById('jump-h');
  var elJumpM = document.getElementById('jump-m');
  var elJumpS = document.getElementById('jump-s');
  var elJumpOk = document.getElementById('jump-ok');
  var elJumpCancel = document.getElementById('jump-cancel');
  function pad2(n){ return n<10 ? '0'+n : ''+n; }
  function setJumpFromSec(sec){
    if (!isFinite(sec) || sec < 0) sec = 0;
    var t = Math.floor(sec);
    var h = Math.floor(t/3600), m = Math.floor((t%3600)/60), s = t%60;
    if (elJumpH) elJumpH.value = h ? String(h) : '';
    if (elJumpM) elJumpM.value = pad2(m);
    if (elJumpS) elJumpS.value = pad2(s);
  }
  function jumpSeconds(){
    var h = parseInt((elJumpH && elJumpH.value)||'0', 10) || 0;
    var m = parseInt((elJumpM && elJumpM.value)||'0', 10) || 0;
    var s = parseInt((elJumpS && elJumpS.value)||'0', 10) || 0;
    return h*3600 + m*60 + s;
  }
  function openJump(){
    if (!elJumpModal) return;
    setJumpFromSec(audio.currentTime||0);
    // 全屏沉浸态下，模态需要挂载在全屏元素内才显示
    var fsEl = document.fullscreenElement || document.webkitFullscreenElement;
    var host = fsEl || document.body;
    if (elJumpModal.parentNode !== host) host.appendChild(elJumpModal);
    elJumpModal.classList.add('show');
    elJumpModal.setAttribute('aria-hidden','false');
    setTimeout(function(){ try{ (elJumpM||elJumpS).focus(); (elJumpM||elJumpS).select(); }catch(e){} }, 50);
  }
  function closeJump(){
    if (!elJumpModal) return;
    elJumpModal.classList.remove('show');
    elJumpModal.setAttribute('aria-hidden','true');
    if (elJumpModal.parentNode !== document.body) document.body.appendChild(elJumpModal);
  }
  function applyJump(){
    var sec = jumpSeconds();
    closeJump();
    if (!isFinite(sec) || sec < 0) return;
    if (isFinite(audio.duration) && sec > audio.duration) sec = audio.duration;
    try{ audio.currentTime = sec; }catch(e){}
  }
  function bindNumField(input, next, prev, max){
    if (!input) return;
    input.addEventListener('input', function(){
      var v = (input.value||'').replace(/\D/g,'').slice(0,2);
      if (v && max != null){
        var n = parseInt(v,10);
        if (n > max) v = String(max);
      }
      input.value = v;
      if (v.length >= 2 && next) next.focus();
    });
    input.addEventListener('keydown', function(e){
      if (e.key === 'Backspace' && !input.value && prev){ e.preventDefault(); prev.focus(); }
      if (e.key === 'Enter'){ e.preventDefault(); applyJump(); }
      if (e.key === 'Escape'){ e.preventDefault(); closeJump(); }
    });
    input.addEventListener('focus', function(){ try{ input.select(); }catch(e){} });
  }
  bindNumField(elJumpH, elJumpM, null, 99);
  bindNumField(elJumpM, elJumpS, elJumpH, 59);
  bindNumField(elJumpS, null, elJumpM, 59);
  if (elJumpOk) elJumpOk.addEventListener('click', applyJump);
  if (elJumpCancel) elJumpCancel.addEventListener('click', closeJump);
  if (elJumpModal) elJumpModal.addEventListener('click', function(e){ if (e.target===elJumpModal) closeJump(); });
  if (elJump) elJump.addEventListener('click', openJump);
  if (elImmJump) elImmJump.addEventListener('click', openJump);

  // 沉浸模式
  var immersiveOn = false;
  function tryEnterFullscreen(){
    try{
      var p = null;
      if (elImm.requestFullscreen) p = elImm.requestFullscreen();
      else if (elImm.webkitRequestFullscreen) elImm.webkitRequestFullscreen();
      if (p && p.catch) p.catch(function(){});
    }catch(e){}
  }
  function tryExitFullscreen(){
    try{
      if (document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(function(){});
      else if (document.webkitFullscreenElement && document.webkitExitFullscreen) document.webkitExitFullscreen();
    }catch(e){}
  }
  function enterImmersive(){
    if (!tracks.length) return;
    immersiveOn = true;
    elImm.classList.add('show');
    elImm.setAttribute('aria-hidden', 'false');
    document.documentElement.classList.add('mitm-immersive-open');
    tryEnterFullscreen();
  }
  function exitImmersive(){
    immersiveOn = false;
    elImm.classList.remove('show');
    elImm.setAttribute('aria-hidden', 'true');
    document.documentElement.classList.remove('mitm-immersive-open');
    tryExitFullscreen();
  }

  // 普通模式控件
  elPlay.addEventListener('click', function(){ if(audio.paused) audio.play(); else audio.pause(); });
  elPrev.addEventListener('click', prev);
  elNext.addEventListener('click', next);
  elShuffle.addEventListener('click', function(){ shuffle=!shuffle; updateModeButtons(); saveState(); });
  elRepeat.addEventListener('click', function(){
    repeatMode = repeatMode==='off'?'all':(repeatMode==='all'?'one':'off');
    updateModeButtons(); saveState();
  });
  elVol.addEventListener('input', function(){ setVolume(parseInt(elVol.value,10)/100); });
  elImmEnter.addEventListener('click', enterImmersive);

  // 沉浸模式控件
  elImmPlay.addEventListener('click', function(){ if(audio.paused) audio.play(); else audio.pause(); });
  elImmPrev.addEventListener('click', prev);
  elImmNext.addEventListener('click', next);
  elImmShuffle.addEventListener('click', function(){ shuffle=!shuffle; updateModeButtons(); saveState(); });
  elImmRepeat.addEventListener('click', function(){
    repeatMode = repeatMode==='off'?'all':(repeatMode==='all'?'one':'off');
    updateModeButtons(); saveState();
  });
  elImmVol.addEventListener('input', function(){ setVolume(parseInt(elImmVol.value,10)/100); });
  elImmExit.addEventListener('click', exitImmersive);
  elImm.addEventListener('click', function(e){ if (e.target === elImm) exitImmersive(); });
  var touchStartY = 0;
  elImm.addEventListener('touchstart', function(e){ touchStartY = (e.touches && e.touches[0]) ? e.touches[0].clientY : 0; }, {passive:true});
  elImm.addEventListener('touchend', function(e){
    var y = (e.changedTouches && e.changedTouches[0]) ? e.changedTouches[0].clientY : touchStartY;
    if (y - touchStartY > 110) exitImmersive();
  }, {passive:true});
  document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && immersiveOn) exitImmersive(); });

  audio.addEventListener('play', updatePlayButtons);
  audio.addEventListener('pause', updatePlayButtons);
  audio.addEventListener('timeupdate', function(){ syncProgress(); if (Math.floor(audio.currentTime) % 5 === 0) saveState(); });
  audio.addEventListener('loadedmetadata', syncProgress);
  audio.addEventListener('ended', function(){
    if (repeatMode==='one'){ audio.currentTime=0; audio.play().catch(function(){}); return; }
    next();
  });

  updateModeButtons();
  updatePlayButtons();
  renderPlaylist();
  if (tracks.length){
    loadTrack(curIdx, false);
    if (typeof st.pos === 'number' && st.pos > 0){
      audio.addEventListener('loadedmetadata', function(){ try{ audio.currentTime = st.pos; }catch(e){} }, {once:true});
    }
    // 上一页正在播放 → 自动续播（webview 限制 autoplay 时静默失败也无所谓）
    if (st.playing){
      var startPlay = function(){ try{ audio.play().catch(function(){}); }catch(e){} };
      if (audio.readyState >= 2) startPlay();
      else audio.addEventListener('loadeddata', startPlay, {once:true});
    }
  } else {
    elTitle.textContent='（音乐目录下没有音频文件）';
    var emptyDir = elPlaylist.getAttribute('data-empty-dir') || '音乐';
    elPlaylist.innerHTML='<div class="empty">请将音频放入 '+escape(emptyDir)+'/ 目录</div>';
    elImmEnter.disabled = true;
  }
})();
"""
    body_tpl = f"""
<style>{css}</style>
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="/">🏠</a>
  <span class="brand">音乐播放器<small>{html.escape(str(music_dir))}</small></span>
  <span class="spacer"></span>
  <span class="muted" id="count-label">{len(tracks)} 首</span>
</div>
<div class="content music-layout mitm-music-page">
  <div class="card player" style="padding:0;background:linear-gradient(165deg,#141a27,#0e1219)">
    <aside class="player-left">
      <div class="np-card">
        <div class="np-inner">
          <div class="np-cover" id="np-cover"></div>
          <div class="np-title" id="np-title">—</div>
          <div class="np-meta" id="np-meta">—</div>
          <div class="seek-row">
            <span id="cur-time">0:00</span>
            <div class="seek-wrap">
              <input class="seek" type="range" id="seek" min="0" max="1000" value="0" aria-label="播放进度">
              <div class="seek-tip" id="seek-tip">0:00</div>
            </div>
            <span id="dur-time">0:00</span>
          </div>
          <div class="controls">
            <button id="btn-mode-shuffle" class="mode" title="随机">🔀</button>
            <button id="btn-prev" title="上一首">⏮</button>
            <button id="btn-play" class="play" title="播放/暂停">▶</button>
            <button id="btn-next" title="下一首">⏭</button>
            <button id="btn-mode-repeat" class="mode" title="循环">🔁</button>
            <button id="btn-jump" class="text-btn" title="跳转到指定时间">跳转</button>
            <button id="btn-immersive" class="text-btn" title="沉浸播放">沉浸</button>
          </div>
          <div class="volume">
            <span>🔊</span><input class="seek" type="range" id="vol" min="0" max="100" value="80" aria-label="音量">
          </div>
        </div>
      </div>
    </aside>
    <div class="player-right">
      <div class="playlist-head">播放列表</div>
      <div class="playlist-scroll">
        <div id="playlist" data-empty-dir="{html.escape(_DIR_MUSIC)}"></div>
      </div>
    </div>
  </div>

  <div class="immersive-layer" id="immersive-layer" aria-hidden="true">
    <div class="immersive-bg" id="immersive-bg"></div>
    <div class="immersive-shell">
      <div class="immersive-top">
        <button class="immersive-exit" id="btn-immersive-exit">退出沉浸</button>
      </div>
      <div class="immersive-cover" id="im-cover"></div>
      <div class="immersive-title" id="im-title">—</div>
      <div class="immersive-meta" id="im-meta">—</div>
      <div class="immersive-progress">
        <span id="im-cur-time">0:00</span>
        <div class="seek-wrap">
          <input class="seek" type="range" id="im-seek" min="0" max="1000" value="0" aria-label="播放进度">
          <div class="seek-tip" id="im-seek-tip">0:00</div>
        </div>
        <span id="im-dur-time">0:00</span>
      </div>
      <div class="immersive-controls">
        <button id="im-btn-shuffle" class="mode" title="随机">🔀</button>
        <button id="im-btn-prev" title="上一首">⏮</button>
        <button id="im-btn-play" class="play" title="播放/暂停">▶</button>
        <button id="im-btn-next" title="下一首">⏭</button>
        <button id="im-btn-repeat" class="mode" title="循环">🔁</button>
        <button id="im-btn-jump" class="text-btn" title="跳转到指定时间">跳转</button>
      </div>
      <div class="immersive-volume">
        <span>🔊</span><input class="seek" type="range" id="im-vol" min="0" max="100" value="80" aria-label="音量">
      </div>
      <div class="immersive-hint">下滑或点「退出沉浸」返回普通模式</div>
    </div>
  </div>

  <div class="jump-modal" id="jump-modal" aria-hidden="true">
    <div class="jump-card">
      <div class="jump-h">跳转到指定时间</div>
      <div class="jump-grid">
        <div class="jump-cell">
          <input id="jump-h" type="text" inputmode="numeric" autocomplete="off" placeholder="0" maxlength="2">
          <span>时（可选）</span>
        </div>
        <div class="jump-sep">:</div>
        <div class="jump-cell">
          <input id="jump-m" type="text" inputmode="numeric" autocomplete="off" placeholder="00" maxlength="2">
          <span>分</span>
        </div>
        <div class="jump-sep">:</div>
        <div class="jump-cell">
          <input id="jump-s" type="text" inputmode="numeric" autocomplete="off" placeholder="00" maxlength="2">
          <span>秒</span>
        </div>
      </div>
      <div class="jump-row">
        <button class="jump-btn" id="jump-cancel" type="button">取消</button>
        <button class="jump-btn primary" id="jump-ok" type="button">确定</button>
      </div>
    </div>
  </div>
</div>
<audio id="audio" preload="metadata"></audio>
<script id="mitm-tracks" type="application/json">{tracks_json}</script>
<script>{js}</script>"""
    return _html_response(_shell("音乐播放器", body_tpl, extra_head=_MUSIC_LOCK_HEAD))


# ---------------------------------------------------------------------------
# 上传
# ---------------------------------------------------------------------------

def _upload_get_response(flow, *, notice: str = "", upload_list: list[str] | None = None) -> Response:
    up = _ensure_upload_dir()
    rel_up = _rel_of(up)
    notice_html = f'<div class="card" style="border-color:rgba(124,193,255,.4);color:#a4d3ff">{html.escape(notice)}</div>' if notice else ""
    list_html = ""
    if upload_list:
        items = "".join(f'<li>{html.escape(n)}</li>' for n in upload_list)
        list_html = f'<div class="card"><div class="muted" style="margin-bottom:6px">已上传：</div><ul style="margin:0;padding-left:1.2em">{items}</ul></div>'
    body = f"""
<div class="topbar">
  <a class="btn btn-ghost btn-sm" href="/">🏠</a>
  <span class="brand">上传到 {html.escape(rel_up)}</span>
  <span class="spacer"></span>
  <a class="btn btn-ghost btn-sm" href="/browse?path={_q(rel_up)}">查看目录</a>
</div>
<div class="content">
  {notice_html}
  {list_html}
  <div class="card">
    <form method="post" action="/upload" enctype="multipart/form-data">
      <p class="muted" style="margin-top:0">选择文件上传到 <code>{html.escape(rel_up)}</code>；可多选。需已登录且账号具备「上传」与「浏览 u/」等权限；浏览 <code>upl/</code> 由管理员配置。</p>
      <input class="input" type="file" name="files" multiple required style="width:100%">
      <div class="row" style="margin-top:12px">
        <button class="btn btn-primary" type="submit">上传</button>
        <a class="btn btn-ghost" href="/upload">清空</a>
      </div>
    </form>
  </div>
</div>"""
    return _html_response(_shell("文件上传", body))


_FILENAME_STAR_RE = re.compile(r"filename\*\s*=\s*([^;]+)", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)


def _decode_rfc5987(val: str) -> str:
    try:
        charset, _lang, encoded = val.split("'", 2)
    except ValueError:
        return val
    try:
        return unquote(encoded, encoding=charset or "utf-8", errors="replace")
    except LookupError:
        return unquote(encoded)


def _parse_multipart(content_type: str, body: bytes) -> list[tuple[str, str, bytes]]:
    if not content_type or "multipart/form-data" not in content_type.lower():
        return []
    header = f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode("latin-1", errors="replace")
    try:
        msg = BytesParser(policy=email_policy).parsebytes(header + body)
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str, bytes]] = []
    for part in msg.iter_parts():
        cd = part.get("Content-Disposition", "") or ""
        name_m = re.search(r'name="([^"]*)"', cd)
        name = name_m.group(1) if name_m else ""
        filename = ""
        m_star = _FILENAME_STAR_RE.search(cd)
        if m_star:
            filename = _decode_rfc5987(m_star.group(1).strip().strip('"'))
        else:
            m = _FILENAME_RE.search(cd)
            if m:
                filename = m.group(1).strip()
        payload = part.get_payload(decode=True) or b""
        out.append((name, filename, payload))
    return out


def _unique_upload_path(up: Path, name: str) -> Path:
    # 基本清洗：去掉路径分隔符
    safe = re.sub(r'[\\/]', '_', name).strip()
    if not safe or safe in (".", ".."):
        safe = "upload.bin"
    target = up / safe
    if not target.exists():
        return target
    stem = Path(safe).stem
    suffix = Path(safe).suffix
    for i in range(1, 10000):
        cand = up / f"{stem} ({i}){suffix}"
        if not cand.exists():
            return cand
    return up / f"{stem}.{os.getpid()}{suffix}"


def _upload_post_response(flow) -> Response:
    ctype = flow.request.headers.get("Content-Type", "")
    body = flow.request.content or b""
    parts = _parse_multipart(ctype, body)
    if not parts:
        return _upload_get_response(flow, notice="未收到任何文件，或 Content-Type 无法解析。")
    up = _ensure_upload_dir()
    saved: list[str] = []
    for _name, filename, data in parts:
        if not filename or not data:
            continue
        target = _unique_upload_path(up, filename)
        try:
            target.write_bytes(data)
            saved.append(f"{target.name}（{_fmt_size(len(data))}）")
        except OSError as e:
            saved.append(f"{filename} 写入失败：{e}")
    if not saved:
        return _upload_get_response(flow, notice="没有有效的文件被保存。")
    return _upload_get_response(flow, notice=f"已保存 {len(saved)} 个文件到 {_rel_of(up)}/", upload_list=saved)


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

def _dispatch_open(flow, path: Path) -> Response:
    kind = _classify(path)
    if kind == "pdf":
        return _pdf_reader_response(flow, path)
    if kind in ("video", "audio"):
        return _video_response(flow, path)
    if kind == "image":
        return _image_response(flow, path)
    if kind == "text":
        return _text_response(flow, path)
    # 未知类型：尝试内联展示，不主动触发「另存为」下载
    return _binary_response(flow, path, inline=True)


def _resolve_path_from_query(flow) -> Path | None:
    rel = _query_first(flow, "path", "f", "file", "v", "open", "mitm_open")
    if not rel:
        return None
    root = _share_root()
    p = Path(rel)
    if p.is_absolute():
        try:
            rp = p.resolve()
            rp.relative_to(root)
        except (OSError, ValueError):
            return None
        return rp if rp.is_file() else None
    cand = _safe_child(root, rel)
    return cand if (cand is not None and cand.is_file()) else None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

# PDF 阅读进度：账号 + 相对路径 → 上次页码
_PDF_PROG_LOCK = threading.RLock()


def _pdf_progress_path() -> Path:
    env = (os.environ.get("MITM_DATA_DIR", "") or "").strip()
    base = Path(env).expanduser().resolve() if env else _BASE
    return base / "mitm_pdf_progress.json"


def _pdf_progress_load() -> dict:
    p = _pdf_progress_path()
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8") or "{}")
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _pdf_progress_save(data: dict) -> None:
    p = _pdf_progress_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(p)
    except OSError:
        pass


def _pdf_progress_get(user: str, rel: str) -> int | None:
    if not user or not rel:
        return None
    with _PDF_PROG_LOCK:
        d = _pdf_progress_load()
        u = d.get(user) or {}
        v = u.get(rel)
        if isinstance(v, int) and v > 0:
            return v
    return None


def _pdf_progress_set(user: str, rel: str, page: int) -> None:
    if not user or not rel or page <= 0:
        return
    with _PDF_PROG_LOCK:
        d = _pdf_progress_load()
        u = d.setdefault(user, {})
        u[rel] = int(page)
        _pdf_progress_save(d)


def _pdf_progress_response(flow) -> Response:
    if flow.request.method.upper() != "POST":
        return Response.make(405, b"", {"Content-Type": "text/plain"})
    ctx = user_auth.get_user_ctx_from_flow(flow)
    user = (getattr(ctx, "username", "") or "").strip() if ctx else ""
    if not user:
        return Response.make(401, b'{"error":"auth"}', {"Content-Type": "application/json"})
    try:
        body_text = flow.request.get_text() or ""
        data = json.loads(body_text or "{}")
    except (ValueError, AttributeError):
        data = {}
    rel = (data.get("path") or "").strip()
    rel = _unobfuscate(rel)  # body 里的 path 也可能是加密 token
    try:
        page = int(data.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    if not rel or page <= 0:
        return Response.make(400, b'{"error":"bad"}', {"Content-Type": "application/json"})
    _pdf_progress_set(user, rel, page)
    body = b'{"ok":true}'
    return Response.make(200, body, {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "Content-Length": str(len(body)),
    })


def _pdf_png_response(flow) -> Response:
    path = _resolve_path_from_query(flow)
    if path is None or _classify(path) != "pdf":
        return _error_page("未找到 PDF。")
    try:
        p = int(_query_first(flow, "p", "page") or "1")
    except ValueError:
        p = 1
    idx = max(0, p - 1)
    png, err = _pdf_page_png(path, idx, _raster_scale())
    if png is None:
        return _error_page(f"渲染失败：{err}")
    headers = {
        "Content-Type": "image/png",
        "Cache-Control": "no-store",
        "Content-Length": str(len(png)),
    }
    if flow.request.method.upper() == "HEAD":
        return Response.make(200, b"", headers)
    return Response.make(200, png, headers)


def _route(flow) -> Response:
    path = _url_path(flow)
    method = flow.request.method.upper()
    # 探针：登录用户的所有请求都进活动记录（不阻塞主流程）
    try:
        _ctx_for_probe = user_auth.get_user_ctx_from_flow(flow)
        user_auth.touch_session(flow)
        if _ctx_for_probe and _ctx_for_probe.username:
            _track_user_activity(flow, _ctx_for_probe)
    except Exception:  # noqa: BLE001
        pass
    try:
        if path == "/__login":
            return _login_response(flow)
        if path == "/__logout":
            return _logout_response(flow)
        if path == "/__mitm-exit":
            return _mitm_exit_page_response()
        if path == "/__mitm-exit-telemetry":
            return _exit_telemetry_response(flow)
        if path == "/__mitm-trap":
            return _mitm_trap_page_response(flow)
        gated = _auth_gate_response(flow, path)
        if gated is not None:
            return gated
        if path == "/__admin":
            return _admin_response(flow)
        if path == "/__admin/trans":
            return _admin_trans_response(flow)
        if path == "/__admin/activity":
            return _admin_activity_response(flow)
        if path == "/upload":
            if method == "POST":
                return _upload_post_response(flow)
            return _upload_get_response(flow)
        if path == "/music":
            return _music_response(flow)
        if path == "/music_tracks":
            return _music_tracks_response(flow)
        if path == "/subtitle":
            return _subtitle_response(flow)
        if path == "/subtitle_internal":
            return _subtitle_internal_response(flow)
        if path.startswith("/assets/"):
            return _assets_response(path)
        if path == "/file":
            p = _resolve_path_from_query(flow)
            if p is None:
                return _error_page("文件不存在。")
            dl = (_query_first(flow, "dl") or "").strip()
            return _binary_response(flow, p, inline=(dl not in ("1", "true", "yes")))
        if path == "/dl":
            return _error_page("直链下载已关闭，请用阅读/播放页或文件浏览内打开。")
        if path == "/pdf":
            p = _resolve_path_from_query(flow)
            if p is None or _classify(p) != "pdf":
                return _error_page("未找到 PDF。")
            return _pdf_reader_response(flow, p)
        if path == "/pdf.png":
            return _pdf_png_response(flow)
        if path == "/pdf_progress":
            return _pdf_progress_response(flow)
        if path == "/video":
            p = _resolve_path_from_query(flow)
            if p is None:
                return _error_page("未找到视频/音频。")
            return _video_response(flow, p)
        if path == "/video_trans_status":
            return _video_trans_status_response(flow)
        if path == "/video_trans_clear":
            return _video_trans_clear_response(flow)
        if path == "/video_trans_session":
            return _video_trans_session_response(flow)
        if path == "/video_trans_jump":
            return _video_trans_jump_response(flow)
        if path == "/__trans_debug":
            return _trans_debug_response(flow)
        if path.startswith("/hls/"):
            return _hls_response(path)
        if path == "/image":
            p = _resolve_path_from_query(flow)
            if p is None:
                return _error_page("未找到图片。")
            return _image_response(flow, p)
        if path == "/text":
            p = _resolve_path_from_query(flow)
            if p is None:
                return _error_page("未找到文本。")
            return _text_response(flow, p)
        if path == "/open":
            p = _resolve_path_from_query(flow)
            if p is None:
                return _error_page("未找到文件。")
            return _dispatch_open(flow, p)
        if path == "/browse":
            return _browse_response(flow)
        # 兜底：首页
        return _home_response(flow)
    except Exception as e:  # noqa: BLE001
        return _error_page(f"脚本异常：{e!r}")


# ---------------------------------------------------------------------------
# mitmproxy 入口
# ---------------------------------------------------------------------------

def request(flow) -> None:
    if flow.request.method.upper() == "CONNECT":
        return
    try:
        u = flow.request.pretty_url
    except (AttributeError, OSError, TypeError, ValueError):
        u = f"{_normalize_host(flow)}{_url_path(flow)}"
    _log_visit_line("HTTP", f"{flow.request.method.upper()}\t{u}", flow)
    if not _host_matches(flow):
        return
    flow.response = _route(flow)


def http_connect(flow) -> None:
    try:
        h = (flow.request.host or "").strip()
        port = int(getattr(flow.request, "port", 443) or 443)
        _log_visit_line("CONNECT", f"{h}:{port}", flow)
    except (AttributeError, OSError, TypeError, ValueError):
        pass
