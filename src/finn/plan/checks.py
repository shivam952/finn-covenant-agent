"""Generic plausibility check evaluator driven by layout configuration."""

from __future__ import annotations

from finn.models import CheckResult, PlanItem
from finn.plan.layout import CheckConfig, CheckTermConfig


def _match_term(
    items: dict[str, PlanItem],
    term: CheckTermConfig,
    plan_year: int,
) -> list[PlanItem]:
    """Find all PlanItems matching a single CheckTermConfig after substituting {year}."""
    year_str = str(plan_year)
    label_all = [kw.replace("{year}", year_str).lower() for kw in term.label_all]
    label_none = [kw.replace("{year}", year_str).lower() for kw in term.label_none]
    column_all = [kw.replace("{year}", year_str).lower() for kw in term.column_all]

    matches: list[PlanItem] = []
    for item in items.values():
        if item.role != term.role:
            continue

        full_lbl = item.label.lower()
        if " – " in full_lbl:
            row_part, col_part = full_lbl.split(" – ", 1)
        else:
            row_part, col_part = full_lbl, full_lbl

        if not all(kw in row_part or kw in full_lbl for kw in label_all):
            continue
        if any(kw in full_lbl for kw in label_none):
            continue
        if column_all and not all(kw in col_part or kw in full_lbl for kw in column_all):
            continue

        matches.append(item)

    return matches


def evaluate_checks(
    items: dict[str, PlanItem],
    checks_cfg: list[CheckConfig],
    plan_year: int,
) -> list[CheckResult]:
    """Evaluate all configured plausibility checks against parsed PlanItems."""
    results: list[CheckResult] = []

    for chk in checks_cfg:
        skipped_reason: str | None = None
        lhs_val = 0.0
        rhs_val = 0.0

        for side_name, terms in (("LHS", chk.lhs), ("RHS", chk.rhs)):
            side_sum = 0.0
            for term in terms:
                matched = _match_term(items, term, plan_year)
                if len(matched) == 0:
                    kw_desc = ", ".join(term.label_all + term.column_all)
                    skipped_reason = (
                        f"Keine Position für Rolle '{term.role}' mit Merkmalen [{kw_desc}] gefunden"
                    )
                    break
                if len(matched) > 1:
                    ids = ", ".join(m.id for m in matched)
                    skipped_reason = (
                        f"Mehrdeutige Zuordnung ({len(matched)} Treffer: {ids}) in Rolle '{term.role}'"
                    )
                    break
                side_sum += term.sign * matched[0].value

            if skipped_reason is not None:
                break
            if side_name == "LHS":
                lhs_val = side_sum
            else:
                rhs_val = side_sum

        if skipped_reason is not None:
            results.append(
                CheckResult(
                    id=chk.id,
                    description=chk.description,
                    passed=False,
                    skipped=True,
                    lhs=None,
                    rhs=None,
                    message_key="check_skipped",
                    params={
                        "id": chk.id,
                        "description": chk.description,
                        "reason": skipped_reason,
                    },
                )
            )
            continue

        passed = abs(lhs_val - rhs_val) <= chk.tolerance
        results.append(
            CheckResult(
                id=chk.id,
                description=chk.description,
                passed=passed,
                skipped=False,
                lhs=lhs_val,
                rhs=rhs_val,
                message_key="check_passed" if passed else "check_failed",
                params={
                    "id": chk.id,
                    "description": chk.description,
                    "lhs": lhs_val,
                    "rhs": rhs_val,
                },
            )
        )

    return results
