"""Pydantic v2 data models for contracts, covenants, plan items, mappings, and results."""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, model_validator


class Source(BaseModel):
    page: int                    # PDF page number, 1-based
    section: str                 # e.g. "§ 13 Abs. 1", "§ 1 Abs. 1", "Anlage 3 Abs. 6"
    quote: str                   # contract text word for word (short, the defining sentence)
    verified: bool | None = None
    verify_note: str | None = None


class Component(BaseModel):
    """One line of a definition, e.g. 'zuzüglich der Steuern vom Einkommen und vom Ertrag'."""

    name: str                    # short German label, e.g. "Steuern vom Einkommen und vom Ertrag"
    sign: Literal[1, -1]
    weight: float = 1.0          # e.g. 0.5 for "50 % der Sonderposten"
    ref: str | None = None       # name of another Quantity if this line refers to one (e.g. "EBITDA")
    definition: str              # what the contract says is included/excluded for this line


class Quantity(BaseModel):
    """A named amount defined by the contract, e.g. 'EBITDA', 'Schuldendienst', 'Wirtschaftliches Eigenkapital'."""

    name: str
    components: list[Component]
    source: Source


class Covenant(BaseModel):
    id: str                      # e.g. "V4-EKQ"
    name: str                    # e.g. "Eigenkapitalquote"
    numerator: str               # Quantity name
    denominator: str             # Quantity name
    operator: Literal[">=", "<="]
    limit: float                 # in the unit below: 27.5 for 27,5 %, 1.30 for a ratio
    unit: Literal["percent", "ratio"]
    rounding_decimals: int | None    # None if the contract states no rounding rule
    test_basis: Literal["stichtag", "geschaeftsjahr"]   # balance-sheet date vs. fiscal-year period
    source: Source                # where the obligation + limit is stated
    definition_sources: list[Source]   # where the definitions are
    plan_basis_note: str | None   # what the contract says about a breach on PLAN figures (early warning vs. default)


class ContractCovenants(BaseModel):
    contract_file: str
    contract_label: str          # e.g. "Vertrag 3 – Landesbank Mittelland, Investitionsdarlehen LBM-INV-2025-0412"
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
    id: str                      # equals the cell reference, e.g. "GuV!D17"
    label: str                   # row label (Finanzierung sheet: "row label – column header")
    sheet: str
    cell: str                    # "D17"
    raw_value: float             # as stored in Excel (expenses are negative there)
    value: float                 # normalised: expense input lines as positive amounts, totals keep their sign
    is_expense: bool
    is_total: bool = False       # True if the cell is a formula in Excel
    depends_on: list[str] = []   # all PlanItem ids the formula depends on, transitively


class TermMapping(BaseModel):
    contract_file: str
    quantity: str
    component: str
    item_ids: list[str]          # may be EMPTY = this line has no plan value (e.g. no extraordinary items) → contributes 0
    rationale: str               # one German sentence why these cells match the definition


class Result(BaseModel):
    contract_label: str
    covenant_id: str
    covenant_name: str
    numerator_value: float
    denominator_value: float
    value_raw: float
    value_reported: float        # rounded per contract; raw if no rule
    limit: float
    operator: str
    unit: str
    headroom: float              # positive = buffer, negative = breach (in percentage points for percent)
    met: bool
    stress_text: str             # German, e.g. "EBITDA dürfte um 15,5 % sinken, bevor der Covenant bricht."
    stress_value: float
    formula_text: str            # filled-in formula, e.g. "(40.200 − 3.200) / 23.680 = 1,5625 → 1,56"
    contract_sources: list[Source]
    plan_cells: list[str]
    warnings: list[str] = []


class RunReport(BaseModel):
    results: list[Result]
    contracts_without_covenants: list[ContractCovenants]
    warnings: list[str]


_CODE_ONLY_SOURCE_FIELDS = ("verified", "verify_note")


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
