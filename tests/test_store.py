"""로그인 계정 · 설정 영속 저장 (screener/store.py) 회귀 테스트."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader
from screener.sessions import COOKIE_NAME, SessionManager
from screener.store import Store, StoreError, hash_password, verify_password
from screener.web import AUTH_COOKIE, create_app

from tests.test_sessions import FakeClient

USER, PW = "tester", "pw-1234!"


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "app.db", secret_key="unit-test-key")
    st.ensure_user(USER, PW)
    yield st
    st.close()


# ------------------------------------------------------------------ 저장소
def test_password_is_hashed_not_stored():
    h = hash_password(PW)
    assert PW not in h and h.startswith("scrypt$")
    assert verify_password(PW, h) and not verify_password("틀린비번", h)


def test_ensure_user_is_idempotent(store):
    assert store.ensure_user(USER, "다른비번") is False      # 기존 비번을 덮어쓰지 않는다
    assert store.verify(USER, PW) is True


def test_verify_rejects_unknown_user(store):
    assert store.verify("없는사람", PW) is False


def test_set_password(store):
    store.set_password(USER, "새비번!")
    assert store.verify(USER, "새비번!") and not store.verify(USER, PW)
    with pytest.raises(StoreError):
        store.set_password("없는사람", "x")


def test_secrets_are_encrypted_at_rest(tmp_path):
    st = Store(tmp_path / "app.db", secret_key="unit-test-key")
    st.ensure_user(USER, PW)
    st.save_settings(USER, {"client_id": "tsck_live_abcd1234",
                            "client_secret": "tssk_live_secret",
                            "telegram_bot_token": "123456789:AAtoken", "top": 50})
    raw = (tmp_path / "app.db").read_bytes()
    assert b"tssk_live_secret" not in raw          # secret 은 평문으로 남지 않는다
    assert b"AAtoken" not in raw
    assert b"tsck_live_abcd1234" in raw            # client_id 는 평문 (마스킹 표시용)
    assert st.load_settings(USER) == {
        "client_id": "tsck_live_abcd1234", "client_secret": "tssk_live_secret",
        "telegram_bot_token": "123456789:AAtoken", "top": 50}
    st.close()


def test_wrong_key_cannot_decrypt(tmp_path):
    st = Store(tmp_path / "app.db", secret_key="key-a")
    st.save_settings(USER, {"client_secret": "tssk_live_secret"})
    st.close()
    other = Store(tmp_path / "app.db", secret_key="key-b")
    assert other.load_settings(USER)["client_secret"] == ""   # 못 풀면 빈 값으로 다시 입력받는다
    other.close()


def test_token_lifecycle(store):
    token = store.create_token(USER)
    assert store.user_for_token(token) == USER
    assert store.user_for_token("가짜토큰") is None
    assert store.delete_token(token) and store.user_for_token(token) is None


def test_expired_token_is_rejected(store):
    token = store.create_token(USER, ttl=-1)
    assert store.user_for_token(token) is None


# ------------------------------------------------------------------ 웹 로그인
@pytest.fixture
def app(tmp_path, store):
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path / "cache",
                         workers=2, client_factory=FakeClient, max_sessions=2)
    application = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig(),
                             sessions=mgr, store=store)
    yield application
    mgr.stop_all()


def test_pages_require_login(app):
    with TestClient(app) as c:
        r = c.get("/", headers={"accept": "text/html"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/login"
        assert c.get("/api/state").status_code == 401
        # 로그인 화면과 정적 파일은 열려 있어야 한다.
        assert c.get("/login").status_code == 200
        assert c.get("/static/common.css").status_code == 200
        assert c.get("/healthz").status_code == 200


def test_login_logout_flow(app):
    with TestClient(app) as c:
        assert c.post("/api/login", json={"username": USER, "password": "틀린비번"}).status_code == 401
        r = c.post("/api/login", json={"username": USER, "password": PW})
        assert r.status_code == 200 and c.cookies.get(AUTH_COOKIE)
        assert c.get("/api/me").json()["username"] == USER
        assert c.get("/api/state").status_code == 200

        c.post("/api/logout")
        assert c.get("/api/state").status_code == 401


def test_settings_persist_and_restore(app, store):
    """설정을 저장하면 DB 에 남고, 쿠키를 잃어도 로그인만으로 복구된다."""
    body = {"client_id": "tsck_live_abcd1234", "client_secret": "tssk_live_secret",
            "top": 0, "interval": 60, "telegram_bot_token": "123456789:AAtoken",
            "telegram_chat_id": "12345678"}
    with TestClient(app) as c:
        c.post("/api/login", json={"username": USER, "password": PW})
        assert c.post("/api/session", json=body).json()["persisted"] is True
        saved = store.load_settings(USER)
        assert saved["client_secret"] == "tssk_live_secret" and saved["top"] == 0

    # 새 브라우저(쿠키 없음) — 로그인만 하면 저장된 키로 세션이 되살아난다.
    with TestClient(app) as c2:
        c2.post("/api/login", json={"username": USER, "password": PW})
        info = c2.get("/api/session").json()
        assert info["source"] == "session" and info["telegram"] is True
        assert info["username"] == USER
        # 저장된 secret 은 절대 응답에 실리지 않는다.
        assert "tssk_live_secret" not in c2.get("/api/session").text


def test_session_delete_clears_saved_settings(app, store):
    with TestClient(app) as c:
        c.post("/api/login", json={"username": USER, "password": PW})
        c.post("/api/session", json={"client_id": "tsck_live_abcd1234",
                                     "client_secret": "tssk_live_secret",
                                     "top": 0, "interval": 60})
        c.delete("/api/session")
        assert store.load_settings(USER) is None
        assert c.get("/api/session").json()["source"] in (None, "server")


# ------------------------------------------------------- 볼린저 설정 영속화
def test_bollinger_config_roundtrip(store):
    assert store.load_bollinger("user:x") is None
    store.save_bollinger("user:x", {"symbols": ["005930"], "poll_interval": 90})
    assert store.load_bollinger("user:x") == {"symbols": ["005930"], "poll_interval": 90}


SESSION_BODY = {"client_id": "tsck_live_abcd1234", "client_secret": "tssk_live_secret",
                "top": 0, "interval": 60}


def test_bollinger_watchlist_survives_session_rebuild(app, store):
    """감시 목록·설정은 DB 에 남고, 세션을 새로 만들어도(재시작·새 브라우저) 복구된다."""
    with TestClient(app) as c:
        c.post("/api/login", json={"username": USER, "password": PW})
        c.post("/api/session", json=SESSION_BODY)
        r = c.post("/api/bollinger/config", json={"symbols": "005930", "stop_loss_pct": 0.03,
                                                  "manage_existing": True, "poll_interval": 90})
        assert r.json()["persisted"] is True
        assert c.post("/api/bollinger/symbols", json={"symbol": "100130"}).json()["persisted"] is True
        saved = store.load_bollinger(f"user:{USER}")
        assert saved["symbols"] == ["005930", "100130"]
        assert "manage_existing" not in saved                 # 안전장치는 저장하지 않는다

    # 메모리 세션을 모두 버린다 (= 서버 재시작과 같은 상황)
    app.state.sessions.stop_all()
    with TestClient(app) as c2:
        c2.post("/api/login", json={"username": USER, "password": PW})
        body = c2.get("/api/bollinger/state").json()
        cfg = body["config"]
        assert cfg["symbols"] == ["005930", "100130"]
        assert cfg["stop_loss_pct"] == 0.03 and body["poll_interval"] == 90
        assert cfg["manage_existing"] is False
        assert body["running"] is False                       # 복구해도 자동 시작하지 않는다


def test_bollinger_settings_are_per_user(app, store):
    store.ensure_user("other", PW)
    with TestClient(app) as c:
        c.post("/api/login", json={"username": USER, "password": PW})
        c.post("/api/session", json=SESSION_BODY)
        c.post("/api/bollinger/symbols", json={"symbol": "005930"})
    assert store.load_bollinger("user:other") is None
    assert store.load_bollinger("server") is None


def test_server_bot_loads_saved_config(tmp_path):
    from tests.test_bollinger_bot import ExplodingClient, make_bot
    st = Store(tmp_path / "app.db", secret_key="k")
    st.save_bollinger("server", {"symbols": ["000660"], "interval": "30m", "poll_interval": 120,
                                 "manage_existing": True})
    bot, _ = make_bot(ExplodingClient(), enabled=False, live=False)
    create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig(), bollinger=bot, store=st)
    assert bot.cfg.symbols == ("000660",) and bot.cfg.interval == "30m"
    assert bot.poll_interval == 120 and bot.cfg.manage_existing is False
    st.close()


# ------------------------------------------------------- 다중 사용자 격리
def test_legacy_db_gets_admin_column(tmp_path):
    """관리자 구분 이전의 DB 도 열리고 is_admin 열이 추가된다."""
    import sqlite3
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, "
               "created_at INTEGER NOT NULL, last_login INTEGER)")
    db.execute("INSERT INTO users VALUES('old', ?, 0, NULL)", (hash_password(PW),))
    db.commit()
    db.close()
    st = Store(path, secret_key="k")
    assert st.is_admin("old") is False and st.verify("old", PW)
    st.set_admin("old")
    assert st.admins() == ["old"]
    st.close()


def test_create_user_validates(store):
    with pytest.raises(StoreError):
        store.create_user("../evil", "password123")
    with pytest.raises(StoreError):
        store.create_user("newbie", "short")
    with pytest.raises(StoreError):
        store.create_user(USER, "password123")               # 중복
    assert store.create_user("newbie", "password123") == "newbie"
    assert store.is_admin("newbie") is False


def test_delete_user_removes_everything(store):
    store.save_settings(USER, {"client_id": "x", "client_secret": "y"})
    store.save_bollinger(f"user:{USER}", {"symbols": ["005930"]})
    tok = store.create_token(USER)
    assert store.delete_user(USER)
    assert store.load_settings(USER) is None
    assert store.load_bollinger(f"user:{USER}") is None
    assert store.user_for_token(tok) is None and USER not in store.users()


def _login(c, user=USER, pw=PW):
    r = c.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200, r.text
    return r


def test_other_users_session_cookie_is_ignored(app, store):
    """같은 브라우저에서 A 로그아웃 → B 로그인해도 A 의 키·계좌 세션을 쓰면 안 된다."""
    store.ensure_user("bob", PW)
    with TestClient(app) as c:
        _login(c)
        c.post("/api/session", json=SESSION_BODY)
        a_sid = c.cookies.get(COOKIE_NAME)
        assert a_sid
        c.post("/api/logout")
        assert not c.cookies.get(COOKIE_NAME)

        # 쿠키를 강제로 되살려도(탈취·잔존) 다른 계정에는 붙지 않는다.
        _login(c, "bob")
        c.cookies.set(COOKIE_NAME, a_sid)
        info = c.get("/api/session").json()
        assert info["username"] == "bob" and info["source"] != "session"
        assert c.post("/api/bollinger/symbols", json={"symbol": "005930"}).status_code == 503
        # B 의 설정 저장이 A 의 세션을 끊지도 않는다.
        c.post("/api/session", json={**SESSION_BODY, "client_id": "tsck_live_bob00001"})
        assert app.state.sessions.get(a_sid) is not None
        assert app.state.sessions.get(a_sid).owner == USER


def test_server_account_is_admin_only(tmp_path, store):
    """서버 .env 계좌(주문·볼린저 봇)는 관리자만 쓴다. 시세 화면은 모두 공유."""
    from tests.test_bollinger_bot import ExplodingClient, make_bot
    bot, _ = make_bot(ExplodingClient(), enabled=False, live=False)
    store.ensure_user("boss", PW, admin=True)
    application = create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig(),
                             bollinger=bot, trading=bot.trading, store=store)
    with TestClient(application) as c:
        _login(c)                                              # 일반 사용자
        assert c.get("/api/state").json()["configured"] is True
        assert c.get("/api/bollinger/state").json()["configured"] is False
        assert c.post("/api/bollinger/start").status_code == 503
        assert c.get("/api/trading/status").json()["configured"] is False
        info = c.get("/api/session").json()
        assert info["is_admin"] is False and info["server_trading"] is False
        c.post("/api/logout")

        _login(c, "boss")
        assert c.get("/api/bollinger/state").json()["configured"] is True
        assert c.get("/api/trading/status").json()["configured"] is True
        assert c.get("/api/me").json()["is_admin"] is True


def test_signup_flow(tmp_path, store):
    application = create_app(SyntheticDataLoader(n_symbols=1), StrategyConfig(),
                             store=store, signup=True, signup_code="invite-me")
    with TestClient(application) as c:
        opts = c.get("/api/auth/options").json()
        assert opts["signup"] and opts["signup_code_required"]
        body = {"username": "newbie", "password": "password123"}
        assert c.post("/api/signup", json=body).status_code == 403          # 코드 없음
        assert c.post("/api/signup", json={**body, "code": "invite-me"}).status_code == 200
        me = c.get("/api/me").json()
        assert me["username"] == "newbie" and me["is_admin"] is False
        assert c.post("/api/signup", json={**body, "code": "invite-me"}).status_code == 400


def test_signup_disabled_by_default(app):
    with TestClient(app) as c:
        assert c.get("/api/auth/options").json()["signup"] is False
        r = c.post("/api/signup", json={"username": "newbie", "password": "password123"})
        assert r.status_code == 403


def test_login_is_throttled(app):
    with TestClient(app) as c:
        for _ in range(5):
            assert c.post("/api/login", json={"username": USER, "password": "x"}).status_code == 401
        # 잠긴 동안에는 맞는 비밀번호도 막힌다.
        assert c.post("/api/login", json={"username": USER, "password": PW}).status_code == 429


def test_password_change_revokes_other_logins(app, store):
    with TestClient(app) as other, TestClient(app) as c:
        _login(other)
        _login(c)
        assert c.post("/api/account/password",
                      json={"current": "wrong", "new": "newpass-123"}).status_code == 400
        r = c.post("/api/account/password", json={"current": PW, "new": "newpass-123"})
        assert r.status_code == 200 and r.json()["revoked"] == 1
        assert c.get("/api/me").json()["username"] == USER          # 내 로그인은 유지
        assert other.get("/api/state").status_code == 401           # 다른 기기는 끊김
        assert store.verify(USER, "newpass-123")


def test_account_delete(app, store):
    with TestClient(app) as c:
        _login(c)
        c.post("/api/session", json=SESSION_BODY)
        sid = c.cookies.get(COOKIE_NAME)
        assert c.request("DELETE", "/api/account", json={"password": "x"}).status_code == 400
        assert c.request("DELETE", "/api/account", json={"password": PW}).status_code == 200
        assert USER not in store.users() and store.load_settings(USER) is None
        assert app.state.sessions.get(sid) is None
        assert c.get("/api/state").status_code == 401


def test_last_admin_cannot_delete_self(app, store):
    store.set_admin(USER)
    with TestClient(app) as c:
        _login(c)
        r = c.request("DELETE", "/api/account", json={"password": PW})
        assert r.status_code == 400 and USER in store.users()


def test_session_logs_are_per_user(tmp_path):
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path / "cache",
                         client_factory=FakeClient,
                         order_log=tmp_path / "out" / "orders.jsonl",
                         bollinger_log=tmp_path / "out" / "bollinger.log")
    order, bb = mgr.log_paths("alice")
    assert order == tmp_path / "out" / "users" / "alice" / "orders.jsonl"
    assert bb == tmp_path / "out" / "users" / "alice" / "bollinger.log"
    assert mgr.log_paths(None)[0] == tmp_path / "out" / "orders.jsonl"
    assert ".." not in str(mgr.log_paths("../x")[0].relative_to(tmp_path))
