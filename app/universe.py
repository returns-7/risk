import csv
import re
from datetime import date
from dataclasses import dataclass
from pathlib import Path

from app.domain import RiskError


@dataclass(frozen=True)
class Instrument:
    ticker: str
    name: str
    status: str
    kind: str = ''
    inception: date | None = None
    asset_grade: int | None = None
    leveraged_or_inverse: bool | None = None
    high_difficulty: bool = False


def load_universe(path: Path) -> dict[str, Instrument]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            entries = []
            for row in csv.DictReader(stream):
                kind = row.get('kind', '')
                leverage = row.get('leveraged_or_inverse', '')
                difficulty = row.get('high_difficulty') or ''
                if difficulty not in {'', 'true', 'false'}:
                    raise ValueError
                if kind not in {'', 'STOCK', 'ETF', 'FUND'} or leverage not in {'', 'true', 'false'}:
                    raise ValueError
                inception = row.get('inception_date', '')
                asset_grade = row.get('asset_grade', '')
                if asset_grade and asset_grade not in {'1', '2', '3', '4', '5', '6'}:
                    raise ValueError
                entries.append(Instrument(row['ticker'], row['name'], row['status'], kind,
                    date.fromisoformat(inception) if inception else None,
                    int(asset_grade) if asset_grade else None,
                    leverage == 'true' if leverage else None, difficulty == 'true'))
        if not entries or len({e.ticker for e in entries}) != len(entries):
            raise ValueError
        if any(not re.fullmatch(r"[0-9A-Z]{6}", e.ticker) or e.status not in ("active", "code_mismatch", "stale") for e in entries):
            raise ValueError
        return {e.ticker: e for e in entries}
    except (OSError, KeyError, ValueError) as exc:
        raise RiskError("UNIVERSE_UNAVAILABLE", "종목 목록 파일을 확인하세요.", 503) from exc
