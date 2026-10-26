"""单管理员密码与可撤销会话，不在数据库保存明文密码或令牌。"""

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from data_service.config import Settings
from data_service.db import connect

LOGIN_WINDOW_SECONDS = 60
LOGIN_ATTEMPTS = 10
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
# 迁移使用706223001，同步worker使用706223002；初始化不能等待常驻worker锁。
ADMIN_INITIALIZATION_LOCK = 706223003


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str | None) -> bool:
    """未知账号也执行同等成本哈希，损坏的哈希不能降低鉴权要求。"""
    salt = b"mds-dummy-salt-v1"
    expected = bytes(32)
    usable = False
    if encoded:
        try:
            algorithm, n_value, r_value, p_value, salt_value, digest_value = encoded.split("$")
            if (algorithm, n_value, r_value, p_value) == ("scrypt", "16384", "8", "1"):
                candidate_salt = base64.b64decode(salt_value, validate=True)
                candidate_digest = base64.b64decode(digest_value, validate=True)
                if len(candidate_salt) == 16 and len(candidate_digest) == 32:
                    salt, expected, usable = candidate_salt, candidate_digest, True
        except (ValueError, TypeError):
            usable = False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, expected) and usable


def initialize_admin(settings: Settings) -> bool:
    """只创建第一个管理员；重复初始化不会更改密码或增加账号。"""
    with connect(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (ADMIN_INITIALIZATION_LOCK,))
        if connection.execute("SELECT 1 FROM auth_admin WHERE id = 1").fetchone():
            return False
        if not settings.admin_password:
            raise ValueError("首次初始化必须设置至少12字符的管理员密码")
        connection.execute(
            "INSERT INTO auth_admin(id, username, password_hash) VALUES (1, %s, %s)",
            (settings.admin_username, hash_password(settings.admin_password)),
        )
    return True


def _reserve_login_attempt(settings: Settings, client_address: str) -> None:
    # 不信任客户端自己传入的转发头；同一个反向代理出口共享限额时会更严格。
    bucket_key = hashlib.sha256(client_address.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc)
    with connect(settings) as connection:
        connection.execute(
            "DELETE FROM auth_login_limits WHERE window_start < %s",
            (now - timedelta(days=1),),
        )
        row = connection.execute(
            """INSERT INTO auth_login_limits(bucket_key, window_start, attempts) VALUES (%s, %s, 1)
               ON CONFLICT(bucket_key) DO UPDATE SET
                 window_start = CASE WHEN auth_login_limits.window_start <= %s
                                     THEN EXCLUDED.window_start ELSE auth_login_limits.window_start END,
                 attempts = CASE WHEN auth_login_limits.window_start <= %s THEN 1
                                 ELSE auth_login_limits.attempts + 1 END
               RETURNING attempts""",
            (bucket_key, now, now - timedelta(seconds=LOGIN_WINDOW_SECONDS),
             now - timedelta(seconds=LOGIN_WINDOW_SECONDS)),
        ).fetchone()
    if row["attempts"] > LOGIN_ATTEMPTS:
        raise AuthError(429, "LOGIN_RATE_LIMIT", "登录尝试过于频繁，请稍后重试")


def login(settings: Settings, username: str, password: str, client_address: str) -> str:
    _reserve_login_attempt(settings, client_address)
    with connect(settings) as connection:
        # 与本地密码重置使用同一行锁，避免旧密码校验后又在重置之后发出新会话。
        admin = connection.execute(
            "SELECT id, username, password_hash FROM auth_admin WHERE id = 1 FOR UPDATE"
        ).fetchone()
        verified = verify_password(password, admin["password_hash"] if admin else None)
        if not admin or not verified or not hmac.compare_digest(username.encode(), admin["username"].encode()):
            raise AuthError(401, "LOGIN_FAILED", "账号或密码不正确")
        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_hours)
        connection.execute("DELETE FROM auth_sessions WHERE expires_at <= CURRENT_TIMESTAMP")
        connection.execute(
            "INSERT INTO auth_sessions(token_hash, admin_id, expires_at) VALUES (%s, %s, %s)",
            (hashlib.sha256(token.encode()).hexdigest(), admin["id"], expires_at),
        )
    return token


def authenticate(settings: Settings, authorization: str | None) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError(401, "SESSION_REQUIRED", "请先登录")
    token = authorization[7:]
    if not TOKEN_PATTERN.fullmatch(token):
        raise AuthError(401, "SESSION_INVALID", "登录已失效，请重新登录")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect(settings) as connection:
        admin = connection.execute(
            """SELECT a.id, a.username FROM auth_sessions s JOIN auth_admin a ON a.id = s.admin_id
               WHERE s.token_hash = %s AND s.expires_at > CURRENT_TIMESTAMP""",
            (token_hash,),
        ).fetchone()
    if not admin:
        raise AuthError(401, "SESSION_INVALID", "登录已失效，请重新登录")
    return {"id": admin["id"], "username": admin["username"], "token_hash": token_hash}


def logout(settings: Settings, token_hash: str) -> None:
    with connect(settings) as connection:
        connection.execute("DELETE FROM auth_sessions WHERE token_hash = %s", (token_hash,))


def reset_password(settings: Settings) -> None:
    """仅本地显式命令可重置；更新密码和撤销全部会话必须一起提交。"""
    if not settings.admin_password:
        raise ValueError("重置密码必须通过环境配置提供新密码")
    with connect(settings) as connection:
        admin = connection.execute("SELECT id FROM auth_admin WHERE id = 1 FOR UPDATE").fetchone()
        if not admin:
            raise ValueError("管理员尚未初始化")
        connection.execute(
            "UPDATE auth_admin SET password_hash=%s WHERE id=1", (hash_password(settings.admin_password),),
        )
        connection.execute("DELETE FROM auth_sessions")
