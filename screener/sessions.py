"""브라우저 세션별 토스 OpenAPI 자격증명 관리.

서버 `.env` 에 고정된 키 하나를 모두가 쓰는 대신, 접속자가 대시보드 상단
**설정** 버튼에서 각자의 `client_id` / `client_secret` 을 입력하면
그 자격증명 전용 클라이언트·로더·스캐너를 만들어 세션에 물려준다.

보관 원칙
- 자격증명은 **서버 메모리에만** 둔다. 디스크에 쓰지 않고, 응답으로도 되돌려주지 않는다
  (화면에는 마스킹된 client_id 만 보여준다).
- 브라우저는 임의의 세션 ID 쿠키(`sf_session`)만 갖는다.
- 유휴 세션은 TTL 이 지나면 스캔 스레드를 멈추고 통째로 폐기한다.

토스는 **클라이언트당 유효 토큰 1개**라 자격증명이 다르면 토큰도 별개다.
토큰 캐시 파일을 `client_id` 해시로 분리해 서로 무효화하지 않게 한다.
일봉 캐시(`cache/candles`)는 시세라 사용자와 무관하므로 공유해 API 호출을 아낀다.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .autobuy import AutoBuyer
from .bollinger import BollingerConfig
from .bollinger_bot import BollingerBot
from .friday import FridayConfig, FridayRecorder, FridayScanner, FridayService
from .supply import SupplyConfig, SupplyScanner, SupplyService
from .config import StrategyConfig
from .data_loader import TossDataLoader
from .notifier import DedupeNotifier, EmailNotifier, MultiNotifier, TelegramNotifier, parse_emails
from .scan_service import ScanService
from .screener import Screener
from .toss_client import TossAPIError, TossClient, token_cache_path
from .trading import TradingService

log = logging.getLogger(__name__)

COOKIE_NAME = "sf_session"
DEFAULT_IDLE_TTL = 6 * 3600.0     # 6시간 동안 요청이 없으면 세션 폐기
DEFAULT_MAX_SESSIONS = 8          # 동시 세션 상한 (세션마다 스캔 스레드가 하나 뜬다)


class SessionError(RuntimeError):
    """사용자에게 그대로 보여줄 세션/자격증명 오류."""


@dataclass
class SessionSettings:
    """설정 창에서 받은 값. 자격증명 외에는 전부 선택 항목이다."""

    client_id: str
    client_secret: str
    top: int = 100
    interval: int = 300
    limit: int | None = None
    live: bool = False
    account_seq: int | None = None
    max_order_amount: float = 1_000_000.0
    trading_opt_in: bool = False          # 서버가 실거래를 허용할 때만 의미가 있다
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # 세력추정매매 새 시그널 이메일 알림 — 여러 주소를 콤마/줄바꿈으로 넣는다.
    # 발신 계정은 서버 공용(MAIL_SMTP_*), 여기엔 받는 주소만 둔다.
    alert_emails: str = ""
    # 세력추정매매 자동 매수 — 점수 컷오프 이상 시그널을 금액 안에서 최대 수량으로 산다.
    # 실주문은 trading_opt_in(+서버 스위치)일 때만, 아니면 모의로 기록만 한다.
    auto_buy: bool = False
    auto_buy_score: int = 75
    auto_buy_amount: int = 0

    @classmethod
    def from_body(cls, body: dict, *, defaults: "SessionSettings | None" = None) -> "SessionSettings":
        """요청 본문 → 설정 (검증 포함). 예상 밖의 키는 버린다."""
        cid = str(body.get("client_id", "")).strip()
        secret = str(body.get("client_secret", "")).strip()
        if not cid or not secret:
            raise SessionError("client_id 와 client_secret 을 모두 입력하세요.")
        if not re.fullmatch(r"[\w.\-]{8,128}", cid):
            raise SessionError("client_id 형식이 올바르지 않습니다.")
        if len(secret) < 8 or len(secret) > 256:
            raise SessionError("client_secret 형식이 올바르지 않습니다.")
        d = defaults or cls(client_id="", client_secret="")
        return cls(
            client_id=cid,
            client_secret=secret,
            top=_int(body.get("top"), d.top, lo=0, hi=1000),
            interval=_int(body.get("interval"), d.interval, lo=30, hi=3600),
            limit=(_int(body.get("limit"), 0, lo=0, hi=5000) or None),
            live=bool(body.get("live", d.live)),
            account_seq=(_int(body.get("account_seq"), 0, lo=0, hi=10**12) or None),
            max_order_amount=float(_int(body.get("max_order_amount"),
                                        int(d.max_order_amount), lo=0, hi=10**10)),
            trading_opt_in=bool(body.get("trading_opt_in", d.trading_opt_in)),
            telegram_bot_token=str(body.get("telegram_bot_token",
                                             d.telegram_bot_token)).strip()[:200],
            telegram_chat_id=str(body.get("telegram_chat_id",
                                          d.telegram_chat_id)).strip()[:64],
            alert_emails=", ".join(parse_emails(
                body.get("alert_emails", d.alert_emails))[:20])
                if "alert_emails" in body else d.alert_emails,
            auto_buy=bool(body.get("auto_buy", d.auto_buy)),
            auto_buy_score=_int(body.get("auto_buy_score"), d.auto_buy_score, lo=0, hi=100),
            auto_buy_amount=_int(body.get("auto_buy_amount"), d.auto_buy_amount,
                                 lo=0, hi=10**10),
        )


def _int(value, default: int, *, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def mask(text: str, keep: int = 4) -> str:
    """client_id 표시용 마스킹 — 앞 접두사와 뒤 4자리만 남긴다."""
    if not text:
        return ""
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:6]}…{text[-keep:]}"


@dataclass
class UserSession:
    """한 접속자의 실행 컨텍스트 (자격증명 전용 로더·스캐너·주문 서비스)."""

    sid: str
    settings: SessionSettings
    client: TossClient
    service: ScanService
    trading: TradingService | None = None
    bollinger: BollingerBot | None = None
    supply: SupplyService | None = None
    friday: FridayService | None = None
    # 이 세션을 만든 로그인 사용자. 로그인을 안 쓰는 서버면 None.
    # 웹은 쿠키의 세션이라도 owner 가 현재 로그인 사용자와 다르면 **쓰지 않는다**
    # (같은 브라우저에서 계정을 바꿔 로그인했을 때 남의 키·계좌로 주문하는 사고 방지).
    owner: str | None = None
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_seen = time.time()

    def close(self) -> None:
        self.service.stop()
        if self.bollinger is not None:
            self.bollinger.stop()
        if self.supply is not None:
            self.supply.stop()
        if self.friday is not None:
            self.friday.stop()

    def info(self) -> dict:
        """화면에 돌려줄 정보. **secret 은 절대 포함하지 않는다.**"""
        s = self.settings
        return {
            "configured": True,
            "source": "session",
            "client_id_masked": mask(s.client_id),
            "top": s.top, "interval": s.interval, "limit": s.limit, "live": s.live,
            "account_seq": s.account_seq,
            "max_order_amount": s.max_order_amount,
            "trading_opt_in": s.trading_opt_in,
            "telegram": bool(s.telegram_bot_token and s.telegram_chat_id),
            "alert_emails": s.alert_emails,
            "email_count": len(parse_emails(s.alert_emails)),
            "auto_buy": s.auto_buy,
            "auto_buy_score": s.auto_buy_score,
            "auto_buy_amount": s.auto_buy_amount,
            "auto_buy_live": bool(self.service.auto_buyer and self.trading
                                  and self.trading.enabled),
            "bollinger_running": bool(self.bollinger and self.bollinger.state.running),
            "created_at": int(self.created_at),
        }

    def idle(self, now: float, ttl: float) -> bool:
        """유휴 폐기 대상인가. 자동매매 봇이 도는 세션은 화면을 안 봐도 살려 둔다."""
        if self.bollinger is not None and self.bollinger.state.running:
            return False
        return now - self.last_seen > ttl


class SessionManager:
    """세션 생성·조회·만료. 스레드 안전."""

    def __init__(self, cfg: StrategyConfig, *, cache_dir: Path,
                 workers: int = 8, cache_ttl: float = 3600.0,
                 trading_enabled: bool = False,
                 max_order_amount: float = 1_000_000.0,
                 order_log: Path | None = None,
                 bollinger_log: Path | None = None,
                 idle_ttl: float = DEFAULT_IDLE_TTL,
                 max_sessions: int = DEFAULT_MAX_SESSIONS,
                 supply_interval: int = 60,
                 friday_cfg: FridayConfig | None = None,
                 friday_log: Path | None = None,
                 mail: dict | None = None,
                 client_factory=TossClient):
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.workers = workers
        self.cache_ttl = cache_ttl
        self.trading_enabled = trading_enabled          # 서버 마스터 스위치
        self.max_order_amount = max_order_amount        # 서버 상한 (사용자는 더 낮게만 가능)
        # 서버 공용 이메일 발신 계정 (수신 주소만 사용자 설정). None 이면 이메일 알림 비활성.
        self.mail = mail if (mail and mail.get("host") and mail.get("user")
                             and mail.get("password")) else None
        self.order_log = Path(order_log) if order_log else None
        self.bollinger_log = Path(bollinger_log) if bollinger_log else None
        self.idle_ttl = idle_ttl
        self.max_sessions = max_sessions
        self.supply_interval = supply_interval
        self.friday_cfg = friday_cfg or FridayConfig()
        self.friday_log = Path(friday_log) if friday_log else None
        self._client_factory = client_factory
        self._sessions: dict[str, UserSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- 조회
    def get(self, sid: str | None) -> UserSession | None:
        if not sid:
            return None
        with self._lock:
            sess = self._sessions.get(sid)
        if sess is None:
            return None
        if sess.idle(time.time(), self.idle_ttl):
            self.drop(sid)
            return None
        sess.touch()
        return sess

    def drop(self, sid: str | None) -> bool:
        if not sid:
            return False
        with self._lock:
            sess = self._sessions.pop(sid, None)
        if sess is None:
            return False
        sess.close()
        log.info("세션 종료 (%s)", mask(sess.settings.client_id))
        return True

    def sweep(self) -> None:
        """유휴 세션 정리."""
        now = time.time()
        with self._lock:
            stale = [sid for sid, s in self._sessions.items() if s.idle(now, self.idle_ttl)]
            dropped = [self._sessions.pop(sid) for sid in stale]
        for sess in dropped:
            sess.close()

    def stop_all(self) -> None:
        with self._lock:
            sessions, self._sessions = list(self._sessions.values()), {}
        for sess in sessions:
            sess.close()

    # ------------------------------------------------------------- 생성
    def log_paths(self, owner: str | None) -> tuple[Path | None, Path | None]:
        """(주문 로그, 볼린저 로그). 로그인 사용자면 `<로그 폴더>/users/<아이디>/` 로 분리한다."""
        if not owner:
            return self.order_log, self.bollinger_log
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", owner).lstrip(".") or "_"
        order = (self.order_log.parent / "users" / safe / self.order_log.name
                 if self.order_log else None)
        bb = (self.bollinger_log.parent / "users" / safe / self.bollinger_log.name
              if self.bollinger_log else None)
        return order, bb

    def friday_path(self, owner: str | None) -> Path | None:
        """금요일 마감 신호 기록 파일. 로그인 사용자면 `<로그 폴더>/users/<아이디>/` 로 분리한다."""
        if not self.friday_log:
            return None
        if not owner:
            return self.friday_log
        safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", owner).lstrip(".") or "_"
        return self.friday_log.parent / "users" / safe / self.friday_log.name

    def create(self, settings: SessionSettings, *, owner: str | None = None) -> UserSession:
        """자격증명을 검증하고 세션을 만든다. 기존 동일 세션은 호출부에서 먼저 drop 한다."""
        self.sweep()
        with self._lock:
            if len(self._sessions) >= self.max_sessions:
                raise SessionError(
                    f"동시 사용자 수 상한({self.max_sessions}명)에 도달했습니다. "
                    "잠시 후 다시 시도하세요.")

        client = self._client_factory(
            settings.client_id, settings.client_secret,
            token_cache=token_cache_path(self.cache_dir, settings.client_id))
        self._verify(client)

        order_log, bollinger_log = self.log_paths(owner)
        loader = TossDataLoader(client, markets=self.cfg.markets,
                                cache_dir=self.cache_dir, cache_ttl=self.cache_ttl)
        telegram = None
        channels: list = []
        if settings.telegram_bot_token and settings.telegram_chat_id:
            telegram = TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id)
            channels.append(telegram)
        emails = parse_emails(settings.alert_emails)
        if emails and self.mail:
            channels.append(self._email_notifier(emails))
        # 텔레그램·이메일을 한 래퍼로 묶고 중복 시그널을 한 번만 보낸다.
        notifier = DedupeNotifier(MultiNotifier(*channels)) if channels else None
        service = ScanService(Screener(loader, self.cfg, max_workers=self.workers),
                              interval=settings.interval, top=settings.top,
                              limit=settings.limit, live=settings.live,
                              notifier=notifier)
        trading = TradingService(
            client,
            # 실거래는 **서버 스위치와 사용자 동의가 모두 있을 때만** 켜진다.
            enabled=self.trading_enabled and settings.trading_opt_in,
            max_order_amount=min(settings.max_order_amount, self.max_order_amount),
            account_seq=settings.account_seq,
            log_path=order_log,
        )

        if settings.auto_buy and settings.auto_buy_amount > 0:
            service.auto_buyer = AutoBuyer(
                trading, min_score=settings.auto_buy_score, amount=settings.auto_buy_amount,
                notify=telegram.send_text if telegram else None)

        # 볼린저 봇은 만들어만 두고 **자동 시작하지 않는다** — 주문을 내는 루프라
        # 사용자가 화면에서 명시적으로 시작해야 한다.
        bot = BollingerBot(client, trading, BollingerConfig(),
                           notify=telegram.send_text if telegram else None,
                           log_path=bollinger_log)

        # 수급 포착 매매 — 시세만 보므로 주문과 무관하다. 장중에만 촘촘히 돈다.
        supply = SupplyService(SupplyScanner(loader, SupplyConfig(), max_workers=self.workers),
                               interval=self.supply_interval)

        # 금요일 마감 돌파 — 시세만 보고 주문과 무관하다. 기록 창(금요일 14:25~15:20)에만 촘촘히 돈다.
        friday = FridayService(
            FridayScanner(loader, self.friday_cfg, max_workers=self.workers,
                          recorder=FridayRecorder(self.friday_path(owner))),
            interval=self.supply_interval)

        sess = UserSession(sid=secrets.token_urlsafe(24), settings=settings,
                           client=client, service=service, trading=trading,
                           bollinger=bot, supply=supply, friday=friday, owner=owner)
        with self._lock:
            self._sessions[sess.sid] = sess
        service.start()
        supply.start()
        friday.start()
        log.info("세션 시작 (%s%s, 상위 %d종목 · %d초 주기)",
                 mask(settings.client_id), f" · {owner}" if owner else "",
                 settings.top, settings.interval)
        return sess

    # ------------------------------------------------------------- 내부
    def _email_notifier(self, recipients: list[str]) -> EmailNotifier:
        """서버 공용 발신 계정으로 보내는 이메일 채널을 만든다 (수신자만 사용자별)."""
        m = self.mail
        return EmailNotifier(recipients, host=m["host"], port=m.get("port", 587),
                             user=m["user"], password=m["password"],
                             crypto=m.get("crypto", "tls"),
                             from_name=m.get("from_name", "왔니 주식"))

    def _verify(self, client: TossClient) -> None:
        """실제 호출로 자격증명을 검증한다 (토큰 발급 + 가벼운 시세 조회)."""
        try:
            client.prices(["005930"])
        except TossAPIError as exc:
            if exc.status in (400, 401, 403):
                raise SessionError(
                    f"자격증명 확인 실패 [{exc.status} {exc.code}]. "
                    "키 값과 토스증권에 등록한 허용 IP 를 확인하세요.") from exc
            raise SessionError(f"토스 API 오류: {exc}") from exc
        except Exception as exc:
            raise SessionError(f"토스 API 연결 실패: {exc}") from exc

