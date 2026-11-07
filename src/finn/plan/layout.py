"""Typed loader for the external business plan layout configuration."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from finn.config import PROJECT_ROOT


class PlanColumnConfig(BaseModel):
    """Words that mark a column header as the plan column (next to a 4-digit year in the same header)."""

    keywords: list[str] = Field(default_factory=list)


class RoleConfig(BaseModel):
    """Configuration for a logical workbook sheet role."""

    sheet_aliases: list[str] = Field(default_factory=list)
    content_keywords: list[str] = Field(default_factory=list)
    column_mode: Literal["plan_year", "all_numeric"]
    normalise_expenses: bool = False
    # "flow" = amounts over the period (income statement, payments); "stock" = balances at a date.
    measure: Literal["flow", "stock"] = "stock"
    # Labels of sub-item lines ("davon …") that carry the sign convention of the line above.
    sub_item_prefixes: list[str] = Field(default_factory=list)


class CheckTermConfig(BaseModel):
    """Single additive or subtractive term in a declarative plausibility check."""

    role: str
    label_all: list[str] = Field(default_factory=list)
    label_none: list[str] = Field(default_factory=list)
    column_all: list[str] = Field(default_factory=list)
    sign: Literal[1, -1] = 1


class CheckConfig(BaseModel):
    """Declarative equality check between two linear combinations of plan items."""

    id: str
    description: str
    lhs: list[CheckTermConfig]
    rhs: list[CheckTermConfig]
    tolerance: float = 0.5
    # A failed blocking check stops the covenant calculation: Finn replies that the plan is
    # inconsistent instead of reporting numbers it cannot certify. Skipped checks never block.
    blocking: bool = True


class PlanLayoutConfig(BaseModel):
    """Complete workbook layout and plausibility check configuration."""

    amount_unit: str = ""
    # Test date of the plan year (fiscal year end); {year} is replaced by the plan year.
    test_date_template: str  # required in the layout config, e.g. "31.12.{year}"
    cover_unit_markers: list[str] = Field(default_factory=list)
    unit_candidates: list[str] = Field(default_factory=list)
    plan_column: PlanColumnConfig = Field(default_factory=PlanColumnConfig)
    roles: dict[str, RoleConfig] = Field(default_factory=dict)
    checks: list[CheckConfig] = Field(default_factory=list)


def load_plan_layout(path: Path | None = None) -> PlanLayoutConfig:
    """Load the plan layout configuration from a TOML file."""
    if path is None:
        env_val = os.environ.get("PLAN_LAYOUT", "").strip()
        if env_val and not env_val.startswith("#"):
            p = Path(env_val)
            path = p if p.is_absolute() else (PROJECT_ROOT / p).resolve()
        else:
            path = PROJECT_ROOT / "config" / "plan_layout.toml"
    else:
        path = Path(path)

    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return PlanLayoutConfig.model_validate(raw)
