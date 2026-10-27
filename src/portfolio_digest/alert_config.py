"""Validated alert rules in the shared schedule configuration."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class HoldingRules(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    daily_percent: Decimal = Field(default=Decimal("10"), gt=0, allow_inf_nan=False)
    stale_trading_days: int = Field(default=3, ge=0, le=3660)


class AlertConfig(HoldingRules):
    enabled: bool = True
    portfolio_drop_percent: Decimal = Field(default=Decimal("5"), gt=0, allow_inf_nan=False)
    period_days: int = Field(default=7, ge=1, le=3660)
    cooldown_hours: int | None = Field(default=None, ge=1)
    holdings: dict[str, HoldingRules] = Field(default_factory=dict)
