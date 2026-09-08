"""세션별 개인 API 키 (설정 창) 회귀 테스트.

실제 토스 API 는 호출하지 않는다 — `client_factory` 로 가짜 클라이언트를 주입한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader
from screener.sessions import (COOKIE_NAME, SessionError, SessionManager,
                               SessionSettings, mask)
from screener.toss_client import TossAPIError
from screener.web import create_app


class FakeClient:
    """TossClient 대역. 스캔이 곧바로 끝나도록 전부 빈 응답을 준다."""

    def __init__(self, client_id, client_secret, token_cache=None, **_kw):
        self.client_id, self.client_secret = client_id, client_secret
        self.token_cache = token_cache
        self.calls: list[str] = []

    def prices(self, symbols):
        self.calls.append("prices")
        if self.client_secret == "bad-secret":
            raise TossAPIError(401, "invalid-token", "자격증명 오류")
        return [{"symbol": s, "lastPrice": "1000"} for s in symbols]

    def stocks_all(self, market, *a, **k):
        return []

    def index_candles_paged(self, *a, **k):
        return []

    def rankings(self, *a, **k):
        return {"rankings": []}

    def accounts(self):
        return []


@pytest.fixture
def manager(tmp_path):
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path / "cache",
                         workers=2, client_factory=FakeClient, max_sessions=2)
    yield mgr
    mgr.stop_all()


def settings(**over) -> SessionSettings:
    body = {"client_id": "tsck_live_abcd1234", "client_secret": "tssk_live_secret",
            "top": 0, "interval": 60}
    body.update(over)
    return SessionSettings.from_body(body)


# ----------------------------------------------------------------- 입력 검증
def test_from_body_requires_credentials():
    with pytest.raises(SessionError):
        SessionSettings.from_body({"client_id": "", "client_secret": ""})
    with pytest.raises(SessionError):
        SessionSettings.from_body({"client_id": "x", "client_secret": "tssk_live_secret"})


def test_from_body_clamps_ranges():
    s = settings(interval=1, top=99999, limit=-5)
    assert s.interval == 30            # 하한
    assert s.top == 1000               # 상한
    assert s.limit is None             # 0 이하면 미적용


def test_mask_hides_secret():
    assert "secret" not in mask("tssk_live_secret_value")
    assert mask("tsck_live_abcd1234").endswith("1234")


# ----------------------------------------------------------------- 세션 관리
def test_create_and_get(manager):
    sess = manager.create(settings())
    assert manager.get(sess.sid) is sess
    info = sess.info()
    assert info["configured"] and info["source"] == "session"
    assert "secret" not in str(info).lower()          # secret 유출 금지
    assert info["client_id_masked"] != sess.settings.client_id


def test_token_cache_is_per_credential(manager):
    a = manager.create(settings(client_id="tsck_live_aaaa1111"))
    b = manager.create(settings(client_id="tsck_live_bbbb2222"))
    assert a.client.token_cache != b.client.token_cache    # 서로 토큰을 무효화하지 않는다


def test_invalid_credentials_rejected(manager):
    with pytest.raises(SessionError):
        manager.create(settings(client_secret="bad-secret"))
    assert manager.get("nope") is None


def test_max_sessions(manager):
    manager.create(settings(client_id="tsck_live_aaaa1111"))
    manager.create(settings(client_id="tsck_live_bbbb2222"))
    with pytest.raises(SessionError):
        manager.create(settings(client_id="tsck_live_cccc3333"))


def test_drop_stops_service(manager):
    sess = manager.create(settings())
    assert manager.drop(sess.sid) is True
    assert manager.get(sess.sid) is None
    assert manager.drop(sess.sid) is False


def test_idle_sessions_expire(tmp_path):
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                         client_factory=FakeClient, idle_ttl=-1)
    sess = mgr.create(settings())
    assert mgr.get(sess.sid) is None            # 곧바로 만료
    mgr.stop_all()


def test_trading_needs_server_switch_and_opt_in(tmp_path):
    off = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                         client_factory=FakeClient, trading_enabled=False)
    assert off.create(settings(trading_opt_in=True)).trading.enabled is False
    off.stop_all()

    on = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                        client_factory=FakeClient, trading_enabled=True,
                        max_order_amount=1_000_000)
    assert on.create(settings(trading_opt_in=False)).trading.enabled is False
    sess = on.create(settings(client_id="tsck_live_bbbb2222", trading_opt_in=True,
                              max_order_amount=5_000_000))
    assert sess.trading.enabled is True
    assert sess.trading.max_order_amount == 1_000_000   # 서버 상한을 넘지 못한다
    on.stop_all()


# ----------------------------------------------------------------- HTTP
@pytest.fixture
def client(manager):
    app = create_app(None, StrategyConfig.tuned(), interval=3600, top=0,
                     workers=2, sessions=manager)
    with TestClient(app) as c:
        yield c


def test_state_without_credentials(client):
    body = client.get("/api/state").json()
    assert body["configured"] is False and body["source"] is None
    assert client.get("/api/session").json()["multi_user"] is True
    assert client.get("/api/signals").status_code == 503


def test_session_lifecycle_over_http(client):
    r = client.post("/api/session", json={"client_id": "tsck_live_abcd1234",
                                          "client_secret": "tssk_live_secret",
                                          "top": 0, "interval": 60})
    assert r.status_code == 200, r.text
    assert COOKIE_NAME in r.cookies
    assert "tssk" not in r.text                        # secret 회신 금지

    state = client.get("/api/state").json()
    assert state["configured"] is True and state["source"] == "session"
    assert client.get("/api/signals").status_code == 200

    assert client.delete("/api/session").json()["dropped"] is True
    assert client.get("/api/state").json()["configured"] is False


def test_bad_credentials_return_400(client):
    r = client.post("/api/session", json={"client_id": "tsck_live_abcd1234",
                                          "client_secret": "bad-secret"})
    assert r.status_code == 400
    assert "확인" in r.json()["detail"]


def test_sessions_are_isolated(manager):
    app = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig.tuned(),
                     interval=3600, top=0, workers=2, sessions=manager)
    with TestClient(app) as c:
        assert c.get("/api/state").json()["source"] == "server"   # 쿠키 없으면 서버 기본
        c.post("/api/session", json={"client_id": "tsck_live_abcd1234",
                                     "client_secret": "tssk_live_secret",
                                     "top": 0, "interval": 60})
        assert c.get("/api/state").json()["source"] == "session"
