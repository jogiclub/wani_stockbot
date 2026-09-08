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
  python main.py explain 005930                # 특정 종목 조건 진단
  python main.py bollinger --symbols 005930 --bb-interval 15m   # 볼린저 반전 매매 봇(모의)
"""

from __future__ import annotations

import argparse
import logging
import sys
import time

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
    rows = scr.watch_candidates(limit=args.limit, symbols=resolve_symbols(args, loader))
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

    application = create_app(loader, cfg, interval=args.interval, top=args.top or 0,
                             limit=args.limit, live=args.live, notifier=notifier,
                             workers=args.workers, trading=trading, sessions=sessions,
                             bollinger=bollinger)
    print(f"대시보드: http://{args.host}:{args.port}")
    uvicorn.run(application, host=args.host, port=args.port, log_level="warning")
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
    p.add_argument("--max-sessions", type=int, default=8,
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
    p.add_argument("command", choices=["scan", "watch", "poll", "web", "backtest",
                                       "explain", "demo", "bollinger"])

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

    app = AppConfig()
    handler = {"scan": cmd_scan, "watch": cmd_watch, "poll": cmd_poll, "web": cmd_web,
               "backtest": cmd_backtest, "explain": cmd_explain, "demo": cmd_demo,
               "bollinger": cmd_bollinger}[args.command]
    try:
        return handler(args, app, cfg)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
