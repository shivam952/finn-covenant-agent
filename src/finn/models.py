"""Pydantic v2 data models for contracts, covenants, plan items, mappings, and results."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class Source(BaseModel):
    page: int                    # PDF page number, 1-based
    section: str                 # e.g. "§ 13 Abs. 1", "§ 1 Abs. 1", "Anlage 3 Abs. 6"
    quote: str                   # contract text word for word (short, the defining sentence)


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


class PlanItem(BaseModel):
    id: str                      # equals the cell reference, e.g. "GuV!D17"
    label: str                   # row label (Finanzierung sheet: "row label – column header")
    sheet: str
    cell: str                    # "D17"
    raw_value: float             # as stored in Excel (expenses are negative there)
    value: float                 # normalised: expense input lines as positive amounts, totals keep their sign
    is_expense: bool


class TermMapping(BaseModel):
    quantity: str
    component: str
    item_ids: list[str]          # PlanItem ids only; summed
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

