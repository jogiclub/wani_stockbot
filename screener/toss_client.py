"""토스증권 OpenAPI REST 클라이언트.

문서: https://developers.tossinvest.com/docs
- Base URL: https://openapi.tossinvest.com
- 인증: OAuth 2.0 Client Credentials (POST /oauth2/token)
- 응답 envelope: {"result": ...} / 실패 시 {"error": {...}}
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

try:                                    # 파일 락 (POSIX)
    import fcntl
except ImportError:                     # Windows 등
    fcntl = None

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.tossinvest.com"
WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"

# 엔드포인트별 Rate Limits Group (초당 요청 수). 공식 문서 기준값.
RATE_LIMITS = {
    "AUTH": 5,
    "STOCK": 5,
    "STOCK_ALL": 1,
    "MARKET_DATA": 15,
    "MARKET_DATA_CHART": 20,
    "RANKING": 5,
    "MARKET_INFO": 3,
    "MARKET_INDICATOR_CHART": 5,
    "ACCOUNT": 1,
    "ASSET": 5,
    "ORDER": 10,
    "ORDER_HISTORY": 5,
    "ORDER_INFO": 6,
    "CONDITIONAL_ORDER": 5,
    "CONDITIONAL_ORDER_HISTORY": 5,
    "MARKET_INDICATOR": 5,
    "STOCK_TRADING_TREND": 3,
}


def token_cache_path(cache_dir: Path | str, client_id: str) -> Path:
    """자격증명별 토큰 캐시 경로.

    토스는 **클라이언트당 유효 토큰 1개**다. 같은 client_id 를 쓰는 프로세스·세션은
    같은 파일을 공유해야 서로의 토큰을 무효화하지 않고, 다른 client_id 는
    파일이 갈라져야 한다.
    """
    digest = hashlib.sha256(client_id.encode()).hexdigest()[:16]
    return Path(cache_dir) / "tokens" / f"{digest}.json"


class TossAPIError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"[{status}] {code}: {message}")
        self.status, self.code, self.message = status, code, message


class _RateLimiter:
    """그룹별 슬라이딩 윈도우 TPS 제한기 (스레드 안전)."""

    def __init__(self, per_second: int):
        self.per_second = max(1, per_second)
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= 1.0:
                    self._hits.popleft()
                if len(self._hits) < self.per_second:
                    self._hits.append(now)
                    return
                wait = 1.0 - (now - self._hits[0])
            time.sleep(max(wait, 0.01))


@dataclass
class _Token:
    value: str
    expires_at: float

    @property
    def valid(self) -> bool:
        return bool(self.value) and time.time() < self.expires_at - 30  # 30초 여유


class _SharedTokenStore:
    """프로세스 간 토큰 공유 저장소.

    토스는 **클라이언트당 유효 토큰이 1개**라, 같은 자격증명을 쓰는 프로세스가
    각자 발급받으면 서로의 토큰을 무효화시킨다 (web 과 poller 를 함께 띄우면
    주문 POST 가 `401 invalid-token` 으로 실패하던 원인).

    발급을 파일 락으로 직렬화하고 결과를 공유해 그 경합을 없앤다.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def locked(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = self.lock_path.open("a+")
        except OSError:                     # 파일을 못 쓰면 공유 없이 진행
            yield None
            return
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield fh
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def read(self) -> _Token:
        try:
            data = json.loads(self.path.read_text())
            return _Token(data.get("access_token", ""), float(data.get("expires_at", 0)))
        except (OSError, ValueError, TypeError):
            return _Token("", 0.0)

    def write(self, token: _Token) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"access_token": token.value,
                                       "expires_at": token.expires_at}))
            tmp.replace(self.path)
            self.path.chmod(0o600)
        except OSError as exc:
            log.debug("토큰 캐시 기록 실패: %s", exc)

    def invalidate(self, stale: str) -> None:
        """무효화된 토큰이 아직 파일에 있으면 지운다 (남의 새 토큰은 건드리지 않음)."""
        with self.locked():
            if self.read().value == stale:
                try:
                    self.path.unlink(missing_ok=True)
                except OSError:
                    pass


class TossClient:
    """토스증권 OpenAPI 호출 래퍼.

    - 액세스 토큰 자동 발급/갱신 (client 당 유효 토큰 1개)
    - 그룹별 TPS 제한 준수, 429/5xx 지수 백오프 재시도
    """

    def __init__(self, client_id: str, client_secret: str, timeout: float = 10.0,
                 max_retries: int = 6, session: requests.Session | None = None,
                 token_cache: Path | str | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._token = _Token("", 0.0)
        self._token_lock = threading.Lock()
        self._store = _SharedTokenStore(Path(token_cache)) if token_cache else None
        self._limiters = {g: _RateLimiter(tps) for g, tps in RATE_LIMITS.items()}

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        with self._token_lock:
            if self._token.valid:
                return self._token.value
            if self._store is None:
                self._token = self._issue_token()
                return self._token.value
            # 프로세스 간 발급 경합 방지 — 락 안에서 다시 확인한다
            with self._store.locked():
                shared = self._store.read()
                if shared.valid:
                    self._token = shared
                    return shared.value
                self._token = self._issue_token()
                self._store.write(self._token)
                return self._token.value

    def _invalidate_token(self) -> None:
        """401 을 받았을 때 캐시된 토큰을 버린다."""
        with self._token_lock:
            stale, self._token = self._token.value, _Token("", 0.0)
        if self._store and stale:
            self._store.invalidate(stale)

    def _issue_token(self) -> _Token:
        """새 토큰을 발급받는다. 이 호출로 기존 토큰은 즉시 무효화된다."""
        self._limiters["AUTH"].acquire()
        resp = self.session.post(
            f"{BASE_URL}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        if resp.status_code != 200:
            body = _safe_json(resp)
            raise TossAPIError(resp.status_code,
                               body.get("error", "token-error"),
                               body.get("error_description", resp.text[:200]))
        body = resp.json()
        log.debug("토큰 발급 완료 (만료 %ss)", body.get("expires_in"))
        return _Token(body["access_token"],
                      time.time() + int(body.get("expires_in", 1800)))

    # ------------------------------------------------------------------ http
    def get(self, path: str, params: dict[str, Any] | None = None,
            group: str = "MARKET_DATA", account: int | None = None) -> Any:
        """GET 요청 후 envelope 의 `result` 를 반환한다."""
        limiter = self._limiters.setdefault(group, _RateLimiter(RATE_LIMITS.get(group, 5)))
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            limiter.acquire()
            token = self._access_token()
            try:
                resp = self.session.get(
                    f"{BASE_URL}{path}",
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    headers=self._headers(token, account),
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:      # 네트워크 오류
                last_exc = exc
                time.sleep(min(2 ** attempt * 0.5, 4))
                continue

            if resp.status_code == 200:
                return resp.json().get("result")
            if resp.status_code == 401:                    # 토큰 만료·무효 → 재발급
                self._invalidate_token()
                last_exc = TossAPIError(401, "unauthorized", resp.text[:200])
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                # 같은 자격증명을 쓰는 프로세스가 여럿이면 프로세스별 TPS 제한만으로는
                # 부족해 429 가 난다. Retry-After 를 우선 따르고, 지터를 섞어 재충돌을 피한다.
                retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                backoff = retry_after or min(2 ** attempt * 0.5, 8) * (1 + random.random() * 0.3)
                time.sleep(backoff)
                last_exc = TossAPIError(resp.status_code, "retryable", resp.text[:200])
                continue

            body = _safe_json(resp).get("error", {})
            raise TossAPIError(resp.status_code, body.get("code", "unknown"),
                               body.get("message", resp.text[:200]))
        raise last_exc or RuntimeError("요청 실패")

    def post(self, path: str, body: dict, group: str = "ORDER",
             account: int | None = None) -> Any:
        """POST 요청.

        **타임아웃·5xx·429 는 재시도하지 않는다** — 주문이 접수됐는지 알 수 없어
        중복 체결 위험이 있기 때문이다.

        예외는 **401 하나뿐**이다. 401 은 인증 계층에서 거절된 것이라 주문 처리에
        도달하지 못했음이 확실하므로, 토큰을 새로 받아 한 번만 다시 보낸다.
        (토스는 클라이언트당 유효 토큰이 1개라 다른 프로세스가 재발급하면
        이쪽 토큰이 무효화된다 — 이 재시도가 없으면 주문이 401 로 실패한다.)
        """
        limiter = self._limiters.setdefault(group, _RateLimiter(RATE_LIMITS.get(group, 5)))
        for attempt in range(2):
            limiter.acquire()
            token = self._access_token()
            resp = self.session.post(f"{BASE_URL}{path}", json=body,
                                     headers=self._headers(token, account),
                                     timeout=self.timeout)
            if resp.status_code in (200, 201):
                return resp.json().get("result")
            if resp.status_code == 401 and attempt == 0:
                log.info("토큰이 무효화돼 재발급 후 1회 재시도합니다 (%s)", path)
                self._invalidate_token()
                continue
            body_err = _safe_json(resp).get("error", {})
            raise TossAPIError(resp.status_code, body_err.get("code", "unknown"),
                               body_err.get("message", resp.text[:200]))
        raise TossAPIError(401, "invalid-token", "토큰 재발급 후에도 인증에 실패했습니다.")

    def _headers(self, token: str, account: int | None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if account is not None:
            headers["X-Tossinvest-Account"] = str(account)
        return headers

    # --------------------------------------------------------------- market
    def candles(self, symbol: str, interval: str = "1d", count: int = 200,
                before: str | None = None, adjusted: bool = True) -> list[dict]:
        """캔들(OHLCV) 조회. 최대 200봉. interval: '1d' | '1m'."""
        result = self.get("/api/v1/candles", {
            "symbol": symbol, "interval": interval, "count": min(count, 200),
            "before": before, "adjusted": str(adjusted).lower(),
        }, group="MARKET_DATA_CHART")
        return (result or {}).get("candles", [])

    def candles_paged(self, symbol: str, interval: str = "1d", total: int = 400,
                      adjusted: bool = True) -> list[dict]:
        """200봉 초과가 필요할 때 nextBefore 로 페이지네이션하며 이어붙인다."""
        out: list[dict] = []
        before: str | None = None
        while len(out) < total:
            result = self.get("/api/v1/candles", {
                "symbol": symbol, "interval": interval,
                "count": min(200, total - len(out)), "before": before,
                "adjusted": str(adjusted).lower(),
            }, group="MARKET_DATA_CHART") or {}
            chunk = result.get("candles", [])
            if not chunk:
                break
            out.extend(chunk)
            before = result.get("nextBefore")
            if not before:
                break
        return out

    def index_candles(self, symbol: str = "KOSDAQ", interval: str = "1d",
                      count: int = 200, before: str | None = None) -> dict:
        """시장 지표(지수) 캔들. 심볼 카탈로그: KOSPI, KOSDAQ, KR_BOND_* 등."""
        return self.get(f"/api/v1/market-indicators/{symbol}/candles", {
            "interval": interval, "count": min(count, 200), "before": before,
        }, group="MARKET_INDICATOR_CHART") or {}

    def index_candles_paged(self, symbol: str = "KOSDAQ", interval: str = "1d",
                            total: int = 400) -> list[dict]:
        out: list[dict] = []
        before: str | None = None
        while len(out) < total:
            page = self.index_candles(symbol, interval, min(200, total - len(out)), before)
            chunk = page.get("candles", [])
            if not chunk:
                break
            out.extend(chunk)
            before = page.get("nextBefore")
            if not before:
                break
        return out

    def stocks(self, symbols: Iterable[str]) -> list[dict]:
        """종목 기본 정보 (최대 200건 콤마 구분)."""
        syms = list(symbols)
        out: list[dict] = []
        for i in range(0, len(syms), 200):
            out.extend(self.get("/api/v1/stocks",
                                {"symbols": ",".join(syms[i:i + 200])},
                                group="STOCK") or [])
        return out

    def stocks_all(self, market: str, status: str = "ACTIVE",
                   security_type: str | None = "STOCK",
                   common_share: bool | None = True) -> list[dict]:
        """마켓 전체 종목 유니버스 (일 1회 조회 후 캐싱 권장)."""
        return self.get("/api/v1/stocks/all", {
            "market": market, "status": status, "securityType": security_type,
            "commonShare": None if common_share is None else str(common_share).lower(),
        }, group="STOCK_ALL") or []

    def prices(self, symbols: Iterable[str]) -> list[dict]:
        """현재가 조회 (최대 200건)."""
        syms = list(symbols)
        out: list[dict] = []
        for i in range(0, len(syms), 200):
            out.extend(self.get("/api/v1/prices",
                                {"symbols": ",".join(syms[i:i + 200])},
                                group="MARKET_DATA") or [])
        return out

    def rankings(self, type_: str = "MARKET_TRADING_AMOUNT", market_country: str = "KR",
                 duration: str = "1d", count: int = 100,
                 exclude_caution: bool = True) -> list[dict]:
        """거래대금·거래량·등락률 랭킹.

        `count` 는 API 상한이 100 이며 초과 시 400 invalid-request 가 나므로 클램프한다.
        응답 payload 의 목록 키는 `rankings` 다 (`items` 아님).
        """
        result = self.get("/api/v1/rankings", {
            "type": type_, "marketCountry": market_country, "duration": duration,
            "count": max(1, min(int(count), 100)),
            "excludeInvestmentCaution": str(exclude_caution).lower(),
        }, group="RANKING") or {}
        return result.get("rankings", [])

    # ------------------------------------------------------- 계좌 · 주문
    def accounts(self) -> list[dict]:
        """계좌 목록. `accountSeq` 를 X-Tossinvest-Account 헤더에 쓴다."""
        return self.get("/api/v1/accounts", group="ACCOUNT") or []

    def buying_power(self, account_seq: int, currency: str = "KRW") -> dict:
        """현금 매수 가능 금액. `currency` 는 **필수** 파라미터다 (빠지면 400 invalid-request).

        종목별 조회는 지원하지 않는다 — 응답은 `{"currency", "cashBuyingPower"}` 뿐이다.
        """
        return self.get("/api/v1/buying-power", {"currency": currency},
                        group="ORDER_INFO", account=account_seq) or {}

    def holdings(self, account_seq: int, symbol: str | None = None) -> dict:
        """보유 종목. `symbol` 을 주면 그 종목만 (요약도 그 종목 기준으로 재계산된다)."""
        return self.get("/api/v1/holdings", {"symbol": symbol},
                        group="ASSET", account=account_seq) or {}

    def sellable_quantity(self, account_seq: int, symbol: str) -> dict:
        """매도 가능 수량 (미수·결제 미도래 물량을 뺀 실제 처분 가능분)."""
        return self.get("/api/v1/sellable-quantity", {"symbol": symbol},
                        group="ORDER_INFO", account=account_seq) or {}

    def create_order(self, account_seq: int, body: dict) -> dict:
        """주문 생성 (POST /api/v1/orders). **실거래**."""
        return self.post("/api/v1/orders", body, group="ORDER", account=account_seq) or {}

    def get_order(self, account_seq: int, order_id: str) -> dict:
        return self.get(f"/api/v1/orders/{order_id}", group="ORDER_HISTORY",
                        account=account_seq) or {}

    def cancel_order(self, account_seq: int, order_id: str) -> dict:
        return self.post(f"/api/v1/orders/{order_id}/cancel", {}, group="ORDER",
                         account=account_seq) or {}

    def create_conditional_order(self, account_seq: int, body: dict) -> dict:
        """조건주문 생성 (OCO = 익절/손절 동시 감시). **실거래**."""
        return self.post("/api/v1/conditional-orders", body,
                         group="CONDITIONAL_ORDER", account=account_seq) or {}

    def warnings(self, symbol: str) -> list[dict]:
        """매수 유의사항 (정리매매·단기과열·투자경고/위험·VI·신주인수권).

        응답은 **배열**이며, 활성 항목이 없으면 빈 배열이다.
        """
        return self.get(f"/api/v1/stocks/{symbol}/warnings", group="STOCK") or []

    def market_calendar_kr(self) -> dict:
        return self.get("/api/v1/market-calendar/KR", group="MARKET_INFO") or {}


def _safe_json(resp: requests.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {}
