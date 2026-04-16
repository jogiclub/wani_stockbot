from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


KST_SCHEDULE_HOUR = 16
KST_SCHEDULE_MINUTE = 10


@dataclass(frozen=True)
class Settings:
    app_name: str = "wani_stockbot"
    app_env: str = os.getenv("APP_ENV", "local")
    timezone: str = os.getenv("APP_TIMEZONE", "Asia/Seoul")
    market_data_provider: str = os.getenv("MARKET_DATA_PROVIDER", "krx").lower()
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", "data/results"))
    state_dir: Path = Path(os.getenv("STATE_DIR", "data/state"))
    log_dir: Path = Path(os.getenv("LOG_DIR", "data/logs"))
    input_dir: Path = Path(os.getenv("INPUT_DIR", "data/input"))
    scheduled_input_file: Path = Path(os.getenv("SCHEDULED_INPUT_FILE", "data/input/daily_snapshot.json"))
    krx_id: str | None = os.getenv("KRX_ID")
    krx_pw: str | None = os.getenv("KRX_PW")
    schedule_hour: int = int(os.getenv("SCHEDULE_HOUR", KST_SCHEDULE_HOUR))
    schedule_minute: int = int(os.getenv("SCHEDULE_MINUTE", KST_SCHEDULE_MINUTE))
    recommendation_min_count: int = int(os.getenv("RECOMMENDATION_MIN_COUNT", "3"))
    recommendation_max_count: int = int(os.getenv("RECOMMENDATION_MAX_COUNT", "5"))


settings = Settings()
