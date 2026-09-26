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


def test_session_with_running_bot_is_not_swept(tmp_path):
    """자동매매 봇이 도는 세션은 화면을 오래 안 봐도 폐기하지 않는다."""
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                         client_factory=FakeClient, idle_ttl=-1)
    sess = mgr.create(settings())
    sess.bollinger.state.running = True          # 스레드 없이 실행 중 상태만 흉내
    mgr.sweep()
    assert mgr.get(sess.sid) is sess
    sess.bollinger.state.running = False
    assert mgr.get(sess.sid) is None
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


# ---------------------------------------------------------------- 텔레그램 테스트 전송
class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.text = status, body, str(body)

    def json(self):
        return self._body


def test_telegram_check_rejects_token_in_chat_field():
    """실제 사고: 토큰을 콜론에서 쪼개 Chat ID 칸에 넣고 Bot Token 을 비워 둔 경우."""
    from screener.notifier import TelegramError, TelegramNotifier

    with pytest.raises(TelegramError, match="Bot Token"):
        TelegramNotifier("", "AAHwkk1aRTH9I11vbIDdZ-lEuV4jo").check()
    with pytest.raises(TelegramError, match="Chat ID"):
        TelegramNotifier("8898071993:AAHwkk1aRTH9I11vbIDdZ-lEuV4jo",
                         "AAHwkk1aRTH9I11vbIDdZ-lEuV4jo").check()


def test_telegram_check_success_and_errors(monkeypatch):
    from screener import notifier as nt

    sent = {}

    def fake_post(url, json=None, timeout=None):
        sent["url"], sent["json"] = url, json
        return _Resp(200, {"ok": True, "result": {"chat": {"title": "내 알림방"}}})

    monkeypatch.setattr(nt.requests, "post", fake_post)
    n = nt.TelegramNotifier("8898071993:AAHwkk1aRTH9I11vbIDdZ-lEuV4jo", "12345678")
    assert n.check() == "내 알림방"
    assert sent["json"]["chat_id"] == "12345678"

    monkeypatch.setattr(nt.requests, "post",
                        lambda *a, **k: _Resp(401, {"ok": False, "description": "Unauthorized"}))
    with pytest.raises(nt.TelegramError, match="폐기"):
        n.check()

    monkeypatch.setattr(nt.requests, "post",
                        lambda *a, **k: _Resp(400, {"ok": False, "description": "Bad Request: chat not found"}))
    with pytest.raises(nt.TelegramError, match="getUpdates"):
        n.check()


def test_telegram_test_endpoint(manager, monkeypatch):
    from screener import notifier as nt

    monkeypatch.setattr(nt.requests, "post",
                        lambda *a, **k: _Resp(200, {"ok": True, "result": {"chat": {"first_name": "나"}}}))
    app = create_app(SyntheticDataLoader(n_symbols=2), StrategyConfig(), sessions=manager)
    with TestClient(app) as c:
        r = c.post("/api/session/telegram/test",
                   json={"telegram_bot_token": "8898071993:AAHwkk1aRTH9I11vbIDdZ-lEuV4jo",
                         "telegram_chat_id": "12345678"})
        assert r.status_code == 200 and r.json()["chat"] == "나"

        bad = c.post("/api/session/telegram/test",
                     json={"telegram_bot_token": "", "telegram_chat_id": "12345678"})
        assert bad.status_code == 400 and "Bot Token" in bad.json()["detail"]


def test_auto_buyer_attached_only_when_enabled(manager):
    sess = manager.create(settings())
    assert sess.service.auto_buyer is None
    manager.drop(sess.sid)
    sess = manager.create(settings(auto_buy=True, auto_buy_amount=500000, auto_buy_score=80))
    assert sess.service.auto_buyer.min_score == 80
    assert sess.service.auto_buyer.trading is sess.trading
    info = sess.info()
    assert info["auto_buy"] and info["auto_buy_live"] is False


def test_settings_endpoint_keeps_credentials(client):
    """⚙ 설정 창은 API 키를 받지 않는다 — 저장된 키로 세션을 다시 연다."""
    client.post("/api/session", json={"client_id": "tsck_live_abcd1234",
                                      "client_secret": "tssk_live_secret",
                                      "top": 0, "interval": 60})
    r = client.post("/api/session/settings", json={"interval": 120, "auto_buy": True,
                                                   "auto_buy_score": 80,
                                                   "auto_buy_amount": 300000})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["interval"] == 120 and info["auto_buy_score"] == 80
    assert info["client_id_masked"] and "client_secret" not in info
    # 키만 다시 넣어도 다른 설정은 유지된다
    again = client.post("/api/session", json={"client_id": "tsck_live_abcd1234",
                                              "client_secret": "tssk_live_secret"}).json()
    assert again["interval"] == 120 and again["auto_buy"] is True


def test_settings_endpoint_without_keys_is_rejected(client):
    r = client.post("/api/session/settings", json={"interval": 120})
    assert r.status_code == 400 and "API 키" in r.json()["detail"]


# ------------------------------------------------------------------ 이메일 알림
from screener.notifier import EmailNotifier, parse_emails  # noqa: E402

MAIL = {"host": "smtp.test", "port": 587, "user": "from@test.com",
        "password": "pw", "crypto": "tls", "from_name": "왔니 주식"}


def test_alert_emails_parsed_and_deduped():
    s = SessionSettings.from_body({"client_id": "tsck_live_abcd1234",
                                   "client_secret": "tssk_live_secret",
                                   "alert_emails": "A@A.com, a@a.com\nbad-addr; b@b.co"})
    assert s.alert_emails == "a@a.com, b@b.co"      # 소문자·중복 제거·형식 검증
    assert parse_emails(s.alert_emails) == ["a@a.com", "b@b.co"]


def test_email_channel_attached_when_server_mail_set(tmp_path):
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                         client_factory=FakeClient, mail=MAIL)
    sess = mgr.create(settings(alert_emails="a@a.com, b@b.com"))
    inner = sess.service.notifier.inner.notifiers      # DedupeNotifier→MultiNotifier
    ems = [n for n in inner if isinstance(n, EmailNotifier)]
    assert len(ems) == 1 and ems[0].recipients == ["a@a.com", "b@b.com"]
    assert sess.info()["email_count"] == 2
    mgr.stop_all()


def test_no_email_channel_without_server_mail(tmp_path):
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                         client_factory=FakeClient, mail=None)
    sess = mgr.create(settings(alert_emails="a@a.com"))
    assert sess.service.notifier is None               # 서버 발신 계정 없으면 채널 없음
    assert sess.info()["email_count"] == 1             # 설정값은 보존
    mgr.stop_all()


def test_email_test_endpoint(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(EmailNotifier, "_deliver",
                        lambda self, subj, body: sent.update(to=self.recipients))
    mgr = SessionManager(StrategyConfig.tuned(), cache_dir=tmp_path, workers=2,
                         client_factory=FakeClient, mail=MAIL)
    app = create_app(None, StrategyConfig.tuned(), interval=3600, top=0,
                     workers=2, sessions=mgr)
    with TestClient(app) as c:
        r = c.post("/api/session/email/test", json={"alert_emails": "x@y.com"})
        assert r.status_code == 200 and sent["to"] == ["x@y.com"]
        bad = c.post("/api/session/email/test", json={"alert_emails": "not-an-email"})
        assert bad.status_code == 400
    mgr.stop_all()
