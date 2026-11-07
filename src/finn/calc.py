"""Deterministic calculation of quantities, covenant ratios, headroom, and stress tests."""

from __future__ import annotations

import json
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from finn.fmt import round_kaufmaennisch
from finn.models import (
    ContractCovenants,
    Covenant,
    FormulaParts,
    FormulaTerm,
    Quantity,
    Result,
    Source,
    StressResult,
    TermMapping,
)
from finn.plan.reader import PlanData, read_plan
from finn.render import render_stress


class CalcError(Exception):
    """Raised when a deterministic covenant calculation fails due to invalid mappings, cycles, or division by zero."""


@dataclass
class ComponentTrace:
    """Trace of one evaluated component within a quantity."""

    component_name: str
    sign: int
    weight: float
    raw_amount: float
    weighted_signed_amount: float
    item_ids: list[str] = field(default_factory=list)
    ref_quantity: str | None = None


@dataclass
class QuantityEval:
    """Evaluated quantity with its total value, component traces, cells, and used quantity names."""

    name: str
    value: float
    traces: list[ComponentTrace]
    plan_cells: list[str]
    used_quantities: list[str]


def _dedup_preserve(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _dedup_sources(sources: list[Source]) -> list[Source]:
    seen: set[tuple[int, str, str]] = set()
    out: list[Source] = []
    for s in sources:
        key = (s.page, s.section, s.quote)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _eval_quantity(
    qty_name: str,
    quantities_by_name: dict[str, Quantity],
    contract_file: str,
    mappings: list[TermMapping],
    plan: PlanData,
    visiting: frozenset[str] = frozenset(),
    overrides: dict[str, float] | None = None,
) -> QuantityEval:
    if qty_name in visiting:
        raise CalcError(f"Zyklische Referenz in Größen-Definition erkannt: {qty_name}")
    if qty_name not in quantities_by_name:
        raise CalcError(
            f"Größe '{qty_name}' ist im Vertrag '{contract_file}' nicht definiert."
        )

    qty = quantities_by_name[qty_name]
    if overrides and qty_name.lower() in overrides:
        override_val = overrides[qty_name.lower()]
        return QuantityEval(
            name=qty_name,
            value=override_val,
            traces=[
                ComponentTrace(
                    component_name=qty_name,
                    sign=1,
                    weight=1.0,
                    raw_amount=override_val,
                    weighted_signed_amount=override_val,
                )
            ],
            plan_cells=[],
            used_quantities=[qty_name],
        )

    next_visiting = visiting | {qty_name}
    total = 0.0
    traces: list[ComponentTrace] = []
    cells: list[str] = []
    used_quantities: list[str] = [qty_name]

    for comp in qty.components:
        if comp.ref is not None:
            sub = _eval_quantity(
                comp.ref,
                quantities_by_name,
                contract_file,
                mappings,
                plan,
                next_visiting,
                overrides,
            )
            raw_amount = sub.value
            weighted_signed = comp.sign * comp.weight * raw_amount
            total += weighted_signed
            traces.append(
                ComponentTrace(
                    component_name=comp.name,
                    sign=comp.sign,
                    weight=comp.weight,
                    raw_amount=raw_amount,
                    weighted_signed_amount=weighted_signed,
                    item_ids=list(sub.plan_cells),
                    ref_quantity=comp.ref,
                )
            )
            cells.extend(sub.plan_cells)
            used_quantities.extend(sub.used_quantities)
        else:
            matches = [
                m
                for m in mappings
                if m.contract_file == contract_file
                and m.quantity == qty_name
                and m.component == comp.name
            ]
            if len(matches) == 0:
                raise CalcError(
                    f"Keine Zuordnung (TermMapping) für Komponente '{comp.name}' "
                    f"in Größe '{qty_name}' ({contract_file}) gefunden."
                )
            if len(matches) > 1:
                raise CalcError(
                    f"Mehrdeutige Zuordnung ({len(matches)} Treffer) für Komponente '{comp.name}' "
                    f"in Größe '{qty_name}' ({contract_file})."
                )
            mapping = matches[0]
            comp_sum = 0.0
            for item_id in mapping.item_ids:
                if item_id not in plan.items:
                    raise CalcError(
                        f"Unbekannte Plan-Zelle '{item_id}' in Zuordnung für Komponente '{comp.name}'."
                    )
                comp_sum += plan.items[item_id].value

            weighted_signed = comp.sign * comp.weight * comp_sum
            total += weighted_signed
            traces.append(
                ComponentTrace(
                    component_name=comp.name,
                    sign=comp.sign,
                    weight=comp.weight,
                    raw_amount=comp_sum,
                    weighted_signed_amount=weighted_signed,
                    item_ids=list(mapping.item_ids),
                    ref_quantity=None,
                )
            )
            cells.extend(mapping.item_ids)

    return QuantityEval(
        name=qty_name,
        value=total,
        traces=traces,
        plan_cells=_dedup_preserve(cells),
        used_quantities=_dedup_preserve(used_quantities),
    )


def _traces_to_formula_terms(traces: list[ComponentTrace]) -> list[FormulaTerm]:
    return [
        FormulaTerm(
            name=t.component_name,
            sign=1 if t.sign >= 0 else -1,
            weight=t.weight,
            amount=t.raw_amount,
            is_empty_leaf=(t.ref_quantity is None and len(t.item_ids) == 0),
        )
        for t in traces
    ]


def _build_formula_parts(
    cov: Covenant,
    num_eval: QuantityEval,
    den_eval: QuantityEval,
    scale: float,
    value_raw: float,
    value_reported: float,
) -> FormulaParts:
    return FormulaParts(
        numerator_terms=_traces_to_formula_terms(num_eval.traces),
        denominator_terms=_traces_to_formula_terms(den_eval.traces),
        scale=scale,
        unit=cov.unit,
        value_raw=value_raw,
        value_reported=value_reported,
        rounding_decimals=cov.rounding_decimals,
    )


def _is_raw_met(operator: str, value_raw: float, limit: float) -> bool:
    if operator == ">=":
        return value_raw >= limit
    return value_raw <= limit


def _find_closest_earnings_quantity(
    cov: Covenant,
    quantities_by_name: dict[str, Quantity],
) -> str | None:
    """Return the quantity name with kind == 'earnings' closest to the covenant root via BFS."""
    queue: deque[str] = deque([cov.numerator, cov.denominator])
    visited: set[str] = set()
    while queue:
        q_name = queue.popleft()
        if q_name in visited or q_name not in quantities_by_name:
            continue
        visited.add(q_name)
        qty = quantities_by_name[q_name]
        if qty.kind == "earnings":
            return q_name
        for comp in qty.components:
            if comp.ref is not None and comp.ref not in visited:
                queue.append(comp.ref)
    return None


def _compute_stress(
    cov: Covenant,
    num_eval: QuantityEval,
    den_eval: QuantityEval,
    value_raw: float,
    quantities_by_name: dict[str, Quantity],
    contract_file: str,
    mappings: list[TermMapping],
    plan: PlanData,
) -> tuple[float, StressResult]:
    """Compute numeric stress_value and structured StressResult based on value_raw without rounding benefit."""
    if not _is_raw_met(cov.operator, value_raw, cov.limit):
        diff = abs(value_raw - cov.limit)
        return (
            -diff,
            StressResult(
                kind="already_breached",
                driver_quantity=cov.name,
                pct=diff if cov.unit == "percent" else None,
                amount=diff if cov.unit != "percent" else None,
                limit=cov.limit,
                operator=cov.operator,
                unit=cov.unit,
                rounding_decimals=cov.rounding_decimals,
            ),
        )

    earnings_qty_name = _find_closest_earnings_quantity(cov, quantities_by_name)
    if earnings_qty_name is not None:
        earnings_eval = _eval_quantity(
            earnings_qty_name, quantities_by_name, contract_file, mappings, plan
        )
        earnings_base = earnings_eval.value
        if earnings_base <= 0:
            return 0.0, StressResult(
                kind="not_applicable",
                driver_quantity=earnings_qty_name,
                limit=cov.limit,
                operator=cov.operator,
                unit=cov.unit,
                rounding_decimals=cov.rounding_decimals,
            )

        override_key = earnings_qty_name.lower()

        def ratio_at_drop(drop_frac: float) -> float:
            ov = {override_key: earnings_base * (1.0 - drop_frac)}
            n_ev = _eval_quantity(
                cov.numerator,
                quantities_by_name,
                contract_file,
                mappings,
                plan,
                overrides=ov,
            )
            d_ev = _eval_quantity(
                cov.denominator,
                quantities_by_name,
                contract_file,
                mappings,
                plan,
                overrides=ov,
            )
            if abs(d_ev.value) < 1e-12:
                return float("inf") if cov.operator == "<=" else float("-inf")
            scale = 100.0 if cov.unit == "percent" else 1.0
            return (n_ev.value / d_ev.value) * scale

        lo = 0.0
        hi = 1.0 - 1e-9
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            r_mid = ratio_at_drop(mid)
            if _is_raw_met(cov.operator, r_mid, cov.limit):
                lo = mid
            else:
                hi = mid

        drop_pct = 0.5 * (lo + hi) * 100.0
        drop_abs = earnings_base * (drop_pct / 100.0)
        stress_val = round_kaufmaennisch(drop_pct, 2)
        return (
            stress_val,
            StressResult(
                kind="earnings_decline",
                driver_quantity=earnings_qty_name,
                pct=drop_pct,
                amount=drop_abs,
                limit=cov.limit,
                operator=cov.operator,
                unit=cov.unit,
                rounding_decimals=cov.rounding_decimals,
            ),
        )

    has_equity_in_numerator = any(
        quantities_by_name[q].kind == "equity"
        for q in num_eval.used_quantities
        if q in quantities_by_name
    )
    if cov.unit == "percent" and has_equity_in_numerator:
        p = cov.limit / 100.0
        if abs(1.0 - p) < 1e-12:
            raise CalcError("Ungültiger Prozent-Grenzwert 100 % für Stresstest.")
        loss_abs = (num_eval.value - p * den_eval.value) / (1.0 - p)
        stress_val = round_kaufmaennisch(loss_abs, 2)
        return (
            stress_val,
            StressResult(
                kind="equity_loss",
                driver_quantity=cov.numerator,
                pct=None,
                amount=loss_abs,
                limit=cov.limit,
                operator=cov.operator,
                unit=cov.unit,
                rounding_decimals=cov.rounding_decimals,
            ),
        )

    if cov.operator == ">=":
        delta = num_eval.value - cov.limit * den_eval.value
    else:
        delta = cov.limit * den_eval.value - num_eval.value
    stress_val = round_kaufmaennisch(delta, 2)
    return (
        stress_val,
        StressResult(
            kind="numerator_change",
            driver_quantity=cov.numerator,
            pct=None,
            amount=delta,
            limit=cov.limit,
            operator=cov.operator,
            unit=cov.unit,
            rounding_decimals=cov.rounding_decimals,
        ),
    )


def _undefined_result(
    contract: ContractCovenants,
    cov: Covenant,
    num_eval: QuantityEval,
    den_eval: QuantityEval,
    met: bool,
    quantities_by_name: dict[str, Quantity],
) -> Result:
    """Result for a ratio whose denominator is not positive: no value, no headroom, no stress test."""
    used = _dedup_preserve(num_eval.used_quantities + den_eval.used_quantities)
    sources = _dedup_sources(
        [cov.source, *cov.definition_sources, *[quantities_by_name[q].source for q in used]]
    )
    scale = 100.0 if cov.unit == "percent" else 1.0
    formula = _build_formula_parts(cov, num_eval, den_eval, scale, 0.0, 0.0)
    return Result(
        contract_label=contract.contract_label,
        contract_file=contract.contract_file,
        plan_basis_note=cov.plan_basis_note,
        covenant_id=cov.id,
        covenant_name=cov.name,
        numerator_value=num_eval.value,
        denominator_value=den_eval.value,
        value_raw=0.0,
        value_reported=0.0,
        value_defined=False,
        limit=cov.limit,
        operator=cov.operator,
        unit=cov.unit,
        headroom=0.0,
        met=met,
        stress_value=0.0,
        stress=StressResult(
            kind="not_applicable",
            driver_quantity=cov.denominator,
            limit=cov.limit,
            operator=cov.operator,
            unit=cov.unit,
            rounding_decimals=cov.rounding_decimals,
        ),
        formula=formula,
        contract_sources=sources,
        plan_cells=_dedup_preserve(num_eval.plan_cells + den_eval.plan_cells),
    )


def evaluate_contract(
    contract: ContractCovenants,
    mappings: list[TermMapping],
    plan: PlanData,
) -> list[Result]:
    """Deterministically evaluate all covenants of a single contract against the parsed plan."""
    if not contract.has_covenants:
        return []

    quantities_by_name = {q.name: q for q in contract.quantities}
    results: list[Result] = []

    for cov in contract.covenants:
        num_eval = _eval_quantity(
            cov.numerator,
            quantities_by_name,
            contract.contract_file,
            mappings,
            plan,
        )
        den_eval = _eval_quantity(
            cov.denominator,
            quantities_by_name,
            contract.contract_file,
            mappings,
            plan,
        )

        scale = 100.0 if cov.unit == "percent" else 1.0
        if den_eval.value <= 1e-12:
            # A ratio over a non-positive denominator (e.g. negative EBITDA) has no meaningful
            # value. A maximum covenant cannot be met; a minimum covenant is met only if there is
            # nothing to cover (denominator exactly 0) and the numerator is positive.
            if cov.operator == "<=":
                undefined_met = False
            else:
                undefined_met = abs(den_eval.value) < 1e-12 and num_eval.value > 0
            results.append(
                _undefined_result(contract, cov, num_eval, den_eval, undefined_met, quantities_by_name)
            )
            continue

        value_raw = (num_eval.value / den_eval.value) * scale

        if cov.rounding_decimals is not None:
            value_reported = round_kaufmaennisch(value_raw, cov.rounding_decimals)
        else:
            value_reported = value_raw

        if cov.operator == ">=":
            met = value_reported >= cov.limit
            headroom = value_reported - cov.limit
        else:
            met = value_reported <= cov.limit
            headroom = cov.limit - value_reported

        if cov.rounding_decimals is not None:
            headroom = round_kaufmaennisch(headroom, cov.rounding_decimals)

        stress_value, stress_struct = _compute_stress(
            cov,
            num_eval,
            den_eval,
            value_raw,
            quantities_by_name,
            contract.contract_file,
            mappings,
            plan,
        )

        formula_struct = _build_formula_parts(
            cov, num_eval, den_eval, scale, value_raw, value_reported
        )

        used_qty_names = _dedup_preserve(
            num_eval.used_quantities + den_eval.used_quantities
        )
        qty_sources = [quantities_by_name[q_name].source for q_name in used_qty_names]
        contract_sources = _dedup_sources(
            [cov.source, *cov.definition_sources, *qty_sources]
        )
        plan_cells = _dedup_preserve(num_eval.plan_cells + den_eval.plan_cells)

        # Quote remarks are reported once, by the extraction step (see contracts/extract.py).
        cov_warnings: list[str] = []

        results.append(
            Result(
                contract_label=contract.contract_label,
                contract_file=contract.contract_file,
                plan_basis_note=cov.plan_basis_note,
                covenant_id=cov.id,
                covenant_name=cov.name,
                numerator_value=num_eval.value,
                denominator_value=den_eval.value,
                value_raw=value_raw,
                value_reported=value_reported,
                limit=cov.limit,
                operator=cov.operator,
                unit=cov.unit,
                headroom=headroom,
                met=met,
                stress_value=stress_value,
                stress=stress_struct,
                formula=formula_struct,
                contract_sources=contract_sources,
                plan_cells=plan_cells,
                warnings=cov_warnings,
            )
        )

    return results


def _main(argv: list[str]) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    if len(argv) > 1:
        xlsx_path = Path(argv[1])
    else:
        xlsx_path = sorted((repo_root / "data" / "samples").glob("*.xlsx"))[0]

    cov_path = repo_root / "tests" / "fixtures" / "golden_covenants.json"
    kinds_path = repo_root / "tests" / "fixtures" / "golden_kinds.json"
    map_path = repo_root / "tests" / "fixtures" / "golden_mapping.json"

    raw_contracts = json.loads(cov_path.read_text(encoding="utf-8"))
    kinds_overlay: dict[str, dict[str, str]] = (
        json.loads(kinds_path.read_text(encoding="utf-8"))
        if kinds_path.exists()
        else {}
    )
    for c_dict in raw_contracts:
        c_kinds = kinds_overlay.get(c_dict.get("contract_file", ""), {})
        for q_dict in c_dict.get("quantities", []):
            if q_dict.get("name") in c_kinds:
                q_dict["kind"] = c_kinds[q_dict["name"]]

    contracts = [ContractCovenants.model_validate(c) for c in raw_contracts]
    mappings = [
        TermMapping.model_validate(m)
        for m in json.loads(map_path.read_text(encoding="utf-8"))
    ]
    plan = read_plan(xlsx_path)
    unit = str(plan.meta.get("amount_unit", ""))

    header = f"{'covenant id':<10} | {'value_raw':>10} | {'value_reported':>14} | {'limit':>8} | {'headroom':>10} | {'met':<5} | stress"
    print(header)
    print("-" * len(header))
    for contract in contracts:
        for r in evaluate_contract(contract, mappings, plan):
            stress_str = render_stress(r.stress, lang="de", unit=unit)
            print(
                f"{r.covenant_id:<10} | {r.value_raw:>10.4f} | {r.value_reported:>14.4f} | "
                f"{r.limit:>8.2f} | {r.headroom:>10.4f} | {str(r.met):<5} | {stress_str}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
