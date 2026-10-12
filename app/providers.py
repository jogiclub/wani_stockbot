from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from app.models import SourceSnapshot, StockSnapshot


KRX_MARKETS = ("KOSPI", "KOSDAQ")
FOREIGN_INVESTOR_LABEL = "외국인"
INSTITUTION_INVESTOR_LABEL = "기관합계"
SHORTLIST_LIMIT_PER_MARKET = 80


class MarketDataProviderError(RuntimeError):
    """Raised when a configured market data provider cannot load data."""


class MarketDataProvider(Protocol):
    def load(self) -> tuple[str | None, list[StockSnapshot]]:
        ...


class LocalJsonMarketDataProvider:
    def __init__(self, input_file: Path) -> None:
        self.input_file = input_file

    def load(self) -> tuple[str | None, list[StockSnapshot]]:
        if not self.input_file.exists():
            return None, []
        payload = json.loads(self.input_file.read_text(encoding="utf-8"))
        from app.models import ScreeningRequest

        request = ScreeningRequest.model_validate(payload)
        return request.run_date, request.snapshots


class KrxMarketDataProvider:
    def __init__(self, login_id: str | None = None, login_password: str | None = None) -> None:
        self.login_id = login_id or os.getenv("KRX_ID")
        self.login_password = login_password or os.getenv("KRX_PW")

    def load(self) -> tuple[str | None, list[StockSnapshot]]:
        self._configure_krx_auth()

        try:
            from pykrx import stock
        except ImportError as exc:
            raise MarketDataProviderError("pykrx is not installed.") from exc

        run_date = self._resolve_recent_business_day(stock)
        snapshots: list[StockSnapshot] = []
        captured_at = datetime.now().astimezone()

        for market in KRX_MARKETS:
            snapshots.extend(self._load_market(stock, market, run_date, captured_at))

        if not snapshots:
            raise MarketDataProviderError("KRX returned no stock snapshots.")

        return run_date, snapshots

    def _configure_krx_auth(self) -> None:
        if not self.login_id or not self.login_password:
            raise MarketDataProviderError(
                "KRX credentials are required. Set KRX_ID and KRX_PW environment variables."
            )
        os.environ["KRX_ID"] = self.login_id
        os.environ["KRX_PW"] = self.login_password

    def _resolve_recent_business_day(self, stock_module) -> str:
        candidate = datetime.now().strftime("%Y%m%d")
        for days_back in range(0, 10):
            target = (datetime.now() - timedelta(days=days_back)).strftime("%Y%m%d")
            try:
                ohlcv = stock_module.get_market_ohlcv_by_ticker(date=target, market="KOSPI")
            except Exception:
                continue
            if not ohlcv.empty:
                return target
        raise MarketDataProviderError(f"Unable to resolve a recent KRX business day from {candidate}.")

    def _load_market(self, stock_module, market: str, run_date: str, captured_at: datetime) -> list[StockSnapshot]:
        ohlcv = stock_module.get_market_ohlcv_by_ticker(date=run_date, market=market)
        market_cap = stock_module.get_market_cap_by_ticker(date=run_date, market=market)
        foreign_1d = stock_module.get_market_net_purchases_of_equities_by_ticker(
            run_date, run_date, market, FOREIGN_INVESTOR_LABEL
        )
        institution_1d = stock_module.get_market_net_purchases_of_equities_by_ticker(
            run_date, run_date, market, INSTITUTION_INVESTOR_LABEL
        )

        day_3 = self._shift_calendar_days(run_date, 6)
        day_5 = self._shift_calendar_days(run_date, 10)
        day_20 = self._shift_calendar_days(run_date, 40)

        foreign_3d = stock_module.get_market_net_purchases_of_equities_by_ticker(
            day_3, run_date, market, FOREIGN_INVESTOR_LABEL
        )
        institution_3d = stock_module.get_market_net_purchases_of_equities_by_ticker(
            day_3, run_date, market, INSTITUTION_INVESTOR_LABEL
        )
        price_change_5d = stock_module.get_market_price_change_by_ticker(day_5, run_date, market)

        base = self._build_base_frame(
            market=market,
            ohlcv=ohlcv,
            market_cap=market_cap,
            foreign_1d=foreign_1d,
            foreign_3d=foreign_3d,
            institution_1d=institution_1d,
            institution_3d=institution_3d,
            price_change_5d=price_change_5d,
        )

        if base.empty:
            return []

        short_list = self._shortlist(base)
        snapshots: list[StockSnapshot] = []
        for ticker, row in short_list.iterrows():
            history = stock_module.get_market_ohlcv_by_date(day_20, run_date, ticker)
            investor_history = stock_module.get_market_trading_value_by_date(
                day_3, run_date, ticker, on="순매수"
            )
            name = row["name"]
            if not name:
                name = stock_module.get_market_ticker_name(ticker)
            snapshot = self._build_snapshot(
                ticker=ticker,
                market=market,
                row=row,
                history=history,
                investor_history=investor_history,
                captured_at=captured_at,
                name=name,
            )
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    @staticmethod
    def _shift_calendar_days(yyyymmdd: str, days: int) -> str:
        target = datetime.strptime(yyyymmdd, "%Y%m%d") - timedelta(days=days)
        return target.strftime("%Y%m%d")

    @staticmethod
    def _net_buy_amount(frame):
        if frame.empty:
            return {}
        return frame.iloc[:, -1].to_dict()

    @staticmethod
    def _names(frame):
        if frame.empty:
            return {}
        return frame.iloc[:, 0].astype(str).to_dict()

    def _build_base_frame(
        self,
        *,
        market: str,
        ohlcv,
        market_cap,
        foreign_1d,
        foreign_3d,
        institution_1d,
        institution_3d,
        price_change_5d,
    ):
        import pandas as pd

        ohlcv = ohlcv.copy()
        market_cap = market_cap.copy()
        price_change_5d = price_change_5d.copy()

        if ohlcv.empty or market_cap.empty:
            return pd.DataFrame()

        base = pd.DataFrame(index=ohlcv.index)
        base["market"] = market
        base["current_price"] = pd.to_numeric(ohlcv.iloc[:, 3], errors="coerce").fillna(0.0)
        base["current_trading_value"] = pd.to_numeric(ohlcv.iloc[:, 5], errors="coerce").fillna(0)
        base["market_cap_krw"] = pd.to_numeric(market_cap.iloc[:, 1], errors="coerce").fillna(0)
        base["foreign_net_buy_1d_krw"] = pd.Series(self._net_buy_amount(foreign_1d))
        base["foreign_net_buy_3d_krw"] = pd.Series(self._net_buy_amount(foreign_3d))
        base["institution_net_buy_1d_krw"] = pd.Series(self._net_buy_amount(institution_1d))
        base["institution_net_buy_3d_krw"] = pd.Series(self._net_buy_amount(institution_3d))
        base["price_change_5d_pct"] = pd.to_numeric(price_change_5d.iloc[:, 4], errors="coerce").fillna(0.0)
        name_map = self._names(foreign_3d) or self._names(institution_3d) or self._names(price_change_5d)
        base["name"] = pd.Series(name_map)

        base = base.fillna(
            {
                "foreign_net_buy_1d_krw": 0,
                "foreign_net_buy_3d_krw": 0,
                "institution_net_buy_1d_krw": 0,
                "institution_net_buy_3d_krw": 0,
                "name": "",
            }
        )
        base["institution_rank_percentile"] = self._rank_percentile(base["institution_net_buy_3d_krw"])
        return base

    @staticmethod
    def _rank_percentile(series):
        if len(series) <= 1:
            return series * 0
        ranked = series.rank(method="min", ascending=False) - 1
        return ranked / max(len(series) - 1, 1)

    @staticmethod
    def _shortlist(base):
        eligible = base[
            (base["current_price"] > 0)
            & (base["market_cap_krw"] > 0)
            & (
                (base["foreign_net_buy_3d_krw"] > 0)
                | (base["institution_net_buy_3d_krw"] > 0)
            )
        ].copy()
        eligible["priority"] = (
            eligible["foreign_net_buy_3d_krw"].clip(lower=0)
            + eligible["institution_net_buy_3d_krw"].clip(lower=0)
        )
        return eligible.sort_values("priority", ascending=False).head(SHORTLIST_LIMIT_PER_MARKET)

    def _build_snapshot(self, *, ticker: str, market: str, row, history, investor_history, captured_at: datetime, name: str) -> StockSnapshot | None:
        if history is None or history.empty or len(history) < 5:
            return None

        price_series = history.iloc[:, 3]
        high_series = history.iloc[:, 1]
        trading_value_series = history.iloc[:, 5] if history.shape[1] > 5 else history.iloc[:, 4]

        if len(price_series) < 20 or len(high_series) < 20 or len(trading_value_series) < 6:
            return None

        recent_prices = price_series.tail(20)
        recent_highs = high_series.tail(20)
        recent_trading_values = trading_value_series.tail(6)

        if investor_history is None or investor_history.empty or investor_history.shape[1] < 4:
            return None

        institution_daily = investor_history.iloc[:, 0].tail(3).tolist()
        foreign_daily = investor_history.iloc[:, -2].tail(3).tolist()

        foreign_positive_days = sum(value > 0 for value in foreign_daily)
        institution_positive_days = sum(value > 0 for value in institution_daily)

        foreign_sell_streak_days = 0
        for value in reversed(foreign_daily):
            if value < 0:
                foreign_sell_streak_days += 1
            else:
                break

        foreign_positive_streak = 0
        for value in reversed(foreign_daily):
            if value > 0:
                foreign_positive_streak += 1
            else:
                break

        avg_last_3 = int(sum(recent_trading_values.tail(3)) / 3)
        avg_prev_3 = int(sum(recent_trading_values.head(3)) / 3)
        current_volume = float(history.iloc[-1, 4])
        avg_20_volume = float(history.iloc[:, 4].tail(20).mean())

        source_snapshot = SourceSnapshot(
            source="KRX",
            captured_at=captured_at,
            foreign_net_buy_1d_krw=int(row["foreign_net_buy_1d_krw"]),
            foreign_net_buy_3d_krw=int(row["foreign_net_buy_3d_krw"]),
            institution_net_buy_1d_krw=int(row["institution_net_buy_1d_krw"]),
            institution_net_buy_3d_krw=int(row["institution_net_buy_3d_krw"]),
        )

        return StockSnapshot(
            code=ticker,
            name=name,
            market=market,
            instrument_type="STOCK",
            is_managed=False,
            is_halted=False,
            foreign_positive_days_last_3=foreign_positive_days,
            foreign_positive_streak_last_3=foreign_positive_streak,
            foreign_net_buy_1d_krw=int(row["foreign_net_buy_1d_krw"]),
            foreign_net_buy_3d_krw=int(row["foreign_net_buy_3d_krw"]),
            foreign_sell_streak_days=foreign_sell_streak_days,
            institution_positive_days_last_3=institution_positive_days,
            institution_net_buy_1d_krw=int(row["institution_net_buy_1d_krw"]),
            institution_net_buy_3d_krw=int(row["institution_net_buy_3d_krw"]),
            institution_rank_percentile=float(row["institution_rank_percentile"]),
            institution_turned_to_sell=bool(institution_daily and institution_daily[-1] < 0),
            avg_trading_value_last_3d_krw=avg_last_3,
            avg_trading_value_prev_3d_krw=avg_prev_3,
            market_cap_krw=int(row["market_cap_krw"]),
            current_price=float(row["current_price"]),
            moving_average_20=float(recent_prices.mean()),
            highest_price_20d=float(recent_highs.max()),
            price_change_5d_pct=float(row["price_change_5d_pct"]),
            volume_spike=current_volume >= (avg_20_volume * 1.7 if avg_20_volume > 0 else float("inf")),
            source_snapshots=[source_snapshot],
        )
