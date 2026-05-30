# -*- coding: utf-8 -*-
"""多用户与会话：线程安全 JSON 存储、PBKDF2 口令、内存会话（多并发下需单 mitmdump 进程；多进程需共享存储）。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_SESSION_COOKIE = "mitmsess"
_DEFAULT_ADMIN = "admin"
# 首次初始化时的管理员口令（与 redirect_addon 文档一致，可被环境变量覆盖）
_DEFAULT_ADMIN_PASSWORD = (os.environ.get("MITM_BOOTSTRAP_PASSWORD", "") or "change-me-please").strip()
# 会话：0=不设置 Max-Age（关浏览器/进程后多需重登，取决于 WebView）
_SESSION_MAX_AGE = int((os.environ.get("MITM_SESSION_MAX_AGE", "") or "0") or 0)  # 0 表示仅会话 Cookie
_PBKDF2_ITERS = 200_000
# 单设备登录：1=登录新设备时自动踢掉同账号其它设备；0=允许多端
_SINGLE_DEVICE = (os.environ.get("MITM_SINGLE_DEVICE", "1") or "1").strip().lower() not in ("0", "false", "no", "off", "")

_LOCK = threading.RLock()
# sid -> {username, created, ip, ua, last_seen}
_SESSIONS: dict[str, dict[str, Any]] = {}


def _sessions_path() -> Path:
    return _mitm_dir() / "mitm_sessions.json"


def _load_sessions_from_disk() -> None:
    """启动时加载磁盘会话，避免重启把所有用户踢下线。"""
    p = _sessions_path()
    if not p.is_file():
        return
    try:
        with _LOCK:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                # 兼容历史空文件
                sess = data.get("sessions") if isinstance(data.get("sessions"), dict) else data
                for sid, meta in (sess or {}).items():
                    if isinstance(sid, str) and isinstance(meta, dict):
                        _SESSIONS[sid] = meta
    except (OSError, json.JSONDecodeError, ValueError):
        pass


def _persist_sessions_locked() -> None:
    """必须在已持锁的上下文里调用：把当前 _SESSIONS 原子写盘。"""
    p = _sessions_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump({"version": 1, "sessions": _SESSIONS}, f, ensure_ascii=False)
        tmp.replace(p)
    except OSError:
        pass

# 各功能是否可见/可用（管理员默认全开）
FE_KEYS = (
    "fe_home", "fe_pdf", "fe_video", "fe_music", "fe_upload", "fe_browse",
    "fe_upl", "fe_private", "fe_image", "fe_text", "fe_browser", "fe_chat",
)
DEFAULT_FEATURES = {k: True for k in FE_KEYS}
DEFAULT_FEATURES["fe_upl"] = True
DEFAULT_FEATURES["fe_private"] = False
DEFAULT_FEATURES["fe_browser"] = False  # 浏览器直跳默认关闭：会让 host 进白名单透传，谨慎授权
DEFAULT_FEATURES["fe_chat"] = True  # 即时通讯默认开启


@dataclass
class UserCtx:
    username: str
    role: str  # "admin" | "user"
    banned: bool
    features: dict[str, bool] = field(default_factory=dict)
    is_admin: bool = False
    can_private: bool = False  # 访问 private 目录


def _mitm_dir() -> Path:
    p = (os.environ.get("MITM_DATA_DIR", "") or "").strip()
    if p:
        return Path(p).expanduser().resolve()
    return Path(__file__).resolve().parent


def _users_path() -> Path:
    env = (os.environ.get("MITM_USERS_FILE", "") or "").strip()
    if env:
        return Path(env).expanduser()
    return _mitm_dir() / "mitm_users.json"


# ---------------------------------------------------------------------------
# 文件存储
# ---------------------------------------------------------------------------
def _default_store() -> dict[str, Any]:
    return {
        "version": 1,
        "users": {},
        "private_access_users": [],
    }


def _hash_password(password: str, salt: bytes) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERS, 32)
    return dk.hex()


def _read_store_unlocked() -> dict[str, Any]:
    p = _users_path()
    if not p.is_file():
        return _default_store()
    try:
        raw = p.read_text(encoding="utf-8")
        d = json.loads(raw)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _default_store()
    if not isinstance(d, dict) or "users" not in d:
        return _default_store()
    d.setdefault("private_access_users", [])
    d.setdefault("version", 1)
    return d


def _write_store_unlocked(data: dict[str, Any]) -> None:
    p = _users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(p)


def _validate_username(name: str) -> str | None:
    n = (name or "").strip()
    if not n or len(n) > 64:
        return "用户名 1~64 字符"
    if not re.match(r"^[a-zA-Z0-9_\-\u4e00-\u9fff]+$", n):
        return "仅允许字母数字下划线、中文、短横线"
    return None


# ---------------------------------------------------------------------------
# 引导：无用户文件时创建 admin
# ---------------------------------------------------------------------------
def _bootstrap_if_empty() -> None:
    d = _read_store_unlocked()
    if d.get("users"):
        return
    salt = os.urandom(16)
    hpw = _hash_password(_DEFAULT_ADMIN_PASSWORD, salt)
    d["users"][_DEFAULT_ADMIN] = {
        "role": "admin",
        "password_hash": hpw,
        "salt": base64.b64encode(salt).decode("ascii"),
        "banned": False,
        "features": {**DEFAULT_FEATURES, "fe_private": True, "fe_upl": True},
    }
    d["private_access_users"] = [_DEFAULT_ADMIN]
    _write_store_unlocked(d)


_SESSIONS_LOADED = False


def _ensure_bootstrap() -> None:
    global _SESSIONS_LOADED
    with _LOCK:
        _bootstrap_if_empty()
        if not _SESSIONS_LOADED:
            _load_sessions_from_disk()
            _SESSIONS_LOADED = True


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
def _get_cookie_value(flow) -> str:
    try:
        cj = flow.request.cookies
        if not cj:
            return ""
        v = cj.get(_SESSION_COOKIE)
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return str(v[0]) if v else ""
        return str(v)
    except (AttributeError, KeyError, TypeError, ValueError):
        return ""


def _new_sid() -> str:
    return secrets.token_hex(32)


def _invalidate_user_sessions(username: str) -> None:
    to_del = [sid for sid, meta in _SESSIONS.items() if (meta or {}).get("username") == username]
    for sid in to_del:
        _SESSIONS.pop(sid, None)


def get_user_ctx_from_flow(flow) -> UserCtx | None:
    """从请求取会话并解析为 UserCtx；未登录或封禁/删除用户时返回 None。"""
    _ensure_bootstrap()
    sid = _get_cookie_value(flow)
    if not sid:
        return None
    with _LOCK:
        meta = _SESSIONS.get(sid)
        if not meta:
            return None
        un = meta.get("username")
        if not un:
            return None
        store = _read_store_unlocked()
        rec = (store.get("users") or {}).get(un)
        if not rec or rec.get("banned"):
            _SESSIONS.pop(sid, None)
            return None
    return _user_record_to_ctx(un, rec, store)


def _user_record_to_ctx(un: str, rec: dict, store: dict) -> UserCtx:
    role = (rec.get("role") or "user").lower()
    is_ad = role == "admin"
    fe = {**DEFAULT_FEATURES, **(rec.get("features") or {})}
    for k in FE_KEYS:
        fe.setdefault(k, DEFAULT_FEATURES.get(k, True))
    private_list: list = store.get("private_access_users") or []
    # 私密目录：管理员或名单内用户
    can_priv = is_ad or (un in private_list)
    return UserCtx(
        username=un,
        role=role,
        banned=bool(rec.get("banned")),
        features=fe,
        is_admin=is_ad,
        can_private=bool(can_priv),
    )


def login_user(username: str, password: str, *, ip: str = "", ua: str = "") -> tuple[str | None, str | None]:
    """成功返回 (session_id, None)，失败 (None, 错误信息)。
    单设备模式下：登录后会清掉该用户的其它会话。
    """
    _ensure_bootstrap()
    err = _validate_username(username)
    if err:
        return None, err
    with _LOCK:
        store = _read_store_unlocked()
        rec = (store.get("users") or {}).get(username)
        if not rec:
            return None, "用户名或密码错误"
        if rec.get("banned"):
            return None, "该账号已封禁"
        salt = base64.b64decode((rec.get("salt") or "").encode("ascii"), validate=True)
        if _hash_password(password, salt) != (rec.get("password_hash") or ""):
            return None, "用户名或密码错误"
        if _SINGLE_DEVICE:
            # 踢掉同账户其它会话
            for old_sid in [s for s, m in _SESSIONS.items() if (m or {}).get("username") == username]:
                _SESSIONS.pop(old_sid, None)
        sid = _new_sid()
        _SESSIONS[sid] = {
            "username": username,
            "created": time.time(),
            "last_seen": time.time(),
            "ip": ip or "",
            "ua": ua or "",
        }
        _persist_sessions_locked()
        return sid, None


def logout_and_clear(sid: str) -> None:
    with _LOCK:
        _SESSIONS.pop(sid, None)
        _persist_sessions_locked()


def logout_from_flow(flow) -> None:
    """根据 Cookie 清除当前会话（幂等）。"""
    sid = _get_cookie_value(flow)
    if sid:
        logout_and_clear(sid)


def kick_user(username: str) -> int:
    """踢掉某用户全部会话；返回被踢的会话数。"""
    n = 0
    with _LOCK:
        for sid in [s for s, m in _SESSIONS.items() if (m or {}).get("username") == username]:
            _SESSIONS.pop(sid, None)
            n += 1
        if n:
            _persist_sessions_locked()
    return n


def list_user_sessions(username: str | None = None) -> list[dict]:
    """列出会话；username 为空返回全部。"""
    out: list[dict] = []
    with _LOCK:
        for sid, m in _SESSIONS.items():
            if not m:
                continue
            un = m.get("username") or ""
            if username and un != username:
                continue
            out.append({
                "sid": sid,
                "username": un,
                "created": m.get("created") or 0,
                "last_seen": m.get("last_seen") or m.get("created") or 0,
                "ip": m.get("ip") or "",
                "ua": (m.get("ua") or "")[:160],
            })
    out.sort(key=lambda x: x.get("last_seen") or 0, reverse=True)
    return out


def touch_session(flow) -> None:
    """请求来时调用：刷新该会话的 last_seen / ip / ua（用于在线探针）。"""
    sid = _get_cookie_value(flow)
    if not sid:
        return
    try:
        peer = flow.client_conn.peername[0] if getattr(flow, "client_conn", None) else ""
    except (AttributeError, IndexError, TypeError):
        peer = ""
    try:
        ua = (flow.request.headers.get("User-Agent") or "")[:200]
    except (AttributeError, TypeError):
        ua = ""
    with _LOCK:
        m = _SESSIONS.get(sid)
        if not m:
            return
        m["last_seen"] = time.time()
        if peer:
            m["ip"] = peer
        if ua:
            m["ua"] = ua


def session_cookie_name() -> str:
    return _SESSION_COOKIE


def set_session_headers(session_id: str) -> str:
    """Set-Cookie 行内容（无 Set-Cookie: 前缀）。"""
    parts = [f"{_SESSION_COOKIE}={session_id}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if _SESSION_MAX_AGE and _SESSION_MAX_AGE > 0:
        parts.append(f"Max-Age={_SESSION_MAX_AGE}")
    return "; ".join(parts)


def clear_session_cookie() -> str:
    return f"{_SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


# ---------------------------------------------------------------------------
# 授权检查
# ---------------------------------------------------------------------------
def feature_allowed(ctx: UserCtx, key: str) -> bool:
    if ctx.banned:
        return False
    if ctx.is_admin:
        return True
    return bool((ctx.features or {}).get(key, DEFAULT_FEATURES.get(key, True)))


def can_access_private_path(ctx: UserCtx) -> bool:
    return ctx.is_admin or ctx.can_private


def can_browse_private_dir(ctx: UserCtx) -> bool:
    """能进 private 且功能开关允许（管理员不限）。"""
    if not ctx or ctx.banned:
        return False
    if ctx.is_admin:
        return True
    return bool(ctx.can_private) and bool(feature_allowed(ctx, "fe_private"))


def can_browse_upl_dir(ctx: UserCtx) -> bool:
    if not ctx or ctx.banned:
        return False
    if ctx.is_admin:
        return True
    return feature_allowed(ctx, "fe_upl")


def is_admin_name(username: str) -> bool:
    with _LOCK:
        store = _read_store_unlocked()
        r = (store.get("users") or {}).get(username) or {}
        return (r.get("role") or "").lower() == "admin"


def list_usernames(*, include_banned: bool = False) -> list[dict]:
    """列出所有用户（供通讯录用）。返回 [{username, role, banned}, ...]，按用户名排序。"""
    with _LOCK:
        _ensure_bootstrap()
        store = _read_store_unlocked()
        out: list[dict] = []
        for un, rec in (store.get("users") or {}).items():
            if not isinstance(rec, dict):
                continue
            banned = bool(rec.get("banned"))
            if banned and not include_banned:
                continue
            out.append({
                "username": un,
                "role": (rec.get("role") or "user").lower(),
                "banned": banned,
            })
    out.sort(key=lambda x: x["username"].lower())
    return out


def user_exists(username: str) -> bool:
    with _LOCK:
        store = _read_store_unlocked()
        return username in (store.get("users") or {})


# ---------------------------------------------------------------------------
# 管理员：CRUD
# ---------------------------------------------------------------------------
def admin_list_actions() -> dict[str, Any]:
    with _LOCK:
        _ensure_bootstrap()
        return _read_store_unlocked()


def admin_set_private_list(usernames: list[str]) -> tuple[bool, str]:
    with _LOCK:
        store = _read_store_unlocked()
        uall = set((store.get("users") or {}).keys())
        clean = [u for u in usernames if u in uall]
        store["private_access_users"] = clean
        _write_store_unlocked(store)
        return True, "已更新「私密」访问名单"

def admin_update_user(
    target: str,
    *,
    role: str | None = None,
    password: str | None = None,
    banned: bool | None = None,
    features: dict[str, bool] | None = None,
) -> tuple[bool, str]:
    with _LOCK:
        store = _read_store_unlocked()
        users = store.setdefault("users", {})
        if target not in users:
            return False, "用户不存在"
        rec = users[target]
        if role is not None:
            if role not in ("admin", "user"):
                return False, "角色非法"
            n_admins = sum(1 for u, m in users.items() if (m.get("role") or "") == "admin" and not m.get("banned"))
            if (rec.get("role") or "") == "admin" and role == "user" and n_admins <= 1:
                return False, "至少保留一个未封禁的管理员"
            rec["role"] = role
        if banned is not None:
            if banned and (rec.get("role") or "") == "admin":
                others_adm = [
                    u for u, m in users.items()
                    if u != target and (m.get("role") or "") == "admin" and not m.get("banned")
                ]
                if not others_adm:
                    return False, "不能封禁最后一个未封禁的管理员"
            rec["banned"] = bool(banned)
        if password:
            if len(password) < 4:
                return False, "密码过短"
            salt = os.urandom(16)
            rec["salt"] = base64.b64encode(salt).decode("ascii")
            rec["password_hash"] = _hash_password(password, salt)
            _invalidate_user_sessions(target)
        if features is not None:
            fe = {**DEFAULT_FEATURES, **(rec.get("features") or {})}
            for k, v in features.items():
                if k in FE_KEYS:
                    fe[k] = bool(v)
            rec["features"] = fe
        _write_store_unlocked(store)
        if banned is not None or role is not None or features is not None or password:
            _invalidate_user_sessions(target)
        return True, "已保存"

def admin_create_user(
    username: str,
    password: str,
    role: str = "user",
    features: dict[str, bool] | None = None,
) -> tuple[bool, str]:
    err = _validate_username(username)
    if err:
        return False, err
    if len(password) < 4:
        return False, "密码至少 4 位"
    if role not in ("admin", "user"):
        return False, "角色非法"
    with _LOCK:
        store = _read_store_unlocked()
        users = store.setdefault("users", {})
        if username in users:
            return False, "用户名已存在"
        salt = os.urandom(16)
        fe = {**DEFAULT_FEATURES, **(features or {})}
        users[username] = {
            "role": role,
            "password_hash": _hash_password(password, salt),
            "salt": base64.b64encode(salt).decode("ascii"),
            "banned": False,
            "features": fe,
        }
        _write_store_unlocked(store)
    return True, "已创建用户"

def admin_delete_user(target: str, operator: str) -> tuple[bool, str]:
    with _LOCK:
        store = _read_store_unlocked()
        users = store.get("users") or {}
        if target not in users:
            return False, "用户不存在"
        n_adm = [u for u, m in users.items() if (m.get("role") or "") == "admin" and not m.get("banned")]
        if (users[target].get("role") or "") == "admin" and len(n_adm) <= 1:
            return False, "不能删除最后一个管理员"
        if target == operator:
            return False, "不能删除当前登录用户"
        del users[target]
        pl = [x for x in (store.get("private_access_users") or []) if x != target]
        store["private_access_users"] = pl
        _write_store_unlocked(store)
        _invalidate_user_sessions(target)
    return True, "已删除用户"
