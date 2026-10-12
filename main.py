#!/usr/bin/env python3
"""세력 추정 매매법 자동 종목 스크리너 CLI.

사용 예:
  python main.py demo                          # API 없이 가상 데이터로 동작 확인
  python main.py scan --limit 300              # 일봉 기준 전체 스캔
  python main.py scan --top 100 --live         # 거래대금 상위 100 + 현재가 반영
  python main.py watch --top 100               # 돌파 대기(관심) 종목
  python main.py poll --top 100 --interval 60  # 장중 반복 감시 + 텔레그램 알림
  python main.py web --port 8083               # 웹 대시보드
  python main.py backtest --limit 300 --years 3 # 과거 성과 검증
  python main.py friday-bt                      # 금요일 마감 돌파 일봉 대리 백테스트 (오프라인)
  python main.py friday                         # 금요일 마감 돌파 1회 스캔 (14:30~15:15 에 기록)
  python main.py friday-report                  # 금요일 신호 포워드 기록 리포트
  python main.py explain 005930                # 특정 종목 조건 진단
  python main.py bollinger --symbols 005930 --bb-interval 15m   # 볼린저 반전 매매 봇(모의)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime

from pathlib import Path

import pandas as pd

from screener.bollinger import INTERVALS as BOLLINGER_INTERVALS, BollingerConfig
from screener.config import AppConfig, StrategyConfig
from screener.data_loader import SyntheticDataLoader, TossDataLoader, CsvDataLoader
from screener.notifier import (ConsoleNotifier, DedupeNotifier, JsonFileNotifier,
                               MultiNotifier, TelegramNotifier)
from screener.pattern_detector import PatternDetector
from screener.screener import Screener
from screener.toss_client import TossClient, token_cache_path


def build_loader(args, app: AppConfig, cfg: StrategyConfig):
    if args.source == "synthetic":
        return SyntheticDataLoader()
    if args.source == "csv":
        return CsvDataLoader(args.csv_dir)
    app.require_toss()
    # 토큰은 캐시 디렉터리를 통해 프로세스 간 공유한다 (web·poller 동시 실행 대비)
    client = TossClient(app.toss_client_id, app.toss_client_secret,
                        token_cache=token_cache_path(app.cache_dir, app.toss_client_id))
    return TossDataLoader(client, markets=cfg.markets, cache_dir=app.cache_dir,
                          cache_ttl=args.cache_ttl)


def build_notifier(args, app: AppConfig):
    parts = [ConsoleNotifier()]
    if app.telegram_bot_token and app.telegram_chat_id and not args.no_telegram:
        parts.append(TelegramNotifier(app.telegram_bot_token, app.telegram_chat_id))
    if args.out:
        parts.append(JsonFileNotifier(args.out))
    return MultiNotifier(*parts)


def resolve_symbols(args, loader) -> list[str] | None:
    if args.symbols:
        return args.symbols
    if getattr(args, "top", 0) and isinstance(loader, TossDataLoader):
        return loader.top_by_turnover(args.top)
    return None


def cmd_scan(args, app, cfg):
    loader = build_loader(args, app, cfg)
    scr = Screener(loader, cfg, max_workers=args.workers)
    report = scr.scan(limit=args.limit, symbols=resolve_symbols(args, loader), live=args.live)
    build_notifier(args, app).send(report.signals)
    print(f"판정 {report.scanned} / 제외 {report.skipped} / 오류 {report.errors}")
    if args.csv_out and report.signals:
        report.to_frame().to_csv(args.csv_out, index=False, encoding="utf-8-sig")
        print(f"CSV 저장: {args.csv_out}")
    return 0


def cmd_watch(args, app, cfg):
    loader = build_loader(args, app, cfg)
    scr = Screener(loader, cfg, max_workers=args.workers)
    symbols = resolve_symbols(args, loader)
    report = scr.scan(limit=args.limit, symbols=symbols, live=args.live)
    done = {s.symbol for s in report.signals} | {b["종목코드"] for b in report.blocked}
    rows = scr.watch_candidates(limit=args.limit, symbols=symbols, exclude=done)
    if not rows:
        print("돌파 대기 종목 없음")
        return 0
    print(f"\n[돌파 대기] STEP1+STEP2 통과 {len(rows)}건 — 전일고가 돌파 시 매수\n")
    print(pd.DataFrame(rows).to_string(index=False))
    return 0


def cmd_poll(args, app, cfg):
    """장중 반복 스캔 (실시간 돌파 감시)."""
    loader = build_loader(args, app, cfg)
    scr = Screener(loader, cfg, max_workers=args.workers)
    notifier = DedupeNotifier(build_notifier(args, app))
    symbols = resolve_symbols(args, loader)
    print(f"장중 감시 시작 (주기 {args.interval}s, Ctrl+C 로 종료)")
    while True:
        try:
            report = scr.scan(limit=args.limit, symbols=symbols, live=True)
            notifier.send(report.signals)
        except KeyboardInterrupt:
            print("\n감시 종료")
            return 0
        except Exception as exc:
            logging.warning("스캔 실패: %s", exc)
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n감시 종료")
            return 0


def build_friday_config(args):
    """CLI 옵션 → FridayConfig (금요일이 휴장일인 주의 목요일 등을 `--friday-extra-days` 로 넣는다)."""
    from screener.friday import FridayConfig

    extra = tuple(d.strip() for d in (args.friday_extra_days or "").split(",") if d.strip())
    return FridayConfig(extra_days=extra)


def cmd_friday_bt(args, app, cfg):
    """P0 — 일봉 캐시로 금요일 마감 돌파 가설을 대리 검증한다 (오프라인, API 불필요)."""
    from screener.friday_backtest import ProxyParams, run_proxy_from_cache

    bars = int(args.years * 250) + cfg.min_history
    daily_dir = app.cache_dir / "candles"
    if not any(daily_dir.glob(f"*_{bars}.json")):
        # 일봉 캐시는 `backtest` 가 만든다 (종목당 years*250+min_history 봉). 가장 흔한 개수를 안내한다.
        counts = sorted({p.stem.split("_")[-1] for p in daily_dir.glob("*_*.json")}) if daily_dir.exists() else []
        print(f"일봉 캐시({bars}봉)가 없습니다. 먼저 `python main.py backtest --years {args.years}` 를 돌리거나 "
              f"--years 를 캐시된 봉 수({', '.join(counts) or '없음'})에 맞추세요.")
        return 2
    params = ProxyParams(slippage=args.slippage)
    result = run_proxy_from_cache(app.cache_dir, params, bars=bars)
    print(result.to_text())
    out = Path(args.csv_out or "out/friday_proxy.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n결과 저장: {out}")
    return 0


def cmd_friday(args, app, cfg):
    """금요일 마감 돌파 1회 스캔 (주말을 넘기는 날 14:30~15:15). 토스 소스 전용. 창 안이면 기록도 남긴다."""
    from screener.friday import FridayRecorder, FridayScanner

    loader = build_loader(args, app, cfg)
    if not isinstance(loader, TossDataLoader):
        print("금요일 마감 매매는 토스 1분봉이 필요합니다 (--source toss).")
        return 2
    recorder = FridayRecorder(app.order_log.parent / "friday_signals.jsonl")
    scanner = FridayScanner(loader, build_friday_config(args), max_workers=args.workers, recorder=recorder)
    report = scanner.scan()
    print(f"기준일 {report.session} · 후보 {report.scanned} · 포착 {len(report.signals)}"
          f" · 제외 {len(report.blocked)} · 오류 {report.errors} · 기록 {report.recorded}"
          f" · 국면 {report.regimes}")
    if not report.signals:
        print("포착된 종목 없음 (금요일 14:30~15:15 사이 가장 의미 있습니다)")
    else:
        cols = ["진입", "점수", "종목코드", "종목명", "판정시각", "현재가", "등락률%", "급증배수",
                "매수비중%", "돌파선", "VWAP", "주간변동폭%", "유의사항"]
        print(pd.DataFrame([{k: s.to_dict()[k] for k in cols} for s in report.signals]).to_string(index=False))
    filled = recorder.fill_outcomes(loader, datetime.now().date())
    if filled:
        print(f"\n지난 신호 {filled}건의 다음 영업일 결과를 채웠습니다.")
    print("\n※ 백테스트로 검증되지 않은 전략입니다. 주말 뉴스·공시는 직접 확인하세요. 주문은 내지 않습니다.")
    return 0


def cmd_friday_report(args, app, cfg):
    """P3 — 포워드 기록 리포트 (월요일 시가·09:30·종가 결과). 결과가 비면 먼저 채운다."""
    from screener.friday import FridayRecorder

    recorder = FridayRecorder(app.order_log.parent / "friday_signals.jsonl")
    if args.source == "toss" and app.toss_client_id and app.toss_client_secret:
        try:
            filled = recorder.fill_outcomes(build_loader(args, app, cfg), datetime.now().date())
            if filled:
                print(f"결과 {filled}건을 새로 채웠습니다.")
        except Exception as exc:
            print(f"(결과 채우기 건너뜀: {exc})")
    rep = recorder.report()
    print(f"기록 후보 {rep['recorded']} · 진입 {rep['recorded_entries']} · 결과 대기 {rep['pending']}"
          f" · 주 수 {rep['weeks']} · 왕복 비용 {rep['cost'] * 100:.2f}%")
    for name, g in rep["groups"].items():
        print(f"  {name:<22} " + (" · ".join(f"{k} {v}" for k, v in g.items()) if g["건수"] else "(결과 없음)"))
    print("\n" + rep["note"])
    return 0


def cmd_supply(args, app, cfg):
    """수급 포착 매매 1회 스캔 (장 초반 거래량 급증 + 수급 연속성). 토스 소스 전용."""
    from screener.supply import SupplyConfig, SupplyScanner

    loader = build_loader(args, app, cfg)
    if not isinstance(loader, TossDataLoader):
        print("수급 포착 매매는 토스 1분봉이 필요합니다 (--source toss).")
        return 2
    report = SupplyScanner(loader, SupplyConfig(), max_workers=args.workers).scan()
    print(f"기준일 {report.session} · 후보 {report.scanned} · 포착 {len(report.signals)}"
          f" · 제외 {len(report.blocked)} · 오류 {report.errors}")
    if not report.signals:
        print("포착된 종목 없음")
        return 0
    cols = ["상태", "점수", "종목코드", "종목명", "판정시각", "현재가", "등락률%", "급증배수",
            "매수비중%", "VWAP", "손절가", "가격위치%", "유의사항"]
    rows = [{k: s.to_dict()[k] for k in cols} for s in report.signals]
    print(pd.DataFrame(rows).to_string(index=False))
    print("\n※ 백테스트로 검증되지 않은 전략입니다. 재료(뉴스·공시)는 직접 확인하세요.")
    return 0


def cmd_explain(args, app, cfg):
    loader = build_loader(args, app, cfg)
    det = PatternDetector(cfg)
    for sym in args.symbols:
        df = loader.ohlcv(sym, count=max(cfg.min_history + 40, 260))
        name = getattr(loader, "meta", lambda s: None)(sym)
        print(f"\n===== {sym} {getattr(name, 'name', '')} (봉 {len(df)}개) =====")
        info = det.explain(df)
        print(pd.Series(info).to_string())
    return 0


def cmd_backtest(args, app, cfg):
    """과거 데이터로 승률·손익비·최대낙폭을 측정한다."""
    from screener.backtest import Backtester, ExitPolicy, format_report

    loader = build_loader(args, app, cfg)
    policy = ExitPolicy(take_profit_r=args.tp_r, final_target_r=args.target_r,
                        max_hold=args.max_hold, slippage=args.slippage)
    bt = Backtester(loader, cfg, policy, max_workers=args.workers)

    if args.symbols:
        universe = args.symbols
    else:
        scr = Screener(loader, cfg, max_workers=args.workers)
        universe = scr.build_universe(limit=args.limit, symbols=resolve_symbols(args, loader))
    bars = int(args.years * 250) + cfg.min_history

    print(f"백테스트 시작: {len(universe)}종목 × 최근 {args.years}년 "
          f"(종목당 {bars}봉) — 수 분 걸릴 수 있습니다")
    result = bt.run(universe, bars=bars)
    print(format_report(result, policy))

    if result.trades:
        out = args.csv_out or "out/backtest_trades.csv"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        result.to_frame().to_csv(out, index=False, encoding="utf-8-sig")
        print(f"\n거래 내역 저장: {out}")
    return 0


def cmd_web(args, app, cfg):
    """웹 대시보드 서버 (기본 8083 포트).

    서버 `.env` 에 자격증명이 없어도 뜬다 — 접속자가 상단 '설정'에서 자기
    토스 API 키를 넣으면 그 키 전용 스캐너가 세션별로 돌아간다.
    """
    import uvicorn
    from screener.sessions import SessionManager
    from screener.web import create_app

    loader = None
    if args.source != "toss" or (app.toss_client_id and app.toss_client_secret):
        loader = build_loader(args, app, cfg)
    else:
        print("서버 API 키가 없습니다 — 접속자가 대시보드 '설정'에서 각자 키를 입력하면 됩니다.")

    # 개인 키 입력(멀티 사용자)은 토스 소스일 때만 의미가 있다.
    sessions = SessionManager(
        cfg, cache_dir=app.cache_dir, workers=args.workers, cache_ttl=args.cache_ttl,
        trading_enabled=app.trading_enabled and not args.no_trading,
        max_order_amount=app.max_order_amount, order_log=app.order_log,
        bollinger_log=app.order_log.parent / "bollinger.log",
        max_sessions=args.max_sessions,
        supply_interval=args.supply_interval,
        friday_cfg=build_friday_config(args),
        friday_log=app.order_log.parent / "friday_signals.jsonl",
        mail={"host": app.mail_smtp_host, "port": app.mail_smtp_port,
              "user": app.mail_smtp_user, "password": app.mail_smtp_pass,
              "crypto": app.mail_smtp_crypto, "from_name": app.mail_from_name}
             if app.mail_enabled else None,
    ) if args.source == "toss" else None

    notifier = None
    if app.telegram_bot_token and app.telegram_chat_id and not args.no_telegram:
        notifier = DedupeNotifier(TelegramNotifier(app.telegram_bot_token, app.telegram_chat_id))
    trading = None
    if isinstance(loader, TossDataLoader):
        from screener.trading import TradingService
        trading = TradingService(
            loader.client,
            enabled=app.trading_enabled and not args.no_trading,
            max_order_amount=app.max_order_amount,
            account_seq=app.account_seq,
            log_path=app.order_log,
        )
        print(f"주문 기능: {'실거래 ON ⚠' if trading.enabled else '모의(dry-run) 전용'}"
              f" · 1회 한도 {int(app.max_order_amount):,}원")

    print(f"이메일 알림: {'가능 (' + app.mail_smtp_user + ')' if app.mail_enabled else '꺼짐 (MAIL_SMTP_PASS 미설정)'}")

    # 볼린저 반전 매매 봇 (서버 기본 자격증명용). 만들어만 두고 자동 시작하지 않는다.
    bollinger = None
    if trading is not None:
        from screener.bollinger_bot import BollingerBot
        bollinger = BollingerBot(
            loader.client, trading, build_bollinger_config(args),
            poll_interval=args.bb_poll, live=False,
            notify=(TelegramNotifier(app.telegram_bot_token, app.telegram_chat_id).send_text
                    if app.telegram_bot_token and app.telegram_chat_id and not args.no_telegram
                    else None),
            log_path=app.order_log.parent / "bollinger.log")

    # 수급 포착 매매 (서버 기본 자격증명용). 시세만 보므로 주문과 무관하다.
    supply = None
    if isinstance(loader, TossDataLoader):
        from screener.supply import SupplyConfig, SupplyScanner, SupplyService
        supply = SupplyService(
            SupplyScanner(loader, SupplyConfig(), max_workers=args.workers),
            interval=args.supply_interval)

    # 금요일 마감 돌파 (서버 기본 자격증명용). 시세만 보고 주문과 무관하다.
    friday = None
    if isinstance(loader, TossDataLoader):
        from screener.friday import FridayRecorder, FridayScanner, FridayService
        friday = FridayService(
            FridayScanner(loader, build_friday_config(args), max_workers=args.workers,
                          recorder=FridayRecorder(app.order_log.parent / "friday_signals.jsonl")),
            interval=args.supply_interval)

    # 로그인·설정 저장소 (SQLite). `--no-login` 이면 예전처럼 인증 없이 뜬다.
    store = None
    if not args.no_login:
        from screener.store import Store
        store = Store(app.db_path, secret_key=app.app_secret_key)
        if app.admin_user and app.admin_password:
            if store.ensure_user(app.admin_user, app.admin_password, admin=True):
                print(f"기본 관리자 계정을 만들었습니다: {app.admin_user}")
        # 관리자 구분 이전에 만든 DB — APP_ADMIN_USER 계정을 관리자로 올린다.
        if not store.admins() and app.admin_user in store.users():
            store.set_admin(app.admin_user)
            print(f"관리자 지정: {app.admin_user}")
        if not store.users():
            print("계정이 없습니다 — `.env` 에 APP_ADMIN_PASSWORD 를 넣거나 "
                  "`python main.py adduser <아이디> --admin` 으로 먼저 만드세요.")
        if app.app_secret_key in ("", "jogiclub", "change-me"):
            print("⚠ APP_SECRET_KEY 가 기본값입니다. 여러 사람이 쓰는 서버라면 "
                  "긴 임의 문자열로 바꾸세요 (바꾸면 저장된 키는 다시 입력해야 합니다).")
        print("회원가입: " + ("열림" + (" (초대 코드 필요)" if app.signup_code else "")
                              if app.signup_enabled else "닫힘"))

    application = create_app(loader, cfg, interval=args.interval, top=args.top or 0,
                             limit=args.limit, live=args.live, notifier=notifier,
                             workers=args.workers, trading=trading, sessions=sessions,
                             bollinger=bollinger, supply=supply, friday=friday, store=store,
                             signup=app.signup_enabled, signup_code=app.signup_code)
    print(f"대시보드: http://{args.host}:{args.port}"
          + (" (로그인 필요)" if store else ""))
    uvicorn.run(application, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_adduser(args, app, cfg):
    """계정 생성·비밀번호 변경. 비밀번호는 **화면에 찍히지 않게** 직접 입력받는다."""
    import getpass

    from screener.store import Store

    store = Store(app.db_path, secret_key=app.app_secret_key)
    pw = args.password or getpass.getpass("비밀번호: ")
    if not pw:
        print("비밀번호가 비었습니다.")
        return 2
    if store.ensure_user(args.username, pw, admin=args.admin):
        print(f"계정을 만들었습니다: {args.username}" + (" (관리자)" if args.admin else ""))
    else:
        store.set_password(args.username, pw)
        print(f"비밀번호를 변경했습니다: {args.username}")
        if args.admin:
            store.set_admin(args.username)
            print("관리자로 지정했습니다.")
    return 0


def build_bollinger_config(args) -> BollingerConfig:
    """CLI 인자 → 볼린저 전략 설정."""
    return BollingerConfig(
        bb_period=args.bb_period, bb_stddev=args.bb_stddev, rsi_period=args.rsi_period,
        rsi_buy=args.rsi_buy, rsi_sell=args.rsi_sell, interval=args.bb_interval,
        symbols=tuple(args.symbols or ()), cash_ratio=args.cash_ratio,
        max_positions=args.max_positions, stop_loss_pct=args.stop_loss,
        mid_take_profit=not args.no_mid_tp)


def cmd_bollinger(args, app, cfg):
    """볼린저 밴드 반전 매매 봇 (콘솔 실행).

    기본은 **모의(dry-run)** 다. 실주문은 `--live` 와 `TOSS_TRADING_ENABLED=true`,
    그리고 `--no-trading` 이 없을 때만 나간다.
    """
    from screener.bollinger_bot import BollingerBot
    from screener.trading import TradingService

    loader = build_loader(args, app, cfg)
    if not isinstance(loader, TossDataLoader):
        print("볼린저 봇은 토스 API 소스(--source toss)에서만 동작합니다.")
        return 2

    bb = build_bollinger_config(args)
    if not bb.symbols:
        print("감시할 종목이 없습니다. --symbols 005930 000660 처럼 지정하세요.")
        return 2

    trading = TradingService(loader.client,
                             enabled=app.trading_enabled and not args.no_trading,
                             max_order_amount=app.max_order_amount,
                             account_seq=app.account_seq, log_path=app.order_log)
    notify = None
    if app.telegram_bot_token and app.telegram_chat_id and not args.no_telegram:
        notify = TelegramNotifier(app.telegram_bot_token, app.telegram_chat_id).send_text

    bot = BollingerBot(loader.client, trading, bb, poll_interval=args.bb_poll,
                       live=args.live_orders, notify=notify,
                       log_path=app.order_log.parent / "bollinger.log")
    mode = "실주문 ⚠" if bot.live else "모의(dry-run)"
    print(f"볼린저 반전 매매 봇 시작 [{mode}] · {bb.interval}봉 · "
          f"감시 {', '.join(bb.symbols)} · {args.bb_poll}초 주기 (Ctrl+C 종료)")
    bot.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bot.stop()
        print("\n봇 종료")
    return 0


def cmd_demo(args, app, cfg):
    """API 없이 가상 데이터로 전체 파이프라인 동작 확인 (PoC)."""
    loader = SyntheticDataLoader(n_symbols=8, signal_symbols=("900001", "900005"))
    scr = Screener(loader, cfg, max_workers=4)
    report = scr.scan()
    ConsoleNotifier().send(report.signals)
    print(f"판정 {report.scanned} / 제외 {report.skipped} / 오류 {report.errors}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="세력 추정 매매법 종목 스크리너")
    p.add_argument("--source", choices=["toss", "synthetic", "csv"], default="toss")
    p.add_argument("--csv-dir", default="data")
    p.add_argument("--symbols", nargs="*", help="특정 종목만 검사 (예: 005930 000660)")
    p.add_argument("--limit", type=int, help="유니버스 최대 종목 수")
    p.add_argument("--top", type=int, default=0, help="거래대금 상위 N 종목만 스캔")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--cache-ttl", type=float, default=3600.0)
    p.add_argument("--live", action="store_true", help="현재가를 반영해 장중 돌파 판정")
    p.add_argument("--interval", type=int, default=60, help="poll/web 스캔 주기(초)")
    p.add_argument("--port", type=int, default=8083, help="web 서버 포트")
    p.add_argument("--host", default="0.0.0.0", help="web 서버 바인드 주소")
    p.add_argument("--out", help="시그널 JSONL 저장 경로")
    p.add_argument("--csv-out", help="시그널 CSV 저장 경로")
    p.add_argument("--no-telegram", action="store_true")
    p.add_argument("--supply-interval", type=int, default=60,
                   help="web: 수급 포착 매매 장중 스캔 주기(초). 장 밖에서는 30분에 한 번")
    p.add_argument("--friday-extra-days", default="",
                   help="금요일 마감 매매: 금요일이 휴장일일 때 주말 직전 영업일(YYYY-MM-DD, 콤마 구분)")
    p.add_argument("--max-sessions", type=int, default=20,
                   help="web: 개인 API 키 세션 동시 접속 상한 (세션마다 스캔 스레드 1개)")
    p.add_argument("--no-trading", action="store_true",
                   help="실거래를 강제로 끈다 (TOSS_TRADING_ENABLED 보다 우선)")
    p.add_argument("-v", "--verbose", action="store_true")
    # 전략 파라미터 오버라이드
    p.add_argument("--profile", choices=["tuned", "spec"], default="tuned",
                   help="tuned=백테스트 검증 프로파일(기본), spec=원본 전략 스펙")
    p.add_argument("--tail", type=float, help="윗꼬리 최소 비율")
    p.add_argument("--vol-mult", type=float, help="기준봉 거래량 배수")
    p.add_argument("--vol-mode", choices=["or", "and"],
                   help="기준봉 거래량 조건: 20일최고 'or' 평균배수 / 'and' 둘 다")
    p.add_argument("--max-adjust", type=int, help="조정 최대 영업일")
    p.add_argument("--drawdown", type=float, help="바닥권 최소 하락률")
    # 백테스트 옵션
    p.add_argument("--years", type=float, default=3.0, help="백테스트 기간(년)")
    p.add_argument("--tp-r", type=float, default=1.0, help="1차 익절 R 배수")
    p.add_argument("--target-r", type=float, default=3.0, help="최종 목표 R 배수")
    p.add_argument("--max-hold", type=int, default=15, help="시간 손절 영업일")
    p.add_argument("--slippage", type=float, default=0.003, help="슬리피지 비율")
    p.add_argument("--stop-mode", choices=["base_low", "prev_low", "pct"],
                   help="손절 기준: 기준봉 저가 / 전일 저가 / 고정 %%")
    p.add_argument("--stop-pct", type=float, help="stop-mode=pct 일 때 손절 폭")
    # 볼린저 밴드 반전 매매
    p.add_argument("--bb-interval", choices=list(BOLLINGER_INTERVALS), default="15m",
                   help="볼린저 봉 주기 (분봉은 1분봉을 리샘플해 만든다)")
    p.add_argument("--bb-period", type=int, default=20, help="볼린저 밴드 기간")
    p.add_argument("--bb-stddev", type=float, default=2.0, help="볼린저 표준편차 배수")
    p.add_argument("--rsi-period", type=int, default=14)
    p.add_argument("--rsi-buy", type=float, default=30.0, help="RSI 과매도 기준(매수)")
    p.add_argument("--rsi-sell", type=float, default=70.0, help="RSI 과매수 기준(매도)")
    p.add_argument("--cash-ratio", type=float, default=0.20, help="1회 매수 예수금 비율")
    p.add_argument("--max-positions", type=int, default=3, help="동시 보유 종목 수 상한")
    p.add_argument("--stop-loss", type=float, default=0.05, help="매수가 대비 강제 손절 폭")
    p.add_argument("--no-mid-tp", action="store_true", help="중앙선 부분 익절을 끈다")
    p.add_argument("--bb-poll", type=int, default=60, help="볼린저 봇 점검 주기(초)")
    p.add_argument("--live-orders", action="store_true",
                   help="⚠ 볼린저 봇 실주문 (TOSS_TRADING_ENABLED 도 함께 켜져 있어야 함)")
    p.add_argument("--no-login", action="store_true",
                   help="web: 로그인 없이 띄운다 (사내망 등 신뢰된 환경에서만)")
    p.add_argument("--admin", action="store_true",
                   help="adduser: 관리자로 만든다 (서버 .env 계좌로 주문·봇 사용 가능)")
    p.add_argument("--password", help="adduser: 비밀번호를 인자로 준다 "
                                      "(셸 기록에 남으니 되도록 대화식 입력을 쓸 것)")
    p.add_argument("command", choices=["scan", "watch", "poll", "web", "backtest",
                                       "explain", "demo", "bollinger", "adduser", "supply",
                                       "friday", "friday-bt", "friday-report"])
    p.add_argument("username", nargs="?", help="adduser: 만들 계정 아이디")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = StrategyConfig.tuned() if args.profile == "tuned" else StrategyConfig.spec()
    if args.tail is not None:
        cfg.upper_tail_min = args.tail
    if args.vol_mult is not None:
        cfg.vol_mult_min = args.vol_mult
    if args.vol_mode is not None:
        cfg.vol_condition_mode = args.vol_mode
    if args.max_adjust is not None:
        cfg.adjust_max_days = args.max_adjust
    if args.drawdown is not None:
        cfg.bottom_drawdown_min = args.drawdown
    if args.stop_mode is not None:
        cfg.stop_mode = args.stop_mode
    if args.stop_pct is not None:
        cfg.stop_pct = args.stop_pct
    if args.command == "demo":
        args.source = "synthetic"
    if args.command == "adduser" and not args.username:
        p.error("adduser 에는 아이디가 필요합니다: python main.py adduser <아이디>")

    app = AppConfig()
    handler = {"scan": cmd_scan, "watch": cmd_watch, "poll": cmd_poll, "web": cmd_web,
               "backtest": cmd_backtest, "explain": cmd_explain, "demo": cmd_demo,
               "bollinger": cmd_bollinger, "adduser": cmd_adduser,
               "supply": cmd_supply, "friday": cmd_friday, "friday-bt": cmd_friday_bt,
               "friday-report": cmd_friday_report}[args.command]
    try:
        return handler(args, app, cfg)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
