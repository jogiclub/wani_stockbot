"""토큰 공유 · 재시도 정책 테스트 (네트워크 없이 동작)."""

import time

import pytest

from screener.toss_client import TossAPIError, TossClient, _Token


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"result": {"ok": True}}
        self.headers = headers or {}
        self.text = "stub"

    def json(self):
        return self._payload


class StubSession:
    """POST 응답을 미리 정해두고 호출 횟수를 센다."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers or {}})
        return self.responses.pop(0) if self.responses else FakeResponse(200)

    def get(self, *a, **k):
        raise AssertionError("이 테스트는 GET 을 쓰지 않는다")


def client(session, tmp_path=None, tokens=("tok-1", "tok-2", "tok-3")):
    c = TossClient("id", "secret", session=session,
                   token_cache=(tmp_path / "token.json") if tmp_path else None)
    issued = iter(tokens)
    c._issue_token = lambda: _Token(next(issued), time.time() + 1800)   # 발급만 가짜로
    return c


# ------------------------------------------------------------------ 토큰 공유
def test_token_is_shared_between_clients(tmp_path):
    """같은 자격증명을 쓰는 두 프로세스가 토큰을 나눠 쓴다.

    토스는 클라이언트당 유효 토큰이 1개라, 각자 발급하면 서로를 무효화시킨다.
    """
    issued = []

    def make():
        c = TossClient("id", "secret", token_cache=tmp_path / "token.json")
        c._issue_token = lambda: (issued.append(1),
                                  _Token(f"tok-{len(issued)}", time.time() + 1800))[1]
        return c

    a, b = make(), make()
    assert a._access_token() == b._access_token() == "tok-1"
    assert len(issued) == 1                      # 두 번째 클라이언트는 재발급하지 않는다


def test_invalidate_clears_shared_token(tmp_path):
    a = client(StubSession([]), tmp_path)
    first = a._access_token()
    a._invalidate_token()
    assert a._access_token() != first            # 새 토큰으로 교체
    assert not (tmp_path / "token.json").read_text().count(first)


def test_client_without_cache_still_works():
    c = client(StubSession([]))
    assert c._access_token() == "tok-1"
    assert c._access_token() == "tok-1"           # 메모리 캐시 재사용


# ------------------------------------------------------------------ POST 정책
def test_post_retries_once_on_401(tmp_path):
    """401 은 인증 계층 거절이라 주문에 도달하지 못했으므로 재시도해도 안전하다."""
    session = StubSession([FakeResponse(401, {"error": {"code": "invalid-token"}}),
                           FakeResponse(200, {"result": {"orderId": "ORD-1"}})])
    c = client(session, tmp_path)
    assert c.post("/api/v1/orders", {"symbol": "005930"}) == {"orderId": "ORD-1"}
    assert len(session.calls) == 2
    # 두 번째 요청은 새 토큰을 쓴다
    assert session.calls[0]["headers"]["Authorization"] != session.calls[1]["headers"]["Authorization"]


def test_post_gives_up_after_second_401(tmp_path):
    session = StubSession([FakeResponse(401, {"error": {"code": "invalid-token"}}),
                           FakeResponse(401, {"error": {"code": "invalid-token"}})])
    with pytest.raises(TossAPIError) as exc:
        client(session, tmp_path).post("/api/v1/orders", {})
    assert exc.value.status == 401
    assert len(session.calls) == 2                # 무한 재시도하지 않는다


@pytest.mark.parametrize("status", [400, 422, 429, 500, 503])
def test_post_never_retries_non_401(status, tmp_path):
    """중복 체결 위험이 있는 상태코드는 절대 재시도하지 않는다."""
    session = StubSession([FakeResponse(status, {"error": {"code": "boom", "message": "x"}}),
                           FakeResponse(200)])
    with pytest.raises(TossAPIError):
        client(session, tmp_path).post("/api/v1/orders", {})
    assert len(session.calls) == 1


def test_post_sends_account_header(tmp_path):
    session = StubSession([FakeResponse(200, {"result": {}})])
    client(session, tmp_path).post("/api/v1/orders", {}, account=7)
    assert session.calls[0]["headers"]["X-Tossinvest-Account"] == "7"
