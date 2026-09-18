"""웹 대시보드 (FastAPI).

백그라운드 스캐너 스레드가 주기적으로 유니버스를 스캔하고, 최신 결과를
메모리에 보관한다. 브라우저는 JSON API 를 폴링해 화면을 갱신한다.

자격증명은 두 가지 경로가 있다.
1. 서버 `.env` (`TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`) — 있으면 기본 세션으로 동작한다.
   시세 화면(스캔·수급)은 모든 로그인 사용자가 함께 보지만, **서버 계좌로 주문하는
   기능(주문·볼린저 봇)은 관리자 계정만** 쓸 수 있다.
2. 접속자가 상단 **설정** 창에 입력한 자기 키 — `SessionManager` 가 세션별 스캐너를 띄운다.
   세션 쿠키(`sf_session`)를 가진 요청은 자기 세션 쪽을 보되, 세션 소유자(owner)가
   현재 로그인 계정과 다르면 무시한다.

실행:  python main.py web --port 8083
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .bollinger import BollingerConfig
from .bollinger_bot import BollingerBot
from .supply import SupplyConfig, SupplyService, SupplyState
from .config import StrategyConfig
from .data_loader import DataLoader
from .notifier import Notifier, TelegramError, TelegramNotifier
from .scan_service import ScanService, ScanState, _now
from .screener import Screener
from .sessions import COOKIE_NAME, SessionError, SessionManager, SessionSettings, UserSession
from .store import Store, StoreError, validate_password
from .trading import TradingError, TradingService

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

AUTH_COOKIE = "sf_auth"
# 로그인 없이 열리는 경로 (로그인 화면 자체와 헬스체크). `/static/**` 도 공개다.
PUBLIC_PATHS = frozenset({"/login", "/api/login", "/api/signup", "/api/auth/options",
                          "/healthz", "/favicon.ico"})

# 로그인 무차별 대입 방어: (IP, 아이디) 당 LOGIN_WINDOW 초 안에 LOGIN_MAX_FAILS 회 실패하면 잠근다.
LOGIN_WINDOW = 300.0
LOGIN_MAX_FAILS = 5

__all__ = ["ScanService", "ScanState", "create_app"]

# 설정 창(`POST /api/session/settings`)이 바꿀 수 있는 항목 — **자격증명은 없다.**
_SETTING_KEYS = frozenset({
    "top", "interval", "limit", "live", "account_seq", "max_order_amount",
    "trading_opt_in", "telegram_bot_token", "telegram_chat_id",
    "auto_buy", "auto_buy_score", "auto_buy_amount",
})


# 볼린저 설정 중 DB 에 저장하지 않는 항목. 기존 보유분 청산 스위치는 안전장치라
# 서버 재시작 뒤에는 항상 기본값(False)으로 돌아가게 둔다.
BB_NOT_PERSISTED = frozenset({"manage_existing"})


# ================================================================== FastAPI
def create_app(loader: DataLoader | None = None, cfg: StrategyConfig | None = None, *,
               interval: int = 300, top: int = 200, limit: int | None = None,
               live: bool = False, notifier: Notifier | None = None, workers: int = 8,
               trading: TradingService | None = None,
               sessions: SessionManager | None = None,
               bollinger: BollingerBot | None = None,
               supply: SupplyService | None = None,
               store: Store | None = None,
               signup: bool = False, signup_code: str = "") -> FastAPI:
    cfg = cfg or StrategyConfig.tuned()
    service = ScanService(Screener(loader, cfg, max_workers=workers),
                          interval=interval, top=top, limit=limit,
                          live=live, notifier=notifier) if loader is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if service:
            service.start()
        if supply:
            supply.start()
        log.info("대시보드 준비 완료 (스캔 주기 %ds)", interval)
        yield
        if service:
            service.stop()
        if supply:
            supply.stop()
        if bollinger:
            bollinger.stop()
        if sessions:
            sessions.stop_all()

    app = FastAPI(title="세력 추정 매매법 스크리너", docs_url="/docs", lifespan=lifespan)
    app.state.service = service
    app.state.trading = trading
    app.state.sessions = sessions
    app.state.bollinger = bollinger
    app.state.supply = supply
    app.state.store = store
    # 로그인 사용자 → 살아 있는 토스 세션 sid. 서버 재시작·쿠키 분실 후에도
    # 저장된 설정으로 세션을 되살리기 위한 지도다.
    user_sids: dict[str, str] = {}
    restore_lock = threading.Lock()
    restore_failed: dict[str, float] = {}      # 복구 실패 시 재시도 쿨다운(60초)
    login_fails: dict[str, list[float]] = {}   # "IP|아이디" → 최근 실패 시각
    fails_lock = threading.Lock()
    # 공통 GNB·테마·설정 모듈 (외부 CDN 없이 로컬 정적 파일로만 제공한다)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ----------------------------------------------------------- 로그인
    def _user(request: Request) -> str | None:
        """이 요청의 로그인 사용자. 로그인 기능이 꺼져 있으면 None."""
        if store is None:
            return None
        return store.user_for_token(request.cookies.get(AUTH_COOKIE))

    def _is_admin(request: Request) -> bool:
        """서버 `.env` 계좌를 만질 수 있는가. 로그인을 안 쓰는 서버는 단일 운영자로 본다."""
        if store is None:
            return True
        return store.is_admin(_user(request))

    def _server_trading(request: Request) -> TradingService | None:
        return trading if _is_admin(request) else None

    def _server_bot(request: Request) -> BollingerBot | None:
        return bollinger if _is_admin(request) else None

    # ------------------------------------------------ 볼린저 설정 영속화
    def _bb_load(owner: str, bot: BollingerBot | None) -> None:
        """DB 에 저장된 볼린저 설정(감시 목록 포함)을 봇에 적용한다."""
        if store is None or bot is None:
            return
        saved = store.load_bollinger(owner)
        if not saved:
            return
        # 안전장치는 저장값으로 되살리지 않는다 — 재시작 뒤에는 항상 기본(False)부터.
        body = {k: v for k, v in saved.items() if k not in BB_NOT_PERSISTED}
        try:
            cfg = BollingerConfig.from_body(body)
        except ValueError as exc:
            log.warning("저장된 볼린저 설정을 읽지 못했습니다 (%s): %s", owner, exc)
            return
        bot.update_config(cfg, poll_interval=saved.get("poll_interval"))

    def _bb_owner(request: Request) -> str | None:
        """이 요청의 볼린저 봇 설정을 저장할 키. 로그인을 안 쓰면 저장하지 않는다."""
        if store is None:
            return None
        if _session(request) is not None:
            user = _user(request)
            return f"user:{user}" if user else None
        return "server" if _is_admin(request) else None

    def _bb_save(request: Request, bot: BollingerBot) -> bool:
        owner = _bb_owner(request)
        if owner is None:
            return False
        values = {k: v for k, v in bot.cfg.describe().items() if k not in BB_NOT_PERSISTED}
        store.save_bollinger(owner, {**values, "poll_interval": bot.poll_interval})
        return True

    _bb_load("server", bollinger)

    def _restore(username: str) -> UserSession | None:
        """DB 에 저장된 설정으로 토스 세션을 되살린다 (이미 있으면 그대로)."""
        if sessions is None:
            return None
        with restore_lock:
            sess = sessions.get(user_sids.get(username))
            if sess is not None:
                return sess
            if time.time() - restore_failed.get(username, 0.0) < 60.0:
                return None                     # 직전 복구 실패 — 매 요청마다 두드리지 않는다
            saved = store.load_settings(username) if store else None
            if not saved or not saved.get("client_id") or not saved.get("client_secret"):
                return None
            try:
                sess = sessions.create(SessionSettings.from_body(saved), owner=username)
            except SessionError as exc:
                restore_failed[username] = time.time()
                log.warning("저장된 설정으로 세션 복구 실패 (%s): %s", username, exc)
                return None
            user_sids[username] = sess.sid
            restore_failed.pop(username, None)
            _bb_load(f"user:{username}", sess.bollinger)
            log.info("저장된 설정으로 세션 복구 (%s)", username)
            return sess

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        """로그인 기능이 켜져 있으면 모든 화면·API 를 막는다."""
        if store is not None:
            path = request.url.path
            public = path in PUBLIC_PATHS or path.startswith("/static/")
            if not public and store.user_for_token(request.cookies.get(AUTH_COOKIE)) is None:
                if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                    return RedirectResponse("/login", status_code=302)
                return JSONResponse({"detail": "로그인이 필요합니다."}, status_code=401)
        return await call_next(request)

    @app.get("/login", include_in_schema=False)
    def login_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "login.html")

    def _login_response(username: str) -> JSONResponse:
        token = store.create_token(username)
        resp = JSONResponse({"ok": True, "username": username,
                             "is_admin": store.is_admin(username)})
        resp.set_cookie(AUTH_COOKIE, token, httponly=True, samesite="lax",
                        max_age=int(30 * 24 * 3600), path="/")
        return resp

    def _throttle_key(request: Request, username: str) -> str:
        ip = request.client.host if request.client else "-"
        return f"{ip}|{username.lower()}"

    def _recent_fails(key: str) -> list[float]:
        now = time.time()
        with fails_lock:
            recent = [t for t in login_fails.get(key, []) if now - t < LOGIN_WINDOW]
            if recent:
                login_fails[key] = recent
            else:
                login_fails.pop(key, None)
            return recent

    @app.get("/api/auth/options")
    def auth_options() -> JSONResponse:
        """로그인 화면용 — 회원가입 가능 여부와 초대 코드 필요 여부."""
        return JSONResponse({"login_required": store is not None,
                             "signup": bool(store is not None and signup),
                             "signup_code_required": bool(signup_code)})

    @app.post("/api/signup")
    def signup_(request: Request, body: dict) -> JSONResponse:
        """회원가입 — 일반 사용자 계정을 만들고 바로 로그인시킨다."""
        if store is None or not signup:
            raise HTTPException(status_code=403, detail="이 서버는 회원가입을 받지 않습니다.")
        key = _throttle_key(request, "#signup")
        if len(_recent_fails(key)) >= LOGIN_MAX_FAILS:
            raise HTTPException(status_code=429, detail="시도가 너무 많습니다. 잠시 후 다시 시도하세요.")
        if signup_code and not secrets.compare_digest(
                str(body.get("code", "")).strip().encode(), signup_code.encode()):
            with fails_lock:
                login_fails.setdefault(key, []).append(time.time())
            raise HTTPException(status_code=403, detail="초대 코드가 올바르지 않습니다.")
        try:
            username = store.create_user(str(body.get("username", ""))[:64],
                                         str(body.get("password", ""))[:256])
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        log.info("회원가입: %s", username)
        resp = _login_response(username)
        resp.delete_cookie(COOKIE_NAME, path="/")        # 이전 사용자의 세션 쿠키가 남지 않게
        return resp

    @app.post("/api/login")
    def login(request: Request, body: dict) -> JSONResponse:
        if store is None:
            raise HTTPException(status_code=503, detail="이 서버는 로그인을 쓰지 않습니다.")
        username = str(body.get("username", "")).strip()[:64]
        password = str(body.get("password", ""))[:256]
        if not username or not password:
            raise HTTPException(status_code=400, detail="아이디와 비밀번호를 입력하세요.")
        key = _throttle_key(request, username)
        if len(_recent_fails(key)) >= LOGIN_MAX_FAILS:
            raise HTTPException(status_code=429,
                                detail="로그인 실패가 반복돼 잠시 막았습니다. 5분 뒤 다시 시도하세요.")
        if not store.verify(username, password):
            with fails_lock:
                login_fails.setdefault(key, []).append(time.time())
            raise HTTPException(status_code=401, detail="아이디 또는 비밀번호가 올바르지 않습니다.")
        with fails_lock:
            login_fails.pop(key, None)
        resp = _login_response(username)
        # 저장된 설정이 있으면 곧바로 스캐너를 되살려 쿠키까지 붙여 준다.
        sess = _restore(username)
        if sess is not None and sessions is not None:
            resp.set_cookie(COOKIE_NAME, sess.sid, httponly=True, samesite="lax",
                            max_age=int(sessions.idle_ttl), path="/")
        else:
            # 같은 브라우저에 다른 계정의 세션 쿠키가 남아 있을 수 있다 — 지운다.
            resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.post("/api/logout")
    def logout(request: Request) -> JSONResponse:
        """로그아웃. 저장된 설정은 지우지 않는다 (다시 로그인하면 복구된다)."""
        if store is not None:
            store.delete_token(request.cookies.get(AUTH_COOKIE))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(AUTH_COOKIE, path="/")
        # 세션(스캔·봇)은 살려 두되 이 브라우저와의 연결만 끊는다 — 다음 사람이 이어받지 못하게.
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/api/me")
    def me(request: Request) -> JSONResponse:
        user = _user(request)
        return JSONResponse({"login_required": store is not None,
                             "username": user,
                             "is_admin": bool(store and store.is_admin(user))})

    @app.post("/api/account/password")
    def account_password(request: Request, body: dict) -> JSONResponse:
        """비밀번호 변경. 성공하면 **다른 기기의 로그인은 모두 끊는다.**"""
        user = _user(request)
        if store is None or not user:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
        if not store.verify(user, str(body.get("current", ""))[:256]):
            raise HTTPException(status_code=400, detail="현재 비밀번호가 올바르지 않습니다.")
        try:
            new = validate_password(str(body.get("new", ""))[:256])
        except StoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.set_password(user, new)
        revoked = store.delete_user_tokens(user, keep=request.cookies.get(AUTH_COOKIE))
        return JSONResponse({"ok": True, "revoked": revoked})

    @app.delete("/api/account")
    def account_delete(request: Request, body: dict) -> JSONResponse:
        """회원 탈퇴 — 세션(스캔·봇)을 멈추고 계정과 저장값을 모두 지운다."""
        user = _user(request)
        if store is None or not user:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
        if not store.verify(user, str(body.get("password", ""))[:256]):
            raise HTTPException(status_code=400, detail="비밀번호가 올바르지 않습니다.")
        if store.is_admin(user) and store.admins() == [user]:
            raise HTTPException(status_code=400,
                                detail="마지막 관리자 계정은 삭제할 수 없습니다.")
        if sessions is not None:
            sessions.drop(user_sids.pop(user, None))
        restore_failed.pop(user, None)
        store.delete_user(user)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(AUTH_COOKIE, path="/")
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    # ------------------------------------------------------- 세션 해석
    def _session(request: Request) -> UserSession | None:
        if sessions is None:
            return None
        user = _user(request)
        sess = sessions.get(request.cookies.get(COOKIE_NAME))
        # 쿠키의 세션이라도 **지금 로그인한 계정의 것**이 아니면 쓰지 않는다.
        if sess is not None and (store is None or sess.owner == user):
            return sess
        # 쿠키가 없거나 만료됐어도 로그인 사용자라면 저장된 설정으로 되살린다.
        return _restore(user) if user else None

    def _current_settings(request: Request) -> SessionSettings | None:
        """현재 사용자의 설정 (메모리 세션 > DB 저장값). 없으면 None."""
        sess = _session(request)
        if sess is not None:
            return sess.settings
        user = _user(request)
        saved = store.load_settings(user) if (user and store is not None) else None
        if not saved:
            return None
        try:
            return SessionSettings.from_body(saved)
        except SessionError:
            return None

    def _drop_own_cookie_session(request: Request) -> bool:
        """쿠키가 가리키는 세션을 정리하되, 남의 세션이면 건드리지 않는다."""
        if sessions is None:
            return False
        sess = sessions.get(request.cookies.get(COOKIE_NAME))
        if sess is None or (store is not None and sess.owner != _user(request)):
            return False
        return sessions.drop(sess.sid)

    def _service(request: Request) -> ScanService:
        """이 요청이 볼 스캐너. 세션 > 서버 기본 순."""
        sess = _session(request)
        if sess is not None:
            return sess.service
        if service is not None:
            return service
        raise HTTPException(status_code=503,
                            detail="API 키가 설정되지 않았습니다. 상단 '설정'에서 키를 입력하세요.")

    def _trading(request: Request) -> TradingService:
        sess = _session(request)
        svc = sess.trading if sess is not None else _server_trading(request)
        if svc is None:
            raise HTTPException(status_code=503, detail="주문 기능이 구성되지 않았습니다.")
        return svc

    def _bot(request: Request) -> BollingerBot:
        """이 요청이 조작할 볼린저 봇. 세션 > 서버 기본(관리자만) 순."""
        sess = _session(request)
        bot = sess.bollinger if sess is not None else _server_bot(request)
        if bot is None:
            raise HTTPException(status_code=503,
                                detail="API 키가 설정되지 않았습니다. 상단 '설정'에서 키를 입력하세요.")
        return bot

    def _supply(request: Request) -> SupplyService | None:
        """이 요청이 볼 수급 포착 스캐너. 세션 > 서버 기본 순 (스캐너와 같은 규칙)."""
        sess = _session(request)
        return sess.supply if sess is not None else supply

    def _supply_or_503(request: Request) -> SupplyService:
        svc = _supply(request)
        if svc is None:
            raise HTTPException(status_code=503,
                                detail="API 키가 설정되지 않았습니다. 상단 '설정'에서 키를 입력하세요.")
        return svc

    @app.get("/supply", include_in_schema=False)
    def supply_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "supply.html")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/bollinger", include_in_schema=False)
    def bollinger_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "bollinger.html")

    @app.get("/api/state")
    def state(request: Request) -> JSONResponse:
        sess = _session(request)
        svc = sess.service if sess is not None else service
        if svc is None:
            # 아직 아무 자격증명도 없다 — 화면은 설정 안내를 띄운다.
            return JSONResponse({**ScanState().as_dict(), "configured": False,
                                 "source": None, "interval": interval, "live": live,
                                 "top": top, "server_time": _now()})
        return JSONResponse({
            **svc.state.as_dict(),
            "configured": True,
            "source": "session" if sess is not None else "server",
            "interval": svc.interval,
            "live": svc.live,
            "top": svc.top,
            "server_time": _now(),
        })

    @app.get("/api/signals")
    def signals(request: Request) -> JSONResponse:
        return JSONResponse(_service(request).state.signals)

    @app.get("/api/watch")
    def watch(request: Request) -> JSONResponse:
        return JSONResponse(_service(request).state.watch)

    # ------------------------------------------------------------- 설정(세션)
    @app.get("/api/session")
    def session_info(request: Request) -> JSONResponse:
        """현재 요청이 어떤 자격증명으로 도는지 (secret 은 절대 내려주지 않는다)."""
        sess = _session(request)
        user = _user(request)
        body = {
            "multi_user": sessions is not None,
            "trading_allowed": bool(sessions and sessions.trading_enabled),
            "server_max_order_amount": sessions.max_order_amount if sessions else None,
            "server_default": service is not None,
            "username": user,
            "is_admin": _is_admin(request),
            # 개인 키 없이 서버 계좌로 주문·봇을 쓸 수 있는가 (관리자만)
            "server_trading": _server_trading(request) is not None,
            "persisted": store is not None,
        }
        if sess is not None:
            return JSONResponse({**body, **sess.info()})
        if service is not None:
            return JSONResponse({**body, "configured": True, "source": "server",
                                 "client_id_masked": None, "top": service.top,
                                 "interval": service.interval, "limit": service.limit,
                                 "live": service.live})
        return JSONResponse({**body, "configured": False, "source": None})

    def _open_session(request: Request, settings: SessionSettings) -> JSONResponse:
        """세션을 새로 열고(기존은 정리) 저장·쿠키까지 처리한다.
        API 키 창과 설정 창이 함께 쓴다 — 설정이 바뀌면 스캐너·봇을 다시 만들어야 한다."""
        user = _user(request)
        _drop_own_cookie_session(request)                # 재설정이면 기존 세션 정리
        if user:
            sessions.drop(user_sids.pop(user, None))
        try:
            sess = sessions.create(settings, owner=user)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # 로그인 상태면 설정을 **서버 DB 에 저장**한다 (자격증명은 암호화).
        if user and store is not None:
            store.save_settings(user, asdict(settings))
            user_sids[user] = sess.sid
            restore_failed.pop(user, None)
            _bb_load(f"user:{user}", sess.bollinger)      # 키를 다시 넣어도 감시 목록은 유지

        resp = JSONResponse({"ok": True, "persisted": bool(user and store), **sess.info()})
        resp.set_cookie(COOKIE_NAME, sess.sid, httponly=True, samesite="lax",
                        max_age=int(sessions.idle_ttl), path="/")
        return resp

    @app.post("/api/session")
    def session_create(request: Request, body: dict) -> JSONResponse:
        """API 키 창에서 받은 자격증명으로 개인 세션을 연다.

        다른 설정(스캔 범위·알림·주문)은 **저장값을 그대로 이어받는다** —
        키만 다시 넣었다고 해서 자동매수 같은 설정이 초기화되면 안 된다.
        """
        if sessions is None:
            raise HTTPException(status_code=503,
                                detail="이 서버는 개인 API 키 입력을 지원하지 않습니다.")
        try:
            settings = SessionSettings.from_body(body, defaults=_current_settings(request))
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _open_session(request, settings)

    @app.post("/api/session/settings")
    def session_settings(request: Request, body: dict) -> JSONResponse:
        """설정 창 — **자격증명은 받지 않는다.** 저장된 키로 세션을 다시 연다.

        스캔 주기·알림·주문 설정은 스캐너·봇을 만들 때 박히므로 세션을 새로 만든다
        (진행 중이던 개인 봇은 멈추고 다시 시작해야 한다).
        """
        if sessions is None:
            raise HTTPException(status_code=503,
                                detail="이 서버는 개인 API 키 입력을 지원하지 않습니다.")
        cur = _current_settings(request)
        if cur is None or not cur.client_id or not cur.client_secret:
            raise HTTPException(status_code=400,
                                detail="먼저 '내 API 키' 창에서 토스 API 키를 등록하세요.")
        merged = {**asdict(cur), **{k: v for k, v in body.items() if k in _SETTING_KEYS}}
        # 비워 둔 비밀 항목은 저장값을 유지한다 (화면에는 다시 보여주지 않으므로).
        for key in ("telegram_bot_token",):
            if not str(body.get(key, "")).strip():
                merged[key] = getattr(cur, key)
        try:
            settings = SessionSettings.from_body(merged, defaults=cur)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _open_session(request, settings)

    @app.delete("/api/session")
    def session_delete(request: Request) -> JSONResponse:
        """연결 해제 — 메모리 세션과 **저장된 설정**을 함께 지운다."""
        dropped = _drop_own_cookie_session(request)
        user = _user(request)
        if user:
            if sessions:
                dropped = sessions.drop(user_sids.pop(user, None)) or dropped
            if store is not None:
                store.clear_settings(user)
        resp = JSONResponse({"ok": True, "dropped": dropped})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.post("/api/session/telegram/test")
    def session_telegram_test(request: Request, body: dict) -> JSONResponse:
        """설정 창의 '테스트 전송'. 입력한 토큰/챗ID 로 실제 메시지를 1건 보내본다.

        입력이 비어 있으면 이미 저장된 세션 값으로 확인한다 (저장 후에는 화면에서
        토큰 칸을 비우기 때문). 값은 응답에 담지 않는다.
        """
        token = str(body.get("telegram_bot_token", "")).strip()[:200]
        chat = str(body.get("telegram_chat_id", "")).strip()[:64]
        sess = _session(request)
        if sess is not None:
            token = token or sess.settings.telegram_bot_token
            chat = chat or sess.settings.telegram_chat_id
        try:
            where = TelegramNotifier(token, chat).check()
        except TelegramError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse({"ok": True, "chat": where})

    @app.get("/api/guide")
    def guide() -> JSONResponse:
        """전략 설명 + 배점 근거 + 백테스트 실측치 (대시보드 '가이드' 탭)."""
        from . import evidence as ev
        from .scoring import ENTRY_CUTOFF, FACTORS, GRADES, REGIME_FACTOR

        factors = [{
            "항목": f.label, "배점": f.max_points,
            "구간": [{"설명": note, "점수": pts} for _, pts, note in f.bands],
        } for f in (*FACTORS, REGIME_FACTOR)]

        return JSONResponse({
            "측정일": ev.MEASURED_AT, "표본": ev.SAMPLE, "청산규칙": ev.EXIT_RULE,
            "단계": ev.STEPS, "요인": factors,
            "등급": [{"등급": g, "최소점수": cut, "판단": a} for cut, g, a in GRADES],
            "등급성과": ev.GRADE_PERFORMANCE, "컷오프": ev.CUTOFF_SUMMARY,
            "컷오프점수": ENTRY_CUTOFF,
            "프로파일": ev.PROFILES, "손절방식": ev.STOP_MODES, "국면": ev.REGIMES,
            "포지션": ev.POSITION_SIZING, "주의": ev.CAVEATS,
        })

    @app.get("/api/config")
    def config() -> JSONResponse:
        return JSONResponse({k: (list(v) if isinstance(v, tuple) else v)
                             for k, v in cfg.describe().items()})

    @app.post("/api/scan")
    def rescan(request: Request) -> JSONResponse:
        svc = _service(request)
        if svc.state.running:
            return JSONResponse({"ok": False, "message": "이미 스캔 중입니다"}, status_code=409)
        threading.Thread(target=svc.scan_once, daemon=True).start()
        return JSONResponse({"ok": True, "message": "스캔을 시작했습니다"})

    @app.get("/api/chart/{symbol}")
    def chart(request: Request, symbol: str, count: int = 60) -> JSONResponse:
        """상세 보기용 최근 일봉 (OHLCV)."""
        data_loader = _service(request).screener.loader
        try:
            df = data_loader.ohlcv(symbol, count=max(count, cfg.min_history))
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if df.empty:
            raise HTTPException(status_code=404, detail="데이터 없음")
        tail = df.tail(count)
        return JSONResponse({
            "symbol": symbol,
            "candles": [
                {"date": idx.strftime("%Y-%m-%d"), "open": float(r.open), "high": float(r.high),
                 "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
                for idx, r in tail.iterrows()
            ],
        })

    # --------------------------------------------------- 수급 포착 매매
    @app.get("/api/supply/state")
    def supply_state(request: Request) -> JSONResponse:
        svc = _supply(request)
        if svc is None:
            return JSONResponse({**SupplyState().as_dict(), "configured": False,
                                 "config": SupplyConfig().describe(), "server_time": _now()})
        # 장 밖에서는 스레드가 30분에 한 번만 돈다. 화면을 열었는데 결과가 없으면 바로 한 번 돌린다.
        if svc.state.finished_at is None and not svc.state.running:
            threading.Thread(target=svc.scan_once, daemon=True).start()
        sess = _session(request)
        return JSONResponse({
            **svc.state.as_dict(),
            "configured": True,
            "source": "session" if sess is not None else "server",
            "interval": svc.interval,
            "idle_interval": svc.idle_interval,
            "config": svc.cfg.describe(),
            "server_time": _now(),
        })

    @app.post("/api/supply/scan")
    def supply_scan(request: Request) -> JSONResponse:
        svc = _supply_or_503(request)
        if svc.state.running:
            return JSONResponse({"ok": False, "message": "이미 스캔 중입니다"}, status_code=409)
        threading.Thread(target=svc.scan_once, daemon=True).start()
        return JSONResponse({"ok": True, "message": "스캔을 시작했습니다"})

    @app.get("/api/supply/chart/{symbol}")
    def supply_chart(request: Request, symbol: str) -> JSONResponse:
        """당일 1분봉 + VWAP + 5분봉 이평 + 누적 매수세 + 평소 누적 거래량."""
        if not re.fullmatch(r"[A-Za-z0-9]{6}", symbol):
            raise HTTPException(status_code=400, detail="종목코드 형식이 아닙니다.")
        svc = _supply_or_503(request)
        try:
            data = svc.scanner.chart(symbol)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not data.get("bars"):
            raise HTTPException(status_code=404, detail="당일 분봉 데이터가 없습니다.")
        return JSONResponse({"symbol": symbol, **data})

    # --------------------------------------------------- 볼린저 반전 매매
    @app.get("/api/bollinger/state")
    def bollinger_state(request: Request) -> JSONResponse:
        sess = _session(request)
        bot = sess.bollinger if sess is not None else _server_bot(request)
        if bot is None:
            return JSONResponse({"configured": False, "running": False, "live": False,
                                 "trading_allowed": False, "decisions": [], "positions": [],
                                 "events": [], "config": BollingerConfig().describe()})
        return JSONResponse({
            "configured": True,
            "trading_allowed": bot.trading.enabled,
            "poll_interval": bot.poll_interval,
            "max_order_amount": bot.trading.max_order_amount,
            "config": bot.cfg.describe(),
            "server_time": _now(),
            **bot.state.as_dict(),
        })

    @app.post("/api/bollinger/config")
    def bollinger_config(request: Request, body: dict) -> JSONResponse:
        bot = _bot(request)
        try:
            cfg = BollingerConfig.from_body(body, bot.cfg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bot.update_config(cfg, poll_interval=body.get("poll_interval"))
        persisted = _bb_save(request, bot)
        return JSONResponse({"ok": True, "config": cfg.describe(),
                             "poll_interval": bot.poll_interval, "persisted": persisted})

    @app.post("/api/bollinger/symbols")
    def bollinger_add_symbol(request: Request, body: dict) -> JSONResponse:
        """감시 목록에 종목 1개를 덧붙인다 (세력추정매매 화면의 종목 메뉴용).

        전체 설정을 다시 보내는 `/config` 와 달리 서버에서 합치므로,
        볼린저 화면에서 동시에 고친 다른 설정을 덮어쓰지 않는다.
        """
        bot = _bot(request)
        symbol = str(body.get("symbol", "")).strip()
        if not re.fullmatch(r"\d{6}", symbol):
            raise HTTPException(status_code=400, detail="종목코드는 6자리 숫자여야 합니다.")
        current = tuple(bot.cfg.symbols)
        added = symbol not in current
        if added:
            bot.update_config(replace(bot.cfg, symbols=current + (symbol,)))
        persisted = _bb_save(request, bot)
        return JSONResponse({"ok": True, "added": added, "symbol": symbol,
                             "symbols": list(bot.cfg.symbols),
                             "running": bot.state.running, "persisted": persisted})

    @app.post("/api/bollinger/live")
    def bollinger_live(request: Request, body: dict) -> JSONResponse:
        """실주문 스위치. 켜려면 `confirm="실행"` 이 필요하다 (실수 방지)."""
        bot = _bot(request)
        want = bool(body.get("live"))
        if want and body.get("confirm") != "실행":
            raise HTTPException(status_code=400,
                                detail="실주문 전환에는 confirm='실행' 이 필요합니다.")
        if want and not bot.trading.enabled:
            raise HTTPException(
                status_code=403,
                detail="서버 실거래 스위치(TOSS_TRADING_ENABLED)와 설정의 '실주문 허용' 이 "
                       "모두 켜져 있어야 합니다.")
        return JSONResponse({"ok": True, "live": bot.set_live(want)})

    @app.post("/api/bollinger/start")
    def bollinger_start(request: Request) -> JSONResponse:
        bot = _bot(request)
        if not bot.cfg.symbols:
            raise HTTPException(status_code=400, detail="감시할 종목을 먼저 추가하세요.")
        bot.start()
        return JSONResponse({"ok": True, "running": True, "live": bot.live})

    @app.post("/api/bollinger/stop")
    def bollinger_stop(request: Request) -> JSONResponse:
        _bot(request).stop()
        return JSONResponse({"ok": True, "running": False})

    @app.post("/api/bollinger/run")
    def bollinger_run(request: Request) -> JSONResponse:
        """지금 한 주기만 즉시 실행 (봇이 꺼져 있어도 판정은 돈다)."""
        bot = _bot(request)
        threading.Thread(target=bot.run_once, daemon=True).start()
        return JSONResponse({"ok": True, "message": "한 주기 실행을 시작했습니다"})

    @app.get("/api/bollinger/chart/{symbol}")
    def bollinger_chart(request: Request, symbol: str, count: int = 80) -> JSONResponse:
        """밴드·RSI 를 붙인 최근 봉 (볼린저 화면 차트용)."""
        bot = _bot(request)
        try:
            df = bot.indicator_frame(symbol)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if df.empty:
            raise HTTPException(status_code=404, detail="데이터 없음")
        tail = df.dropna(subset=["band_mid", "rsi"]).tail(max(10, min(count, 400)))
        if tail.empty:
            raise HTTPException(status_code=404, detail="지표를 계산할 만큼 봉이 모이지 않았습니다")
        return JSONResponse({
            "symbol": symbol, "interval": bot.cfg.interval,
            "bars": [{"time": idx.strftime("%Y-%m-%d %H:%M"),
                      "open": _f(r.open), "high": _f(r.high), "low": _f(r.low),
                      "close": _f(r.close), "volume": _f(r.volume),
                      "upper": _f(r.band_upper), "mid": _f(r.band_mid),
                      "lower": _f(r.band_lower), "rsi": _f(r.rsi)}
                     for idx, r in tail.iterrows()],
        })

    # ------------------------------------------------------------- 주문
    @app.get("/api/trading/status")
    def trading_status(request: Request) -> JSONResponse:
        sess = _session(request)
        svc = sess.trading if sess is not None else _server_trading(request)
        if svc is None:
            return JSONResponse({"configured": False, "enabled": False})
        return JSONResponse({"configured": True, **svc.status()})

    @app.post("/api/trading/preview")
    def trading_preview(request: Request, body: dict) -> JSONResponse:
        """실주문 없이 호가 정렬·검증 결과만 돌려준다."""
        svc = _trading(request)
        try:
            return JSONResponse(svc.preview(**_order_args(body)))
        except TradingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/trading/order")
    def trading_order(request: Request, body: dict) -> JSONResponse:
        """매수 주문 + 체결 시 OCO(익절·손절) 자동 등록.

        `dry_run=false` 와 `confirm="실행"` 이 **둘 다** 있어야 실주문이 나간다.
        """
        svc = _trading(request)
        dry_run = bool(body.get("dry_run", True))
        if not dry_run and body.get("confirm") != "실행":
            raise HTTPException(status_code=400,
                                detail="실주문에는 confirm='실행' 이 필요합니다.")
        try:
            outcome = svc.submit(dry_run=dry_run, **_order_args(body))
        except TradingError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(outcome.as_dict())

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True,
                             "running": bool(service and service.state.running),
                             "last": service.state.finished_at if service else None})

    return app


def _f(v) -> float:
    return round(float(v), 4)


def _order_args(body: dict) -> dict:
    """요청 본문에서 주문 인자만 추려낸다 (예상치 못한 키 차단)."""
    try:
        return {
            "symbol": str(body["symbol"]),
            "name": str(body.get("name", "")),
            "quantity": int(body["quantity"]),
            "entry": float(body["entry"]),
            "stop": float(body["stop"]),
            "target": float(body["target"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise TradingError(f"주문 항목이 올바르지 않습니다: {exc}") from exc
