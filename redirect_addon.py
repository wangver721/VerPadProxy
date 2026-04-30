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
- 任意页面右下角无文字小按钮：**一键退出**（全黑后优先进 **`/__mitm-trap`** 同源专页再跳 **`/__mitm-exit`**；`data:` 为备选；系统返回键是否由壳层拦截仍取决于 ForClass 本身）
- 多用户登录（用户名+密码），**mitm_users.json** 存用户与权限；默认管理员 **admin** / **change-me-please**（建议用 `MITM_BOOTSTRAP_PASSWORD` 覆盖首启）；`private/` 与浏览 `upl/` 由管理员配置名单与功能开关

目录约定（可由环境变量覆盖）：
  payload/
  ├── PDF/    （电子书）
  ├── 视频/   （含字幕 sidecar）
  ├── 音乐/   （支持子目录 + cover.jpg）
  ├── private/（可访问用户由管理端配置）
  └── upl/    （上传/浏览 u 由权限控制，不存在会自动创建）

所有路径均基于 `MITM_SHARE_DIR`（默认为 `C:\VerPadProxy\payload` 或 `/sdcard/VerPadProxy/payload`）。

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
_SUB_EXTS = {".srt", ".vtt"}
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
    # 未设置环境变量时：默认劫持内网两端口；可改为 zzn.sc.forclass.net 等
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


def _q(p: Path | str) -> str:
    return quote(_rel_of(p)) if isinstance(p, Path) else quote(p)


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
    if path == "/__admin" and not ctx.is_admin:
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
        sid, emsg = user_auth.login_user(u_in, pw_in or "")
        if sid:
            h3 = {
                "Location": next_url,
                "Set-Cookie": user_auth.set_session_headers(sid),
                "Cache-Control": "no-store",
            }
            return Response.make(303, b"", h3)
        err = (emsg or "登录失败")
    body = f"""
<div class="content" style="display:flex;align-items:center;justify-content:center;min-height:70vh;box-sizing:border-box">
  <div class="card" style="max-width:360px;width:100%;margin:0 12px;box-sizing:border-box">
    <p class="muted" style="text-align:center;margin:0 0 20px 0">请输入用户名与密码</p>
    {f'<p style="color:#ff9aa2;text-align:center;margin:0 0 12px 0;font-size:.9rem">{html.escape(err)}</p>' if err else ""}
    <form method="post" action="/__login" autocomplete="off">
      <input type="hidden" name="next" value="{html.escape(next_url)}">
      <input class="input" type="text" name="username" required autocomplete="off" inputmode="text" style="width:100%;margin-bottom:12px;box-sizing:border-box" aria-label="用户名">
      <input class="input" type="password" name="password" required autocomplete="off" style="width:100%;margin-bottom:16px;box-sizing:border-box" aria-label="密码">
      <button type="submit" class="btn btn-primary" style="width:100%">登录</button>
    </form>
  </div>
</div>"""
    return _html_response(_shell("登录", body, show_splash_fab=False, exit_telemetry=False))


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
    body = f"""
<div class="topbar">
  <span class="brand">用户管理</span>
  <span class="spacer"></span>
  <span class="muted" style="margin-right:8px">{html.escape(ctx0.username)}</span>
  <a class="btn btn-ghost btn-sm" href="/">返回首页</a>
  <a class="btn btn-ghost btn-sm" href="/__logout">退出登录</a>
</div>
<div class="content">
  {f'<div class="card" style="background:rgba(100,200,150,.1);border-color:var(--border)"><p style="margin:0">{html.escape(notice)}</p></div>' if notice else ''}
  <div class="card">
    <h2 style="margin:0 0 10px 0">新建用户</h2>
    <form method="post" class="row" style="flex-wrap:wrap;gap:8px;align-items:flex-end">
      <input type="hidden" name="action" value="create">
      <input class="input" name="new_username" placeholder="用户名" required style="min-width:120px" autocomplete="off">
      <input class="input" name="new_password" type="password" placeholder="密码" required style="min-width:120px" autocomplete="new-password">
      <select name="new_role" class="input" style="width:auto">
        <option value="user">普通</option>
        <option value="admin">管理员</option>
      </select>
      <button class="btn btn-primary" type="submit">创建</button>
    </form>
  </div>
  <div class="card">
    <h2 style="margin:0 0 8px 0">可访问「{_DIR_PRIVATE}」的用户</h2>
    <p class="muted" style="margin:0 0 8px 0">每行一个用户名，或逗号分隔。管理员始终可访问。</p>
    <form method="post">
      <input type="hidden" name="action" value="set_private">
      <textarea class="input" name="private_users" rows="4" style="width:100%;min-height:80px;font-family:inherit" placeholder="admin">{html.escape(priv_txt)}</textarea>
      <button class="btn btn-primary" type="submit" style="margin-top:8px">保存名单</button>
    </form>
  </div>
  {rows}
</div>"""
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
:root{color-scheme:dark;--bg:#0b0e13;--fg:#e6ecf5;--muted:#8b9bb4;--accent:#4f8fff;--card:#141a27;--card2:#0f1420;--line:rgba(255,255,255,.08)}
*,*:before,*:after{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);
  font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-tap-highlight-color:transparent;touch-action:manipulation}
body{min-height:100vh;min-height:100dvh}
a{color:#7cc1ff;text-decoration:none}
a:active{opacity:.7}
button,input,select{font:inherit;color:inherit}
button{cursor:pointer}
.app{display:flex;flex-direction:column;min-height:100vh;min-height:100dvh}
.topbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:10px;
  padding:10px 14px;padding-top:max(10px,env(safe-area-inset-top));
  background:rgba(12,16,24,.92);backdrop-filter:saturate(1.3) blur(14px);
  -webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
.topbar .brand{font-weight:700;letter-spacing:.02em;margin-right:4px;max-width:60vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topbar .brand small{color:var(--muted);font-weight:500;margin-left:6px}
.topbar .spacer{flex:1}
.btn{display:inline-flex;align-items:center;gap:6px;min-height:40px;padding:8px 14px;
  border-radius:10px;border:1px solid var(--line);
  background:rgba(255,255,255,.06);color:var(--fg);font-weight:600;text-decoration:none;white-space:nowrap}
.btn:active{transform:scale(.97)}
.btn-primary{background:linear-gradient(180deg,#4f8fff,#3a7ae8);border-color:transparent;color:#fff;
  box-shadow:0 4px 14px rgba(58,122,232,.35)}
.btn-ghost{background:transparent}
.btn-sm{min-height:32px;padding:4px 10px;border-radius:8px;font-size:.85rem}
.content{flex:1;padding:14px;padding-bottom:max(20px,env(safe-area-inset-bottom))}
.card{background:linear-gradient(165deg,var(--card) 0%,var(--card2) 100%);border:1px solid var(--line);
  border-radius:14px;padding:14px;box-shadow:0 8px 28px rgba(0,0,0,.25)}
.card + .card{margin-top:14px}
.muted{color:var(--muted);font-size:.85rem}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;
  background:rgba(124,193,255,.14);color:#9dd1ff;border:1px solid rgba(124,193,255,.3)}
.breadcrumbs{display:flex;flex-wrap:wrap;gap:4px;font-size:.9rem;color:#97a8c2}
.breadcrumbs a{padding:4px 8px;border-radius:6px;background:rgba(255,255,255,.04)}
.breadcrumbs .sep{color:#53617a;padding:4px 0}
table.files{width:100%;border-collapse:collapse}
table.files th,table.files td{padding:10px 8px;border-bottom:1px solid var(--line);font-size:.95rem;text-align:left}
table.files th{color:var(--muted);font-weight:600;font-size:.82rem}
table.files td.name{word-break:break-all}
table.files td.size,table.files td.mtime{white-space:nowrap;color:#aab7ca;font-size:.85rem}
table.files .ops{white-space:nowrap}
table.files .ops a{margin-right:10px}
.input{min-height:40px;padding:8px 12px;border-radius:10px;border:1px solid var(--line);
  background:rgba(0,0,0,.25);color:var(--fg);font-size:1rem}
.empty{padding:28px;text-align:center;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.tile{display:flex;flex-direction:column;justify-content:space-between;padding:18px;border-radius:14px;
  background:linear-gradient(145deg,rgba(79,143,255,.22),rgba(58,122,232,.08));border:1px solid rgba(124,193,255,.25);
  color:var(--fg);min-height:120px}
.tile .emoji{font-size:2rem}
.tile .title{font-weight:700;font-size:1.05rem;margin-top:6px}
.tile .desc{color:var(--muted);font-size:.85rem}
/* 全局退出：小方块上叠一层可点链接，z-index 高于提交按钮，便于各 WebView 命中导航 */
#mitm-exit-f{display:block;width:100%;height:100%;margin:0;padding:0;border:0;position:relative;z-index:1}
.mitm-fab-wrap{position:fixed;right:max(6px,env(safe-area-inset-right));bottom:max(6px,env(safe-area-inset-bottom));
  left:auto;top:auto;z-index:2147483000;isolation:isolate;pointer-events:auto;
  width:44px;height:44px;box-sizing:border-box}
.mitm-fab{position:relative;right:auto;bottom:auto;
  width:100%;height:100%;min-width:44px;min-height:44px;border-radius:12px;
  background:rgba(48,64,98,.8);border:1px solid rgba(255,255,255,.2);
  box-shadow:0 4px 20px rgba(0,0,0,.5);-webkit-tap-highlight-color:transparent;
  appearance:none;-webkit-appearance:none;cursor:pointer;
  text-decoration:none;display:block;padding:0;margin:0;outline:0;color:transparent;
  font-size:0;-webkit-user-select:none;user-select:none}
a.mitm-fab-ghost{position:absolute;left:0;top:0;right:0;bottom:0;z-index:6;width:100%;height:100%;
  text-indent:150%;white-space:nowrap;overflow:hidden;opacity:0.1;background:transparent;cursor:pointer;
  touch-action:manipulation;-webkit-tap-highlight-color:rgba(255,255,255,.1)}
a.mitm-fab:visited,a.mitm-fab:link{color:transparent}
.mitm-fab-wrap:active .mitm-fab{transform:scale(.96);background:rgba(64,86,120,.95)}
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
  html.mitm-music-locked .np-cover{max-width:min(80vw,280px)!important;max-height:32vh!important;aspect-ratio:1/1!important}
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


def _shell(title: str, body_html: str, *, extra_head: str = "", extra_body_end: str = "",
           raw: bool = False, show_splash_fab: bool = True, exit_telemetry: bool = True) -> bytes:
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
  function apply(){ inner.style.transform = 'translate('+tx+'px,'+ty+'px) scale('+scale+') rotate('+rot+'deg)'; }
  function setScale(s){ scale=Math.min(5,Math.max(0.3,s)); apply(); var l=document.getElementById('zoom-label'); if(l)l.textContent=Math.round(scale*100)+'%'; }
  function reset(){ scale=1; tx=0; ty=0; rot=0; apply(); setScale(1); }
  window.mitmFit = reset;
  window.mitmZoomIn = function(){ setScale(scale*1.2); };
  window.mitmZoomOut = function(){ setScale(scale/1.2); };
  window.mitmRotate = function(){ rot=(rot+90)%360; apply(); };
  function dlen(ax,ay,bx,by){
    var ddx=ax-bx, ddy=ay-by;
    return Math.sqrt(ddx*ddx+ddy*ddy);
  }
  stage.addEventListener('touchstart', function(e){
    if (e.touches.length===1){
      start={type:'pan',x:e.touches[0].clientX-tx,y:e.touches[0].clientY-ty,moved:false};
    } else if (e.touches.length===2){
      var a=e.touches[0],b=e.touches[1];
      start={type:'pinch',d0:dlen(a.clientX,a.clientY,b.clientX,b.clientY),s0:scale};
    }
  },{passive:true});
  stage.addEventListener('touchmove', function(e){
    if(!start) return;
    if (start.type==='pan' && e.touches.length===1 && scale>1.01){
      e.preventDefault();
      tx=e.touches[0].clientX-start.x;
      ty=e.touches[0].clientY-start.y;
      start.moved=true;
      apply();
    } else if (start.type==='pinch' && e.touches.length===2){
      e.preventDefault();
      var a=e.touches[0],b=e.touches[1];
      var d=dlen(a.clientX,a.clientY,b.clientX,b.clientY);
      start.moved=true;
      setScale(start.s0*d/start.d0);
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
      }
      start=null;
    }
  },{passive:true});
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
    return f"/browse?path={quote(_rel_of(parent))}"


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
            parts_t.append(_tile(f'/browse?path={quote(_DIR_PDF)}', '📕', 'PDF 阅读', f'共 {n_pdf} 本'))
        if user_auth.feature_allowed(ctx, "fe_video"):
            parts_t.append(_tile(f'/browse?path={quote(_DIR_VIDEO)}', '🎬', '视频', f'共 {n_vid} 个'))
        if user_auth.feature_allowed(ctx, "fe_music"):
            parts_t.append(_tile('/music', '🎵', '音乐播放器', f'共 {n_music} 首'))
        if user_auth.feature_allowed(ctx, "fe_private") and user_auth.can_browse_private_dir(ctx):
            parts_t.append(_tile(f'/browse?path={quote(_DIR_PRIVATE)}', '🔐', '私密目录 ' + _DIR_PRIVATE, f'共 {n_priv} 个'))
        if user_auth.feature_allowed(ctx, "fe_upload"):
            parts_t.append(_tile('/upload', '📤', '上传到 ' + _DIR_UPLOAD, f'已存 {n_upload} 个文件（上传后可在 u/ 浏览）'))
        if user_auth.feature_allowed(ctx, "fe_browse"):
            parts_t.append(_tile('/browse', '📁', '全部文件', '文件浏览器'))
    grid_inner = "".join(parts_t) if parts_t else '<p class="muted" style="margin:0">当前账号未开启任何入口，请联系管理员。</p>'
    nav: list[str] = ['<span class="spacer"></span>']
    if ctx is not None:
        nav.append(f'<span class="muted" style="margin-right:8px">{html.escape(ctx.username)}</span>')
        if ctx.is_admin:
            nav.append('<a class="btn btn-ghost btn-sm" href="/__admin">管理</a>')
        nav.append('<a class="btn btn-ghost btn-sm" href="/__logout">退出</a>')
        if user_auth.feature_allowed(ctx, "fe_browse"):
            nav.insert(1, '<a class="btn btn-ghost btn-sm" href="/browse">文件浏览</a>')
    body = f"""
<div class="topbar">
  <span class="brand">媒体中心<small>{html.escape(str(root))}</small></span>
  {"".join(nav)}
</div>
<div class="content">
  <div class="card">
    <div class="grid">
      {grid_inner}
    </div>
  </div>
</div>"""
    return _html_response(_shell("媒体中心", body))


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
            href = f"/browse?path={quote(str(accum).replace(chr(92), '/'))}"
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
                ops.append(f'<a href="/pdf?path={rel_q}&p=1">阅读</a>')
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
    import shutil
    exe = shutil.which("pdfinfo")
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
    import shutil
    exe = shutil.which("mutool")
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
    import shutil, tempfile
    exe = shutil.which("pdftoppm")
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
    import shutil, tempfile
    exe = shutil.which("mutool")
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

    # p 为 1-based；兼容 pdfpage=0-based
    zb = _query_first(flow, "pdfpage", "mitm_page")
    if zb != "":
        try:
            p_raw = max(1, int(zb) + 1)
        except ValueError:
            p_raw = 1
    else:
        try:
            p_raw = int(_query_first(flow, "p", "page", "mitm_goto") or "1")
        except ValueError:
            p_raw = 1
    p_raw = max(1, min(total, p_raw))
    idx = p_raw - 1

    png, perr = _pdf_page_png(pdf_path, idx, _raster_scale())
    if png is None:
        return _error_page(f"渲染第 {p_raw} 页失败：{perr}")
    b64 = base64.b64encode(png).decode("ascii")

    rel = _rel_of(pdf_path)
    rel_q = quote(rel)
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
    <img alt="第{p_raw}页" src="data:image/png;base64,{b64}" draggable="false">
  </div>
</div>"""
    pager = f"""
<div class="pager" aria-label="翻页">
  <a class="btn btn-sm {'disabled' if p_raw <= 1 else ''}" href="{prev_href}">‹</a>
  <form method="get" action="/pdf">
    <input type="hidden" name="path" value="{html.escape(rel)}">
    <input type="number" min="1" max="{total}" name="p" value="{p_raw}" inputmode="numeric" required>
    <button type="submit" class="btn btn-primary btn-sm">转</button>
  </form>
  <a class="btn btn-sm {'disabled' if p_raw >= total else ''}" href="{next_href}">›</a>
</div>"""
    tools = _viewer_tools_html(include_rotate=True)
    body = f'<style>{css}</style>{topbar}{stage}{pager}{tools}'
    script = f'<script>{_VIEWER_JS}</script>'
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


def _load_subtitle_as_vtt(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "big5"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    if path.suffix.lower() == ".srt":
        text = _srt_to_vtt(text)
    elif not text.lstrip().startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
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
_HLS_SEG_DURATION = 4.0


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


def _trans_key(p: Path) -> str:
    try:
        st = p.stat()
        sig = f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}"
    except OSError:
        sig = str(p)
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
    import shutil
    exe = shutil.which("ffprobe")
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
    import shutil
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

        exe = shutil.which("ffmpeg")
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
        burn_sub_idx = _pick_burn_image_sub(p)
        # 图像字幕（PGS/HDMV）只能烧录到画面里，否则播放器无法显示
        if burn_sub_idx >= 0:
            esc_path = str(p).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
            video_filter = (
                f"scale=-2:'min(720,ih)',"
                f"setpts=PTS-STARTPTS,"
                f"format=yuv420p[vbase];"
                f"[vbase]overlay=enable='1'='1':eof_action=pass[vout];"
                f"movie=filename='{esc_path}':si={burn_sub_idx}[ov]"
            )
            cmd += [
                "-i", str(p),
                "-filter_complex",
                (f"[0:v:0]scale=-2:'min(720,ih)',format=yuv420p[v0];"
                 f"[0:s:{burn_sub_idx}]copy[s0];"
                 f"[v0][s0]overlay[v]"),
                "-map", "[v]", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", "28",
                "-r", "30",
                "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                "-force_key_frames", "expr:gte(t,n_forced*4)",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            ]
        else:
            cmd += [
                "-i", str(p),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-crf", "28",
                "-vf", "scale=-2:'min(720,ih)'",
                "-r", "30",
                "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
                "-force_key_frames", "expr:gte(t,n_forced*4)",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ac", "2",
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

    job = _TRANS_JOBS.get(key)
    if job is None:
        if done.is_file() or (total > 0 and _trans_count_ts(p) >= total):
            try:
                done.touch()
            except OSError:
                pass
            return {"ready": True, "playlist": f"/hls/{key}/index.m3u8", "progress": 100}
        return _trans_start(p, 0)

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

    if ret is None:
        return {"running": True, "playlist": f"/hls/{key}/index.m3u8",
                "progress": pct, "start_seg": start_seg, "current_seg": job["progress_seg"]}

    if ret == 0:
        with _TRANS_LOCK:
            _TRANS_JOBS.pop(key, None)
        if total > 0 and _trans_count_ts(p) >= total:
            try:
                done.touch()
            except OSError:
                pass
            return {"ready": True, "playlist": f"/hls/{key}/index.m3u8", "progress": 100}
        return {"running": True, "playlist": f"/hls/{key}/index.m3u8",
                "progress": pct, "start_seg": start_seg}

    err = ""
    try:
        if proc.stderr:
            err = proc.stderr.read().decode("utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    with _TRANS_LOCK:
        _TRANS_JOBS.pop(key, None)
    return {"error": f"ffmpeg 退出码 {ret}: {err.strip()[:300]}"}


_TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "text", "webvtt", "vtt"}
_IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvb_subtitle", "xsub"}


def _probe_subtitles(p: Path) -> list[dict]:
    """ffprobe 列出 mkv 内所有字幕轨道。"""
    import shutil
    exe = shutil.which("ffprobe")
    if not exe:
        return []
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_streams",
             "-select_streams", "s", str(p)],
            capture_output=True, timeout=15, text=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout or "{}")
    except (ValueError, TypeError):
        return []
    out: list[dict] = []
    for i, s in enumerate(data.get("streams", []) or []):
        codec = (s.get("codec_name") or "").lower()
        if not codec:
            continue
        kind = "text" if codec in _TEXT_SUB_CODECS else (
            "image" if codec in _IMAGE_SUB_CODECS else "unknown")
        tags = s.get("tags") or {}
        out.append({
            "idx": i,
            "codec": codec,
            "kind": kind,
            "lang": (tags.get("language") or "und").lower(),
            "title": tags.get("title") or "",
        })
    return out


def _subtitle_internal_response(flow) -> Response:
    """从 mkv 抽取内封文本字幕并转为 WebVTT 流式返回。"""
    import shutil
    p = _resolve_path_from_query(flow)
    if p is None:
        return _error_page("文件不存在。")
    try:
        idx = int(_query_first(flow, "idx") or "0")
    except ValueError:
        idx = 0
    exe = shutil.which("ffmpeg")
    if not exe:
        return _error_page("未安装 ffmpeg。", status=500)
    try:
        r = subprocess.run(
            [exe, "-y", "-i", str(p),
             "-map", f"0:s:{idx}",
             "-c:s", "webvtt",
             "-f", "webvtt", "-loglevel", "error", "-"],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return _error_page(f"抽取字幕失败: {e}", status=500)
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
        return _error_page(f"ffmpeg 字幕转码失败: {err[:200]}", status=500)
    data = r.stdout or b""
    if not data:
        return _error_page("字幕为空", status=500)
    return Response.make(200, data, {
        "Content-Type": "text/vtt; charset=utf-8",
        "Content-Length": str(len(data)),
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
    p = _resolve_path_from_query(flow)
    if p is None:
        return Response.make(404, b'{"error":"not found"}',
                             {"Content-Type": "application/json"})
    key = _trans_key(p)
    out_dir = _trans_dir(p)
    # 先终止可能仍在跑的转码进程
    job = _TRANS_JOBS.get(key)
    if job is not None:
        proc = job.get("proc")
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            except OSError:
                pass
    with _TRANS_LOCK:
        _TRANS_JOBS.pop(key, None)
    # 清掉整个目录
    import shutil as _sh
    try:
        _sh.rmtree(out_dir, ignore_errors=True)
    except OSError:
        pass
    return Response.make(200, b'{"cleared":true}',
                         {"Content-Type": "application/json; charset=utf-8",
                          "Cache-Control": "no-store",
                          "Access-Control-Allow-Origin": "*"})


def _find_video_by_key(key: str) -> Path | None:
    """通过转码 key 反查源视频路径（用于 seek 时按需启动转码）。"""
    for kk, job in _TRANS_JOBS.items():
        if kk == key:
            # 任务里没存源路径；用 out_dir 反推：父目录就是 cache/hls/key，但拿不到源 path
            pass
    return None


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
        return Response.make(200, data, {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
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
        data = target.read_bytes()
    except OSError:
        return Response.make(500, b"", {"Cache-Control": "no-store"})
    return Response.make(200, data, {
        "Content-Type": "video/mp2t",
        "Content-Length": str(len(data)),
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
    })


def _video_probe_compat(path: Path) -> tuple[bool, str]:
    """用 ffprobe 探测视频编码，返回 (浏览器是否大概率兼容, 诊断文案)。"""
    import shutil
    exe = shutil.which("ffprobe")
    if not exe:
        return True, ""  # 没 ffprobe 就不判断，避免误报
    try:
        r = subprocess.run(
            [exe, "-v", "error", "-print_format", "json", "-show_streams", str(path)],
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
    if vcodec in {"vp9", "av1"} and path.suffix.lower() != ".webm":
        incompat.append(f"视频 {vcodec} (容器不匹配)")
    if acodec in {"dts", "eac3", "truehd"}:
        incompat.append(f"音频 {acodec}")
    if not incompat:
        return True, f"{vcodec} / {pix_fmt} / {acodec}"
    return False, " + ".join(incompat)


def _video_response(flow, path: Path) -> Response:
    rel = _rel_of(path)
    rel_q = _q(path)
    src = f"/file?path={rel_q}"
    # 外挂字幕
    ext_subs = _find_subtitles(path)
    track_tags: list[str] = []
    track_count = 0
    for s in ext_subs:
        lang, label = _subtitle_lang_label(s)
        default = " default" if track_count == 0 else ""
        track_tags.append(
            f'<track kind="subtitles" label="{html.escape(label)}" '
            f'srclang="{html.escape(lang)}" src="/subtitle?path={_q(s)}"{default}>'
        )
        track_count += 1
    # 内封文本字幕（subrip / ass / mov_text 等）→ 转成 VTT 提供
    int_subs = _probe_subtitles(path)
    text_idx = 0
    for sub in int_subs:
        if sub.get("kind") != "text":
            continue
        title = sub.get("title") or sub.get("lang") or f"内封 {text_idx+1}"
        lang = sub.get("lang") or "und"
        default = " default" if track_count == 0 else ""
        track_tags.append(
            f'<track kind="subtitles" label="{html.escape(title)} (内封)" '
            f'srclang="{html.escape(lang)}" '
            f'src="/subtitle_internal?path={rel_q}&amp;idx={text_idx}"{default}>'
        )
        track_count += 1
        text_idx += 1
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
  var TRANS_PATH = {json.dumps(rel)};
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
        maxBufferLength: 90,
        maxMaxBufferLength: 240,
        maxBufferSize: 200 * 1024 * 1024,
        backBufferLength: 30,
        startFragPrefetch: true,
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

  function clearTransCache(useBeacon){{
    var url = '/video_trans_clear?path=' + encodeURIComponent(TRANS_PATH);
    try {{
      if (useBeacon && navigator.sendBeacon){{
        navigator.sendBeacon(url, '');
        return;
      }}
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
          setProgress(100, '已全部转完');
          setTimeout(hideOverlay, 800);
          return;
        }}
        setProgress(s.progress || 0, '已转码 ' + (s.progress || 0) + '%（边转边播，可直接观看）');
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
    startTranscodeAndPlay();
    if (v) {{
      v.addEventListener('ended', function(){{ clearTransCache(false); }});
      v.addEventListener('seeking', scheduleJump);
    }}
  }} else {{
    loadDirect(ORIGINAL_SRC, ORIGINAL_MIME);
  }}
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


def _collect_music_tracks(root_music: Path) -> list[dict]:
    if not root_music.is_dir():
        return []
    tracks: list[dict] = []
    try:
        for f in sorted(root_music.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in _AUDIO_EXTS:
                continue
            cover = _find_cover(f)
            rel = _rel_of(f)
            # album：父目录相对名（去掉 _时间戳 后缀更好看）
            album = f.parent.name
            album = re.sub(r"_\d{8,}$", "", album)
            # artist：从目录名 "Artist - Album" 取前半
            artist = ""
            top = f.parent
            # 回溯到音乐根下的直属目录当作艺术家
            while top.parent != root_music and top.parent != _share_root():
                top = top.parent
                if top == top.parent:
                    break
            if " - " in top.name:
                artist = top.name.split(" - ", 1)[0]
            tracks.append({
                "name": f.stem,
                "artist": artist,
                "album": album,
                "src": f"/file?path={_q(f)}",
                "cover": f"/file?path={_q(cover)}" if cover else "",
                "rel": rel,
            })
    except OSError:
        return []
    return tracks


def _music_response(flow) -> Response:
    root = _share_root()
    music_dir = root / _DIR_MUSIC
    tracks = _collect_music_tracks(music_dir)
    tracks_json = _json_embed(tracks)

    css = r"""
.app:has(.mitm-music-page){min-height:100dvh;max-height:100dvh;overflow:hidden;box-sizing:border-box}
@supports not selector(:has(*)){.app{min-height:100dvh}}
.content.mitm-music-page{display:flex;flex-direction:column;min-height:0;flex:1 1 auto;padding-top:8px;padding-bottom:10px}
.mitm-music-page{flex:1 1 auto;min-height:0;max-height:calc(100dvh - 50px);overflow:hidden;box-sizing:border-box;display:flex;flex-direction:column}
.music-layout{flex:1;min-height:0;max-width:1280px;margin:0 auto;width:100%;display:flex;flex-direction:column;overflow:hidden}
.player{display:flex;flex-direction:row;align-items:stretch;gap:0;flex:1;min-height:0;
  border-radius:14px;overflow:hidden;border:1px solid var(--line);background:var(--card)}
.player-left{flex:0 0 clamp(256px,32vw,340px);min-width:0;width:clamp(256px,32vw,340px);
  max-width:100%;height:100%;min-height:0;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-start;align-items:center;
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
  .player-left{flex:0 0 auto;width:100%;height:auto}
  .player-right{min-height:min(50dvh,480px);max-height:55dvh;border-left:none;border-top:1px solid var(--line)}
}
.np-card{padding:0;overflow:visible;border:none;box-shadow:none;background:transparent;margin:0}
.np-inner{padding:18px;display:flex;flex-direction:column;gap:12px;align-items:center;text-align:center}
.np-cover{width:min(72vw,260px);aspect-ratio:1/1;border-radius:20px;background:#222 center/cover;box-shadow:0 10px 36px rgba(0,0,0,.55)}
.np-title{font-size:1.1rem;font-weight:700;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.np-meta{color:var(--muted);font-size:.9rem;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.progress{width:100%;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.8rem}
.progress input[type=range]{flex:1;accent-color:#7cc1ff}
.controls{display:flex;align-items:center;justify-content:center;gap:6px;flex-wrap:wrap}
.controls button{min-width:48px;min-height:44px;border-radius:12px;border:1px solid var(--line);background:rgba(255,255,255,.08);color:var(--fg);font-weight:700;font-size:1rem}
.controls .play{min-width:64px;font-size:1.3rem;background:linear-gradient(180deg,#4f8fff,#3a7ae8);border-color:transparent;color:#fff}
.controls .mode.on{background:rgba(124,193,255,.2);color:#9dd1ff;border-color:rgba(124,193,255,.3)}
.volume{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:.8rem}
.volume input[type=range]{accent-color:#7cc1ff;flex:1}
.track{display:flex;align-items:center;gap:10px;padding:8px 10px;border-bottom:1px solid var(--line);cursor:pointer}
.track:hover{background:rgba(255,255,255,.04)}
.track.active{background:rgba(124,193,255,.12);color:#9dd1ff}
.track .idx{min-width:32px;color:var(--muted);font-size:.8rem;text-align:right}
.track .info{flex:1;min-width:0}
.track .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.track .meta{color:var(--muted);font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
        <div class="progress">
          <span id="cur-time">0:00</span>
          <input type="range" id="seek" min="0" max="1000" value="0">
          <span id="dur-time">0:00</span>
        </div>
        <div class="controls">
          <button id="btn-mode-shuffle" class="mode" title="随机">🔀</button>
          <button id="btn-prev">⏮</button>
          <button id="btn-play" class="play">▶</button>
          <button id="btn-next">⏭</button>
          <button id="btn-mode-repeat" class="mode" title="循环">🔁</button>
        </div>
        <div class="volume">
          <span>🔊</span><input type="range" id="vol" min="0" max="100" value="80">
        </div>
        </div>
      </div>
    </aside>
    <div class="player-right">
      <div class="playlist-head">播放列表</div>
      <div class="playlist-scroll">
        <div id="playlist"></div>
      </div>
    </div>
  </div>
</div>
<audio id="audio" preload="metadata"></audio>
<script id="mitm-tracks" type="application/json">{tracks_json}</script>
<script>
(function(){{
  var tracks = JSON.parse(document.getElementById('mitm-tracks').textContent||'[]');
  var audio=document.getElementById('audio');
  var elTitle=document.getElementById('np-title'), elMeta=document.getElementById('np-meta');
  var elCover=document.getElementById('np-cover'), elPlaylist=document.getElementById('playlist');
  var elSeek=document.getElementById('seek'), elCur=document.getElementById('cur-time'), elDur=document.getElementById('dur-time');
  var elPlay=document.getElementById('btn-play'), elPrev=document.getElementById('btn-prev'), elNext=document.getElementById('btn-next');
  var elShuffle=document.getElementById('btn-mode-shuffle'), elRepeat=document.getElementById('btn-mode-repeat');
  var elVol=document.getElementById('vol');

  // 状态
  var STATE_KEY='mitm_music_state_v1';
  function loadState(){{
    try{{return JSON.parse(localStorage.getItem(STATE_KEY)||'{{}}')||{{}};}}catch(e){{return {{}};}}
  }}
  function saveState(){{
    try{{localStorage.setItem(STATE_KEY, JSON.stringify({{i:curIdx,pos:audio.currentTime||0,vol:audio.volume,shuffle:shuffle,repeat:repeatMode}}));}}catch(e){{}}
  }}
  var st=loadState();
  var curIdx = (typeof st.i==='number' && st.i>=0 && st.i<tracks.length) ? st.i : 0;
  var shuffle = !!st.shuffle;
  var repeatMode = st.repeat||'all'; // off | all | one
  audio.volume = (typeof st.vol==='number') ? st.vol : 0.8;
  elVol.value = Math.round(audio.volume*100);

  function updateModeButtons(){{
    if(shuffle) elShuffle.classList.add('on'); else elShuffle.classList.remove('on');
    elRepeat.classList.remove('on'); elRepeat.textContent='🔁';
    if(repeatMode==='all'){{elRepeat.classList.add('on'); elRepeat.textContent='🔁';}}
    else if(repeatMode==='one'){{elRepeat.classList.add('on'); elRepeat.textContent='🔂';}}
  }}
  updateModeButtons();

  function fmtTime(s){{ if(!isFinite(s)) return '0:00'; var m=Math.floor(s/60), x=Math.floor(s%60); return m+':'+(x<10?'0':'')+x; }}

  function renderPlaylist(){{
    var html='';
    tracks.forEach(function(t,i){{
      var meta=[t.artist,t.album].filter(Boolean).join(' · ');
      html += '<div class="track'+(i===curIdx?' active':'')+'" data-i="'+i+'">'
           +  '<div class="idx">'+(i+1)+'</div>'
           +  '<div class="info"><div class="name">'+escape(t.name)+'</div>'
           +  '<div class="meta">'+escape(meta||' ')+'</div></div>'
           +  '</div>';
    }});
    elPlaylist.innerHTML = html;
    elPlaylist.querySelectorAll('.track').forEach(function(el){{
      el.addEventListener('click', function(){{ playIndex(parseInt(el.getAttribute('data-i'))); }});
    }});
  }}
  function escape(s){{ return (s||'').replace(/[&<>"']/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c];}}); }}

  function loadTrack(i, autoplay){{
    if (!tracks.length) return;
    if (i<0) i=tracks.length-1;
    if (i>=tracks.length) i=0;
    curIdx = i;
    var t = tracks[i];
    audio.src = t.src;
    elTitle.textContent = t.name;
    elMeta.textContent = [t.artist,t.album].filter(Boolean).join(' · ');
    elCover.style.backgroundImage = t.cover ? ('url('+t.cover+')') : 'linear-gradient(145deg,#27314a,#15192a)';
    document.title = t.name + ' - 音乐';
    renderPlaylist();
    // MediaSession
    if ('mediaSession' in navigator){{
      try{{
        navigator.mediaSession.metadata = new MediaMetadata({{title:t.name, artist:t.artist||'', album:t.album||'', artwork: t.cover?[{{src:t.cover, sizes:'512x512', type:'image/jpeg'}}]:[] }});
        navigator.mediaSession.setActionHandler('previoustrack', prev);
        navigator.mediaSession.setActionHandler('nexttrack', next);
        navigator.mediaSession.setActionHandler('play', function(){{ audio.play(); }});
        navigator.mediaSession.setActionHandler('pause', function(){{ audio.pause(); }});
      }}catch(e){{}}
    }}
    if (autoplay) audio.play().catch(function(){{}});
    saveState();
  }}

  function playIndex(i){{ loadTrack(i, true); }}
  function next(){{
    if (!tracks.length) return;
    var i;
    if (shuffle){{
      if (tracks.length<=1) i=curIdx;
      else {{ do {{ i = Math.floor(Math.random()*tracks.length); }} while(i===curIdx); }}
    }} else {{
      i = curIdx+1;
      if (i>=tracks.length){{
        if (repeatMode==='all') i=0;
        else {{ audio.pause(); return; }}
      }}
    }}
    loadTrack(i, true);
  }}
  function prev(){{
    if (!tracks.length) return;
    if (audio.currentTime>3){{ audio.currentTime=0; return; }}
    var i = curIdx-1;
    if (i<0) i=tracks.length-1;
    loadTrack(i, true);
  }}

  elPlay.addEventListener('click', function(){{ if(audio.paused) audio.play(); else audio.pause(); }});
  elPrev.addEventListener('click', prev);
  elNext.addEventListener('click', next);
  elShuffle.addEventListener('click', function(){{ shuffle=!shuffle; updateModeButtons(); saveState(); }});
  elRepeat.addEventListener('click', function(){{
    repeatMode = repeatMode==='off'?'all':(repeatMode==='all'?'one':'off');
    updateModeButtons(); saveState();
  }});
  elVol.addEventListener('input', function(){{ audio.volume = parseInt(elVol.value)/100; saveState(); }});
  elSeek.addEventListener('input', function(){{
    if (isFinite(audio.duration)) audio.currentTime = (parseInt(elSeek.value)/1000) * audio.duration;
  }});

  audio.addEventListener('play', function(){{ elPlay.textContent='⏸'; }});
  audio.addEventListener('pause', function(){{ elPlay.textContent='▶'; }});
  audio.addEventListener('timeupdate', function(){{
    elCur.textContent = fmtTime(audio.currentTime);
    if (isFinite(audio.duration)){{
      elDur.textContent = fmtTime(audio.duration);
      elSeek.value = Math.round((audio.currentTime/audio.duration)*1000);
    }}
    if (Math.floor(audio.currentTime) % 5 === 0) saveState();
  }});
  audio.addEventListener('ended', function(){{
    if (repeatMode==='one'){{ audio.currentTime=0; audio.play().catch(function(){{}}); return; }}
    next();
  }});

  renderPlaylist();
  if (tracks.length){{
    loadTrack(curIdx, false);
    if (typeof st.pos === 'number' && st.pos>0){{
      audio.addEventListener('loadedmetadata', function(){{ try{{audio.currentTime=st.pos;}}catch(e){{}} }}, {{once:true}});
    }}
  }} else {{
    elTitle.textContent='（音乐目录下没有音频文件）';
    elPlaylist.innerHTML='<div class="empty">请将音频放入 '+escape('{html.escape(_DIR_MUSIC)}')+'/ 目录</div>';
  }}
}})();
</script>"""
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
  <a class="btn btn-ghost btn-sm" href="/browse?path={quote(rel_up)}">查看目录</a>
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
        if path == "/upload":
            if method == "POST":
                return _upload_post_response(flow)
            return _upload_get_response(flow)
        if path == "/music":
            return _music_response(flow)
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
        if path == "/video":
            p = _resolve_path_from_query(flow)
            if p is None:
                return _error_page("未找到视频/音频。")
            return _video_response(flow, p)
        if path == "/video_trans_status":
            return _video_trans_status_response(flow)
        if path == "/video_trans_clear":
            return _video_trans_clear_response(flow)
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
