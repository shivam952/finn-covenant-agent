"""Deterministic calculation of quantities, covenant ratios, headroom, and stress tests."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from finn.fmt import fmt_num, fmt_pct, fmt_teur, round_kaufmaennisch
from finn.models import (
    ContractCovenants,
    Covenant,
    Quantity,
    Result,
    Source,
    TermMapping,
)
from finn.plan.reader import PlanData, read_plan


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


def _fmt_amount_auto(x: float) -> str:
    """Format a plan amount in German notation without decimals if integer, else 2 decimals."""
    if abs(x - round(x)) < 1e-9:
        return fmt_num(x, 0)
    return fmt_num(x, 2)


def _format_quantity_expr(q_eval: QuantityEval) -> str:
    """Format a quantity's top-level component traces as a human-readable arithmetic expression."""
    active = [
        t for t in q_eval.traces if not (len(t.item_ids) == 0 and abs(t.raw_amount) < 1e-9)
    ]
    if not active:
        return "0"

    parts: list[str] = []
    for idx, t in enumerate(active):
        amt = t.raw_amount
        effective_sign = t.sign if amt >= 0 else -t.sign
        abs_amt_str = _fmt_amount_auto(abs(amt))
        if abs(t.weight - 1.0) > 1e-9:
            w_decimals = 1 if abs(t.weight * 10 - round(t.weight * 10)) < 1e-9 else 2
            term_str = f"{fmt_num(t.weight, w_decimals)} × {abs_amt_str}"
        else:
            term_str = abs_amt_str

        if idx == 0:
            parts.append(f"-{term_str}" if effective_sign < 0 else term_str)
        else:
            op = "+" if effective_sign > 0 else "−"
            parts.append(f"{op} {term_str}")

    joined = " ".join(parts)
    if len(active) > 1:
        return f"({joined})"
    return joined


def _build_formula_text(
    cov: Covenant,
    num_eval: QuantityEval,
    den_eval: QuantityEval,
    value_raw: float,
    value_reported: float,
) -> str:
    num_expr = _format_quantity_expr(num_eval)
    den_expr = _format_quantity_expr(den_eval)
    unit_suffix = " %" if cov.unit == "percent" else ""
    mult_part = " × 100" if cov.unit == "percent" else ""
    raw_str = f"{fmt_num(value_raw, 4)}{unit_suffix}"

    if cov.rounding_decimals is not None:
        rep_str = f"{fmt_num(value_reported, cov.rounding_decimals)}{unit_suffix}"
        return f"{num_expr} / {den_expr}{mult_part} = {raw_str} → gerundet {rep_str}"
    return f"{num_expr} / {den_expr}{mult_part} = {raw_str}"


def _is_raw_met(operator: str, value_raw: float, limit: float) -> bool:
    if operator == ">=":
        return value_raw >= limit
    return value_raw <= limit


def _compute_stress(
    cov: Covenant,
    num_eval: QuantityEval,
    den_eval: QuantityEval,
    value_raw: float,
    quantities_by_name: dict[str, Quantity],
    contract_file: str,
    mappings: list[TermMapping],
    plan: PlanData,
) -> tuple[float, str]:
    """Compute stress_value and German stress_text based on value_raw without rounding benefit."""
    all_used = _dedup_preserve(num_eval.used_quantities + den_eval.used_quantities)
    ebitda_qty_name = next((q for q in all_used if q.lower() == "ebitda"), None)
    limit_decimals = cov.rounding_decimals if cov.rounding_decimals is not None else (
        1 if cov.unit == "percent" else 2
    )
    limit_str = (
        fmt_pct(cov.limit, limit_decimals)
        if cov.unit == "percent"
        else fmt_num(cov.limit, limit_decimals)
    )

    if not _is_raw_met(cov.operator, value_raw, cov.limit):
        diff = abs(value_raw - cov.limit)
        diff_str = fmt_pct(diff, 2) if cov.unit == "percent" else fmt_num(diff, 2)
        direction = "unterschritten" if cov.operator == ">=" else "überschritten"
        return (
            -diff,
            f"Der Grenzwert von {limit_str} ist bereits um {diff_str} {direction}.",
        )

    if ebitda_qty_name is not None:
        ebitda_eval = _eval_quantity(
            ebitda_qty_name, quantities_by_name, contract_file, mappings, plan
        )
        ebitda_base = ebitda_eval.value
        if ebitda_base <= 0:
            raise CalcError("EBITDA-Ausgangswert für den Stresstest muss positiv sein.")

        def ratio_at_drop(drop_frac: float) -> float:
            ov = {"ebitda": ebitda_base * (1.0 - drop_frac)}
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
        drop_teur = ebitda_base * (drop_pct / 100.0)
        verb = "unterschritten" if cov.operator == ">=" else "überschritten"
        stress_text = (
            f"Das EBITDA dürfte um {fmt_pct(drop_pct, 1)} ({fmt_teur(drop_teur, 0)}) sinken, "
            f"bevor der Grenzwert von {limit_str} {verb} wird."
        )
        return round_kaufmaennisch(drop_pct, 2), stress_text

    if cov.unit == "percent":
        p = cov.limit / 100.0
        if abs(1.0 - p) < 1e-12:
            raise CalcError("Ungültiger Prozent-Grenzwert 100 % für Stresstest.")
        loss_teur = (num_eval.value - p * den_eval.value) / (1.0 - p)
        stress_text = (
            f"Ein zusätzlicher Jahresverlust von {fmt_teur(loss_teur, 0)} würde die Quote "
            f"unter {limit_str} drücken."
        )
        return round_kaufmaennisch(loss_teur, 2), stress_text

    # Fallback for generic ratio without EBITDA
    if cov.operator == ">=":
        delta = num_eval.value - cov.limit * den_eval.value
        stress_text = (
            f"Der Zähler dürfte um {fmt_teur(delta, 0)} sinken, "
            f"bevor der Grenzwert von {limit_str} unterschritten wird."
        )
    else:
        delta = cov.limit * den_eval.value - num_eval.value
        stress_text = (
            f"Der Zähler dürfte um {fmt_teur(delta, 0)} steigen, "
            f"bevor der Grenzwert von {limit_str} überschritten wird."
        )
    return round_kaufmaennisch(delta, 2), stress_text


def evaluate_contract(
    contract: ContractCovenants,
    mappings: list[TermMapping],
    plan: PlanData,
) -> list[Result]:
    """Deterministically evaluate all covenants of a single contract against the 2027 plan."""
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

        if abs(den_eval.value) < 1e-12:
            raise CalcError(
                f"Nenner '{cov.denominator}' für Covenant '{cov.id}' ist 0."
            )

        scale = 100.0 if cov.unit == "percent" else 1.0
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

        stress_value, stress_text = _compute_stress(
            cov,
            num_eval,
            den_eval,
            value_raw,
            quantities_by_name,
            contract.contract_file,
            mappings,
            plan,
        )

        formula_text = _build_formula_text(
            cov, num_eval, den_eval, value_raw, value_reported
        )

        used_qty_names = _dedup_preserve(
            num_eval.used_quantities + den_eval.used_quantities
        )
        qty_sources = [quantities_by_name[q_name].source for q_name in used_qty_names]
        contract_sources = _dedup_sources(
            [cov.source, *cov.definition_sources, *qty_sources]
        )
        plan_cells = _dedup_preserve(num_eval.plan_cells + den_eval.plan_cells)

        cov_warnings: list[str] = []
        for s in contract_sources:
            if s.verified is False:
                note = s.verify_note or "Zitat nicht verifiziert"
                cov_warnings.append(f"{s.section} (S. {s.page}): {note}")
            elif s.verify_note and s.verify_note.startswith("Seitenangabe korrigiert"):
                cov_warnings.append(f"{s.section}: {s.verify_note}")

        results.append(
            Result(
                contract_label=contract.contract_label,
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
                stress_text=stress_text,
                stress_value=stress_value,
                formula_text=formula_text,
                contract_sources=contract_sources,
                plan_cells=plan_cells,
                warnings=cov_warnings,
            )
        )

    return results


def _main(argv: list[str]) -> int:
    xlsx_path = (
        Path(argv[1])
        if len(argv) > 1
        else Path("data/samples/Wirtschaftsplan_2027.xlsx")
    )
    repo_root = Path(__file__).resolve().parents[2]
    cov_path = repo_root / "tests" / "fixtures" / "golden_covenants.json"
    map_path = repo_root / "tests" / "fixtures" / "golden_mapping.json"

    contracts = [
        ContractCovenants.model_validate(c)
        for c in json.loads(cov_path.read_text(encoding="utf-8"))
    ]
    mappings = [
        TermMapping.model_validate(m)
        for m in json.loads(map_path.read_text(encoding="utf-8"))
    ]
    plan = read_plan(xlsx_path)

    header = f"{'covenant id':<10} | {'value_raw':>10} | {'value_reported':>14} | {'limit':>8} | {'headroom':>10} | {'met':<5} | stress_text"
    print(header)
    print("-" * len(header))
    for contract in contracts:
        for r in evaluate_contract(contract, mappings, plan):
            print(
                f"{r.covenant_id:<10} | {r.value_raw:>10.4f} | {r.value_reported:>14.4f} | "
                f"{r.limit:>8.2f} | {r.headroom:>10.4f} | {str(r.met):<5} | {r.stress_text}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
