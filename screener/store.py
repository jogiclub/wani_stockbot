"""계정·설정 영속 저장소 (SQLite).

목적
  - 대시보드 **아이디/비밀번호 로그인**
  - 설정 창에 넣은 값(토스 API 키·텔레그램·주문 한도 …)을 **서버에 저장**해
    브라우저를 닫거나 서버를 재시작해도 그대로 복구한다.

보안 설계 — 약화시키지 말 것
  - 비밀번호는 `scrypt` 해시로만 저장한다 (원문·복호화 불가).
  - 설정에 섞인 **자격증명(client_secret·봇 토큰)은 Fernet 으로 암호화**해 저장한다.
    키는 `APP_SECRET_KEY` 환경변수에서 파생한다(기본값 `jogiclub`).
    **DB 파일만 새어 나가도 키는 풀리지 않지만, 환경변수까지 함께 새면 풀린다.**
  - 로그인 토큰은 원문을 저장하지 않고 sha256 해시만 넣는다(쿠키 탈취 ≠ DB 탈취).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

DEFAULT_SECRET_KEY = "jogiclub"
AUTH_TTL = 30 * 24 * 3600.0          # 로그인 유지 30일

# 설정 중 **암호화해서 저장**할 항목. 나머지는 평문(숫자·불리언)이라 그대로 둔다.
SECRET_FIELDS = ("client_secret", "telegram_bot_token")

_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)

# 회원가입 아이디 규칙. 아이디가 로그 디렉터리 이름(`out/users/<아이디>`)에도 쓰이므로
# 경로 문자를 막는다.
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{2,31}")
MIN_PASSWORD = 8


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise StoreError("아이디는 영문·숫자로 시작하는 3~32자(영문·숫자·_ . -)여야 합니다.")
    return username


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD or len(password) > 256:
        raise StoreError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다.")
    return password


class StoreError(RuntimeError):
    """로그인·저장 실패 (화면에 그대로 보여줄 한국어 안내)."""


# ------------------------------------------------------------------ 비밀번호
def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return secrets.compare_digest(dk.hex(), dk_hex)


def _fernet(secret_key: str) -> Fernet:
    """사람이 정한 문자열 → Fernet 키. 짧은 키라도 scrypt 로 늘려 쓴다."""
    dk = hashlib.scrypt(secret_key.encode(), salt=b"stock-find.store", **_SCRYPT)
    return Fernet(base64.urlsafe_b64encode(dk))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    """SQLite 저장소. 스레드 안전(단일 커넥션 + 락)."""

    def __init__(self, path: str | Path, *, secret_key: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.secret_key = secret_key or os.getenv("APP_SECRET_KEY") or DEFAULT_SECRET_KEY
        self._f = _fernet(self.secret_key)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    created_at    INTEGER NOT NULL,
                    last_login    INTEGER,
                    is_admin      INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS settings (
                    username   TEXT PRIMARY KEY,
                    payload    TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bollinger_configs (
                    owner      TEXT PRIMARY KEY,
                    payload    TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token_hash TEXT PRIMARY KEY,
                    username   TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
            """)
            # 예전 DB(관리자 구분 이전)에는 is_admin 열이 없다 — 제자리에서 추가한다.
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(users)")}
            if "is_admin" not in cols:
                self._db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -------------------------------------------------------------- 계정
    def ensure_user(self, username: str, password: str, *, admin: bool = False) -> bool:
        """없으면 만든다. 만들었으면 True (비밀번호를 덮어쓰지는 않는다)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if row:
                return False
            self._db.execute(
                "INSERT INTO users(username, password_hash, created_at, is_admin) VALUES(?,?,?,?)",
                (username, hash_password(password), int(time.time()), int(admin)))
        log.info("계정 생성: %s%s", username, " (관리자)" if admin else "")
        return True

    def create_user(self, username: str, password: str) -> str:
        """회원가입 — 형식 검증 후 **일반 사용자**로 만든다. 이미 있으면 StoreError."""
        username = validate_username(username)
        validate_password(password)
        if not self.ensure_user(username, password):
            raise StoreError("이미 사용 중인 아이디입니다.")
        return username

    def delete_user(self, username: str) -> bool:
        """계정과 그 계정의 모든 저장값(설정·볼린저 설정·로그인 토큰)을 지운다."""
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM users WHERE username=?", (username,))
            self._db.execute("DELETE FROM settings WHERE username=?", (username,))
            self._db.execute("DELETE FROM auth_tokens WHERE username=?", (username,))
            self._db.execute("DELETE FROM bollinger_configs WHERE owner=?", (f"user:{username}",))
        if cur.rowcount:
            log.info("계정 삭제: %s", username)
        return cur.rowcount > 0

    def is_admin(self, username: str | None) -> bool:
        if not username:
            return False
        with self._lock:
            row = self._db.execute("SELECT is_admin FROM users WHERE username=?",
                                   (username,)).fetchone()
        return bool(row and row["is_admin"])

    def set_admin(self, username: str, admin: bool = True) -> None:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE users SET is_admin=? WHERE username=?",
                                   (int(admin), username))
        if cur.rowcount == 0:
            raise StoreError("존재하지 않는 계정입니다.")

    def admins(self) -> list[str]:
        with self._lock:
            return [r["username"] for r in
                    self._db.execute("SELECT username FROM users WHERE is_admin=1")]

    def set_password(self, username: str, password: str) -> None:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE users SET password_hash=? WHERE username=?",
                                   (hash_password(password), username))
        if cur.rowcount == 0:
            raise StoreError("존재하지 않는 계정입니다.")

    def verify(self, username: str, password: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT password_hash FROM users WHERE username=?",
                                   (username,)).fetchone()
        # 계정이 없어도 해시 계산을 한 번 돌려 응답 시간으로 계정 존재를 흘리지 않는다.
        stored = row["password_hash"] if row else hash_password("-")
        ok = verify_password(password, stored)
        return bool(row) and ok

    def users(self) -> list[str]:
        with self._lock:
            return [r["username"] for r in self._db.execute("SELECT username FROM users")]

    # -------------------------------------------------------- 로그인 토큰
    def create_token(self, username: str, *, ttl: float = AUTH_TTL) -> str:
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO auth_tokens(token_hash, username, created_at, expires_at) VALUES(?,?,?,?)",
                (_token_hash(token), username, now, now + int(ttl)))
            self._db.execute("UPDATE users SET last_login=? WHERE username=?", (now, username))
        return token

    def user_for_token(self, token: str | None) -> str | None:
        if not token:
            return None
        with self._lock:
            row = self._db.execute(
                "SELECT username, expires_at FROM auth_tokens WHERE token_hash=?",
                (_token_hash(token),)).fetchone()
        if row is None:
            return None
        if row["expires_at"] < time.time():
            self.delete_token(token)
            return None
        return row["username"]

    def delete_user_tokens(self, username: str, *, keep: str | None = None) -> int:
        """비밀번호 변경 시 다른 기기의 로그인을 모두 끊는다 (`keep` 토큰만 남김)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM auth_tokens WHERE username=? AND token_hash<>?",
                (username, _token_hash(keep) if keep else ""))
        return cur.rowcount

    def delete_token(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM auth_tokens WHERE token_hash=?",
                                   (_token_hash(token),))
        return cur.rowcount > 0

    def sweep_tokens(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (int(time.time()),))

    # -------------------------------------------------------------- 설정
    def save_settings(self, username: str, values: dict) -> None:
        """설정 한 벌을 저장한다. 자격증명 필드는 암호화해서 넣는다."""
        payload = {}
        for k, v in values.items():
            if k in SECRET_FIELDS and v:
                payload[k] = {"enc": self._f.encrypt(str(v).encode()).decode()}
            else:
                payload[k] = v
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO settings(username, payload, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (username, json.dumps(payload, ensure_ascii=False), int(time.time())))

    def load_settings(self, username: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT payload FROM settings WHERE username=?",
                                   (username,)).fetchone()
        if row is None:
            return None
        out = {}
        for k, v in json.loads(row["payload"]).items():
            if isinstance(v, dict) and "enc" in v:
                try:
                    out[k] = self._f.decrypt(v["enc"].encode()).decode()
                except InvalidToken:
                    # APP_SECRET_KEY 가 바뀌면 예전 값은 못 푼다 — 비워서 다시 입력받는다.
                    log.warning("설정 복호화 실패(%s.%s) — 키가 바뀌었는지 확인하세요", username, k)
                    out[k] = ""
            else:
                out[k] = v
        return out

    def clear_settings(self, username: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM settings WHERE username=?", (username,))

    # ------------------------------------------------- 볼린저 전략 설정
    # owner 는 `user:<아이디>`(개인 세션 봇) 또는 `server`(서버 기본 봇).
    # 자격증명이 없는 전략 설정이라 평문 JSON 으로 둔다.
    def save_bollinger(self, owner: str, values: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO bollinger_configs(owner, payload, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (owner, json.dumps(values, ensure_ascii=False), int(time.time())))

    def load_bollinger(self, owner: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT payload FROM bollinger_configs WHERE owner=?",
                                   (owner,)).fetchone()
        return json.loads(row["payload"]) if row else None
