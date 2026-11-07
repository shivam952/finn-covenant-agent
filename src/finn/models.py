"""Pydantic v2 data models for contracts, covenants, plan items, mappings, and results."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

QuantityKind = Literal[
    "earnings",
    "equity",
    "total_assets",
    "debt",
    "cash",
    "debt_service",
    "taxes",
    "other",
]


class Source(BaseModel):
    page: int
    section: str
    quote: str
    verified: bool | None = None
    verify_note: str | None = None  # code-only: quote_too_short | quote_not_found | quote_loose_match | quote_page_corrected
    original_page: int | None = None  # code-only: the page the LLM cited, if it was corrected


class Component(BaseModel):
    """One line of a contractual quantity definition."""

    name: str
    sign: Literal[1, -1]
    weight: float = 1.0
    ref: str | None = None
    definition: str
    # True when the definition removes this item from a broader term in the same quantity
    # ("X, ausgenommen Y" -> Y). Code subtracts it only from cells that actually contain it.
    carve_out: bool = False


class Quantity(BaseModel):
    """A named amount defined by the contract."""

    name: str
    kind: QuantityKind = "other"
    components: list[Component]
    source: Source


class Covenant(BaseModel):
    id: str
    name: str
    numerator: str
    denominator: str
    operator: Literal[">=", "<="]
    limit: float
    unit: Literal["percent", "ratio"]
    rounding_decimals: int | None
    test_basis: Literal["stichtag", "geschaeftsjahr"]
    source: Source
    definition_sources: list[Source]
    plan_basis_note: str | None


class ContractCovenants(BaseModel):
    contract_file: str
    contract_label: str
    has_covenants: bool
    reason_if_none: str | None
    reason_source: Source | None
    quantities: list[Quantity]
    covenants: list[Covenant]

    @model_validator(mode="after")
    def _validate_consistency(self) -> ContractCovenants:
        if not self.has_covenants:
            if self.covenants:
                raise ValueError("covenants must be empty when has_covenants is False")
            if not self.reason_if_none or not self.reason_if_none.strip():
                raise ValueError("reason_if_none must be set when has_covenants is False")
            return self

        if not self.covenants:
            raise ValueError("covenants must be non-empty when has_covenants is True")

        quantity_names = {q.name for q in self.quantities}
        for cov in self.covenants:
            if cov.numerator not in quantity_names:
                raise ValueError(
                    f"Covenant '{cov.id}' numerator '{cov.numerator}' not found in quantities"
                )
            if cov.denominator not in quantity_names:
                raise ValueError(
                    f"Covenant '{cov.id}' denominator '{cov.denominator}' not found in quantities"
                )

        for qty in self.quantities:
            for comp in qty.components:
                if comp.ref is not None and comp.ref not in quantity_names:
                    raise ValueError(
                        f"Component '{comp.name}' in quantity '{qty.name}' references unknown quantity '{comp.ref}'"
                    )

        return self


class PlanItem(BaseModel):
    id: str
    label: str
    sheet: str
    role: str = ""
    cell: str
    raw_value: float
    value: float
    is_expense: bool
    is_total: bool = False
    depends_on: list[str] = Field(default_factory=list)
    # For a "davon" sub-line: the id of the line it is part of (the nearest line above it).
    part_of: str | None = None


class CheckResult(BaseModel):
    """Structured result of a plausibility or formula cache check."""

    id: str
    description: str = ""
    passed: bool
    skipped: bool = False
    lhs: float | None = None
    rhs: float | None = None
    message_key: str
    params: dict[str, Any] = Field(default_factory=dict)
    blocking: bool = False


class TermMapping(BaseModel):
    contract_file: str
    quantity: str
    component: str
    item_ids: list[str]
    rationale: str


class StressResult(BaseModel):
    """Structured stress test result produced by the calculator."""

    kind: Literal[
        "earnings_decline",
        "equity_loss",
        "numerator_change",
        "already_breached",
        "not_applicable",
    ]
    driver_quantity: str
    pct: float | None = None
    amount: float | None = None
    limit: float
    operator: Literal[">=", "<="] = ">="
    unit: Literal["percent", "ratio"] = "ratio"
    rounding_decimals: int | None = None


class FormulaTerm(BaseModel):
    """One term in a numerator or denominator formula trace."""

    name: str
    sign: Literal[1, -1]
    weight: float = 1.0
    amount: float
    is_empty_leaf: bool = False


class FormulaParts(BaseModel):
    """Structured representation of a calculated ratio formula."""

    numerator_terms: list[FormulaTerm]
    denominator_terms: list[FormulaTerm]
    scale: float
    unit: Literal["percent", "ratio"]
    value_raw: float
    value_reported: float
    rounding_decimals: int | None


class Result(BaseModel):
    contract_label: str
    contract_file: str = ""
    covenant_id: str
    covenant_name: str
    numerator_value: float
    denominator_value: float
    value_raw: float
    value_reported: float
    value_defined: bool = True  # False if the denominator is not positive (e.g. EBITDA <= 0)
    limit: float
    operator: str
    unit: str
    headroom: float
    met: bool
    stress_value: float
    stress: StressResult
    formula: FormulaParts
    contract_sources: list[Source]
    plan_cells: list[str]
    plan_basis_note: str | None = None
    warnings: list[str] = Field(default_factory=list)


class PlanCellInfo(BaseModel):
    """Label and value of one plan cell used by a result (for the reply's source references)."""

    id: str
    sheet: str
    cell: str
    label: str
    value: float


class RunReport(BaseModel):
    results: list[Result]
    contracts_without_covenants: list[ContractCovenants]
    warnings: list[str]
    plan_meta: dict[str, Any] = Field(default_factory=dict)
    plan_cells: dict[str, PlanCellInfo] = Field(default_factory=dict)
    failed_contracts: list[dict[str, str]] = Field(default_factory=list)  # {"contract", "reason"}


_CODE_ONLY_SOURCE_FIELDS = ("verified", "verify_note", "original_page")


def _strip_code_only_fields(node: Any) -> None:
    """Recursively strip code-only Source fields ('verified', 'verify_note') from a JSON schema dict."""
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict) and "quote" in props and "page" in props:
            for field_name in _CODE_ONLY_SOURCE_FIELDS:
                props.pop(field_name, None)
            req = node.get("required")
            if isinstance(req, list):
                for field_name in _CODE_ONLY_SOURCE_FIELDS:
                    if field_name in req:
                        req.remove(field_name)
        for val in node.values():
            _strip_code_only_fields(val)
    elif isinstance(node, list):
        for item in node:
            _strip_code_only_fields(item)


def build_llm_json_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """Generate a JSON schema for LLM structured output with code-only fields excluded."""
    raw_schema = copy.deepcopy(schema.model_json_schema())
    _strip_code_only_fields(raw_schema)
    return raw_schema
