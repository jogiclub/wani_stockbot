from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timedelta, timezone
import asyncio
import logging

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings
from app.models import RecommendationRecord
from app.providers import MarketDataProvider, MarketDataProviderError
from app.services import StockSelector


logger = logging.getLogger(__name__)


class DailyScheduler:
    def __init__(self, selector: StockSelector, provider: MarketDataProvider) -> None:
        self.selector = selector
        self.provider = provider
        self._task: asyncio.Task | None = None
        self._timezone = self._build_timezone()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def run_once(self) -> RecommendationRecord:
        run_date, snapshots = self.provider.load()
        return self.selector.run(snapshots=snapshots, run_date=run_date)

    async def _run_forever(self) -> None:
        while True:
            delay = self._seconds_until_next_run()
            await asyncio.sleep(delay)
            try:
                run_date, snapshots = self.provider.load()
            except MarketDataProviderError as exc:
                logger.warning("Scheduled load skipped: %s", exc)
                continue
            if snapshots:
                self.selector.run(snapshots=snapshots, run_date=run_date)

    def _seconds_until_next_run(self) -> float:
        now = datetime.now(self._timezone)
        scheduled = now.replace(
            hour=settings.schedule_hour,
            minute=settings.schedule_minute,
            second=0,
            microsecond=0,
        )
        if now >= scheduled:
            scheduled = scheduled + timedelta(days=1)
        return max((scheduled - now).total_seconds(), 1.0)

    @staticmethod
    def _build_timezone():
        try:
            return ZoneInfo(settings.timezone)
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=9))
