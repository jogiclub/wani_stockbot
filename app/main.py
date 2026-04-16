from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.runtime import provider, selector
from app.scheduler import DailyScheduler


scheduler = DailyScheduler(selector=selector, provider=provider)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await scheduler.start()
    try:
        yield
    finally:
        await scheduler.stop()


app = FastAPI(title="wani_stockbot", lifespan=lifespan)
app.include_router(router)
