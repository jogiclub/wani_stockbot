from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from app.models import RecommendationRecord


class FileRepository:
    def __init__(self, output_dir: Path, state_dir: Path, log_dir: Path) -> None:
        self.output_dir = output_dir
        self.state_dir = state_dir
        self.log_dir = log_dir
        for directory in (self.output_dir, self.state_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def save_recommendation(self, record: RecommendationRecord) -> Path:
        target = self.output_dir / f"{record.run_date}.json"
        target.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self._write_latest_state(record)
        self._append_audit_log(record)
        return target

    def load_recent_recommendation_codes(self, days: int = 3) -> set[str]:
        state_file = self.state_dir / "recent_recommendations.json"
        if not state_file.exists():
            return set()
        payload = json.loads(state_file.read_text(encoding="utf-8"))
        cutoff_items = payload.get("items", [])[:days]
        codes: set[str] = set()
        for item in cutoff_items:
            codes.update(item.get("codes", []))
        return codes

    def _write_latest_state(self, record: RecommendationRecord) -> None:
        state_file = self.state_dir / "recent_recommendations.json"
        previous_items: list[dict] = []
        if state_file.exists():
            payload = json.loads(state_file.read_text(encoding="utf-8"))
            previous_items = payload.get("items", [])

        selected_codes = [candidate.code for candidate in record.candidates if candidate.selected]
        current_item = {
            "run_date": record.run_date,
            "codes": selected_codes,
            "generated_at": record.generated_at.isoformat(),
        }
        items = [current_item] + [item for item in previous_items if item.get("run_date") != record.run_date]
        state_file.write_text(
            json.dumps({"items": items[:30]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _append_audit_log(self, record: RecommendationRecord) -> None:
        timestamp = datetime.now().strftime("%Y%m%d")
        log_file = self.log_dir / f"{timestamp}.jsonl"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.model_dump(mode="json"), ensure_ascii=False))
            handle.write("\n")

