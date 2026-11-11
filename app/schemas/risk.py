from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Ticker = Annotated[str, StringConstraints(pattern=r"^[0-9A-Z]{6}$", strict=True)]


def display(value: float) -> float:
    # Provisional presentation policy, separate from raw grade classification.
    return float(Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: Ticker
    weight: Decimal = Field(gt=0, le=1, allow_inf_nan=False)

    @field_validator("weight", mode="before")
    @classmethod
    def numeric_weight(cls, value):
        if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
            raise ValueError("weight must be a JSON number")
        return value


class PackageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Item] = Field(min_length=1, max_length=77)


class GradeResponse(BaseModel):
    ticker: str
    var975: float
    riskGrade: int
    volatility: float
    priceFrom: date
    priceTo: date
    asOf: datetime
    analysisVersion: int


class PackageResponse(BaseModel):
    volatility: float
    asOf: datetime


class StockClassificationResponse(BaseModel):
    ticker: Ticker
    riskGrade: int = Field(ge=1, le=2)
    asOf: datetime
    basis: Literal['STOCK_RULE_CURRENT_STATUS'] = 'STOCK_RULE_CURRENT_STATUS'
