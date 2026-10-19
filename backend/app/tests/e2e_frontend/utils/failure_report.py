from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class FailureReport:
    case_id: str
    question: str
    oracle_rows: list[dict[str, Any]]
    stream_rows: list[dict[str, Any]]
    screen_rows: list[dict[str, Any]]
    status: str
    message: str
    backend_log_note: str = (
        "Backend log matching is not collected: successful requests do not log "
        "request_id, and no stable backend log path was configured."
    )
    directory: str = "backend/app/tests/e2e_frontend/reports"

    def save(self) -> Path:
        path = Path(self.directory)
        path.mkdir(parents=True, exist_ok=True)
        report_path = path / f"failure_{self.case_id.lower()}_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.json"
        report_path.write_text(json.dumps(asdict(self), indent=2, default=str), encoding="utf-8")
        return report_path
