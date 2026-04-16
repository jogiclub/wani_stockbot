from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException

from app.models import OutputItem, RecommendationRecord, ScreeningRequest, ScreeningResponse
from app.providers import MarketDataProviderError
from app.runtime import get_provider, get_selector
from app.services import StockSelector


router = APIRouter()


@router.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


def _to_response(record: RecommendationRecord) -> ScreeningResponse:
    items = [
        OutputItem(
            stock_name=candidate.name,
            foreign_flow=candidate.foreign_trend,
            institution_flow=candidate.institution_trend,
            trading_value=candidate.trading_value_summary,
            price_position=candidate.price_position_summary,
            score=candidate.score,
            opinion=candidate.advice.value,
            data_source=candidate.source_summary,
            reasons=candidate.reasons,
            warnings=candidate.warnings,
        )
        for candidate in record.candidates
        if candidate.selected
    ]
    return ScreeningResponse(generated_at=record.generated_at, run_date=record.run_date, items=items)


@router.post("/screen", response_model=ScreeningResponse)
async def screen_stocks(
    request: ScreeningRequest,
    selector: StockSelector = Depends(get_selector),
) -> ScreeningResponse:
    record = selector.run(snapshots=request.snapshots, run_date=request.run_date)
    return _to_response(record)


@router.post("/screen/live", response_model=ScreeningResponse)
async def screen_live_stocks(
    selector: StockSelector = Depends(get_selector),
    provider=Depends(get_provider),
) -> ScreeningResponse:
    try:
        run_date, snapshots = provider.load()
    except MarketDataProviderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not snapshots:
        raise HTTPException(status_code=503, detail="Market data provider returned no snapshots.")

    record = selector.run(snapshots=snapshots, run_date=run_date)
    return _to_response(record)
