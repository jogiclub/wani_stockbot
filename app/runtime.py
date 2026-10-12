from __future__ import annotations

from app.config import settings
from app.providers import KrxMarketDataProvider, LocalJsonMarketDataProvider, MarketDataProvider
from app.repositories import FileRepository
from app.services import SourceVerifier, StockSelector


repository = FileRepository(
    output_dir=settings.output_dir,
    state_dir=settings.state_dir,
    log_dir=settings.log_dir,
)
selector = StockSelector(repository=repository, verifier=SourceVerifier())

if settings.market_data_provider == "local":
    provider: MarketDataProvider = LocalJsonMarketDataProvider(input_file=settings.scheduled_input_file)
else:
    provider = KrxMarketDataProvider(login_id=settings.krx_id, login_password=settings.krx_pw)


def get_selector() -> StockSelector:
    return selector


def get_provider() -> MarketDataProvider:
    return provider
