"""웹 대시보드 (FastAPI).

백그라운드 스캐너 스레드가 주기적으로 유니버스를 스캔하고, 최신 결과를
메모리에 보관한다. 브라우저는 JSON API 를 폴링해 화면을 갱신한다.

자격증명은 두 가지 경로가 있다.
1. 서버 `.env` (`TOSS_CLIENT_ID`/`TOSS_CLIENT_SECRET`) — 있으면 기본 세션으로 동작한다.
2. 접속자가 상단 **설정** 창에 입력한 자기 키 — `SessionManager` 가 세션별 스캐너를 띄운다.
   세션 쿠키(`sf_session`)를 가진 요청은 항상 자기 세션 쪽을 본다.

실행:  python main.py web --port 8083
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .bollinger import BollingerConfig
from .bollinger_bot import BollingerBot
from .config import StrategyConfig
from .data_loader import DataLoader
from .notifier import Notifier
from .scan_service import ScanService, ScanState, _now
from .screener import Screener
from .sessions import COOKIE_NAME, SessionError, SessionManager, SessionSettings, UserSession
from .trading import TradingError, TradingService

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

__all__ = ["ScanService", "ScanState", "create_app"]


# ================================================================== FastAPI
def create_app(loader: DataLoader | None = None, cfg: StrategyConfig | None = None, *,
               interval: int = 300, top: int = 200, limit: int | None = None,
               live: bool = False, notifier: Notifier | None = None, workers: int = 8,
               trading: TradingService | None = None,
               sessions: SessionManager | None = None,
               bollinger: BollingerBot | None = None) -> FastAPI:
    cfg = cfg or StrategyConfig.tuned()
    service = ScanService(Screener(loader, cfg, max_workers=workers),
                          interval=interval, top=top, limit=limit,
                          live=live, notifier=notifier) if loader is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if service:
            service.start()
        log.info("대시보드 준비 완료 (스캔 주기 %ds)", interval)
        yield
        if service:
            service.stop()
        if bollinger:
            bollinger.stop()
        if sessions:
            sessions.stop_all()

    app = FastAPI(title="세력 추정 매매법 스크리너", docs_url="/docs", lifespan=lifespan)
    app.state.service = service
    app.state.trading = trading
    app.state.sessions = sessions
    app.state.bollinger = bollinger
    # 공통 GNB·테마·설정 모듈 (외부 CDN 없이 로컬 정적 파일로만 제공한다)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ------------------------------------------------------- 세션 해석
    def _session(request: Request) -> UserSession | None:
        if sessions is None:
            return None
        return sessions.get(request.cookies.get(COOKIE_NAME))

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
        svc = sess.trading if sess is not None else trading
        if svc is None:
            raise HTTPException(status_code=503, detail="주문 기능이 구성되지 않았습니다.")
        return svc

    def _bot(request: Request) -> BollingerBot:
        """이 요청이 조작할 볼린저 봇. 세션 > 서버 기본 순 (스캐너와 같은 규칙)."""
        sess = _session(request)
        bot = sess.bollinger if sess is not None else bollinger
        if bot is None:
            raise HTTPException(status_code=503,
                                detail="API 키가 설정되지 않았습니다. 상단 '설정'에서 키를 입력하세요.")
        return bot

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
        body = {
            "multi_user": sessions is not None,
            "trading_allowed": bool(sessions and sessions.trading_enabled),
            "server_max_order_amount": sessions.max_order_amount if sessions else None,
            "server_default": service is not None,
        }
        if sess is not None:
            return JSONResponse({**body, **sess.info()})
        if service is not None:
            return JSONResponse({**body, "configured": True, "source": "server",
                                 "client_id_masked": None, "top": service.top,
                                 "interval": service.interval, "limit": service.limit,
                                 "live": service.live})
        return JSONResponse({**body, "configured": False, "source": None})

    @app.post("/api/session")
    def session_create(request: Request, body: dict) -> JSONResponse:
        """설정 창에서 받은 자격증명으로 개인 세션을 연다."""
        if sessions is None:
            raise HTTPException(status_code=503,
                                detail="이 서버는 개인 API 키 입력을 지원하지 않습니다.")
        try:
            settings = SessionSettings.from_body(body)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        sessions.drop(request.cookies.get(COOKIE_NAME))   # 재설정이면 기존 세션 정리
        try:
            sess = sessions.create(settings)
        except SessionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        resp = JSONResponse({"ok": True, **sess.info()})
        resp.set_cookie(COOKIE_NAME, sess.sid, httponly=True, samesite="lax",
                        max_age=int(sessions.idle_ttl), path="/")
        return resp

    @app.delete("/api/session")
    def session_delete(request: Request) -> JSONResponse:
        dropped = sessions.drop(request.cookies.get(COOKIE_NAME)) if sessions else False
        resp = JSONResponse({"ok": True, "dropped": dropped})
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

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

    # --------------------------------------------------- 볼린저 반전 매매
    @app.get("/api/bollinger/state")
    def bollinger_state(request: Request) -> JSONResponse:
        sess = _session(request)
        bot = sess.bollinger if sess is not None else bollinger
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
        return JSONResponse({"ok": True, "config": cfg.describe(),
                             "poll_interval": bot.poll_interval})

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
        svc = sess.trading if sess is not None else trading
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
