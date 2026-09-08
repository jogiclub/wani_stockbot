#!/usr/bin/env python3
"""최소 동작 예시 (PoC) — 증권사 API 없이 알고리즘만 검증한다.

실행:  python poc.py
"""

from screener.config import StrategyConfig
from screener.data_loader import SyntheticDataLoader
from screener.notifier import ConsoleNotifier
from screener.pattern_detector import PatternDetector
from screener.screener import Screener


def main() -> None:
    # 1) 가상 OHLCV (900001, 900005 에 매집→소화→돌파 패턴이 심어져 있음)
    loader = SyntheticDataLoader(n_symbols=8, signal_symbols=("900001", "900005"))

    # 2) 전략 파라미터 (가상 데이터라 유동성 필터는 끔)
    cfg = StrategyConfig(min_avg_turnover=0.0, max_market_cap=0.0)

    # 3) 단일 종목 판정 + 조건별 진단
    det = PatternDetector(cfg)
    df = loader.ohlcv("900001")
    print("=== 최근 8봉 ===")
    print(df.tail(8).to_string(), "\n")

    print("=== 조건 진단 (900001) ===")
    for k, v in det.explain(df).items():
        print(f"{k}: {v}")
    print()

    # 4) 유니버스 전체 스캔 → 콘솔 통보
    report = Screener(loader, cfg, max_workers=4).scan()
    ConsoleNotifier().send(report.signals)

    # 5) 과거 전 구간 백테스트 시그널
    print("=== 백테스트 시그널 (900001) ===")
    for s in det.backtest_signals(df, "900001", "테스트1"):
        print(" ", s.format_line())


if __name__ == "__main__":
    main()
