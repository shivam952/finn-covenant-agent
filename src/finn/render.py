"""Presentation layer for rendering structured stress results, formulas, and check messages."""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

from finn.fmt import fmt_amount, fmt_num, fmt_pct
from finn.models import CheckResult, FormulaParts, FormulaTerm, StressResult

_TEXTS_DIR = Path(__file__).resolve().parent / "texts"


@lru_cache(maxsize=8)
def _load_texts(lang: str) -> dict[str, Any]:
    lang_code = "en" if lang.lower().startswith("en") else "de"
    path = _TEXTS_DIR / f"{lang_code}.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def _fmt_amount_auto(x: float, lang: str) -> str:
    if abs(x - round(x)) < 1e-9:
        return fmt_num(x, decimals=0, lang=lang)
    return fmt_num(x, decimals=2, lang=lang)


def _format_terms_expr(terms: list[FormulaTerm], lang: str) -> str:
    active = [t for t in terms if not (t.is_empty_leaf and abs(t.amount) < 1e-9)]
    if not active:
        return "0"

    parts: list[str] = []
    for idx, t in enumerate(active):
        amt = t.amount
        effective_sign = t.sign if amt >= 0 else -t.sign
        abs_amt_str = _fmt_amount_auto(abs(amt), lang=lang)
        if abs(t.weight - 1.0) > 1e-9:
            w_decimals = 1 if abs(t.weight * 10 - round(t.weight * 10)) < 1e-9 else 2
            term_str = f"{fmt_num(t.weight, decimals=w_decimals, lang=lang)} × {abs_amt_str}"
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


def render_formula(formula: FormulaParts, lang: str = "de", unit: str = "") -> str:
    """Render a structured FormulaParts object into a localized formula string."""
    _ = unit
    templates = _load_texts(lang)["formula"]
    num_expr = _format_terms_expr(formula.numerator_terms, lang=lang)
    den_expr = _format_terms_expr(formula.denominator_terms, lang=lang)
    unit_suffix = " %" if formula.unit == "percent" else ""
    mult_part = " × 100" if formula.unit == "percent" else ""
    lhs = f"{num_expr} / {den_expr}{mult_part}"
    raw_str = f"{fmt_num(formula.value_raw, decimals=4, lang=lang)}{unit_suffix}"

    if formula.rounding_decimals is not None:
        rep_str = (
            f"{fmt_num(formula.value_reported, decimals=formula.rounding_decimals, lang=lang)}"
            f"{unit_suffix}"
        )
        return str(templates["rounded"]).format(lhs=lhs, raw=raw_str, reported=rep_str)
    return str(templates["exact"]).format(lhs=lhs, raw=raw_str)


def render_stress(stress: StressResult, lang: str = "de", unit: str = "") -> str:
    """Render a structured StressResult into a localized sentence."""
    templates = _load_texts(lang)["stress"]
    limit_decimals = (
        stress.rounding_decimals
        if stress.rounding_decimals is not None
        else (1 if stress.unit == "percent" else 2)
    )
    limit_str = (
        fmt_pct(stress.limit, decimals=limit_decimals, lang=lang)
        if stress.unit == "percent"
        else fmt_num(stress.limit, decimals=limit_decimals, lang=lang)
    )

    if stress.kind == "already_breached":
        diff_val = stress.pct if stress.unit == "percent" else stress.amount
        diff_num = abs(diff_val or 0.0)
        diff_str = (
            fmt_pct(diff_num, decimals=2, lang=lang)
            if stress.unit == "percent"
            else fmt_num(diff_num, decimals=2, lang=lang)
        )
        key = "already_breached_ge" if stress.operator == ">=" else "already_breached_le"
        return str(templates[key]).format(limit=limit_str, diff=diff_str)

    if stress.kind == "earnings_decline":
        pct_str = fmt_pct(stress.pct or 0.0, decimals=1, lang=lang)
        amt_str = fmt_amount(stress.amount or 0.0, unit=unit, lang=lang, decimals=0)
        key = "earnings_decline_ge" if stress.operator == ">=" else "earnings_decline_le"
        return str(templates[key]).format(
            driver=stress.driver_quantity,
            pct=pct_str,
            amount=amt_str,
            limit=limit_str,
        )

    if stress.kind == "equity_loss":
        amt_str = fmt_amount(stress.amount or 0.0, unit=unit, lang=lang, decimals=0)
        return str(templates["equity_loss"]).format(amount=amt_str, limit=limit_str)

    amt_str = fmt_amount(stress.amount or 0.0, unit=unit, lang=lang, decimals=0)
    key = "numerator_change_ge" if stress.operator == ">=" else "numerator_change_le"
    return str(templates[key]).format(
        driver=stress.driver_quantity,
        amount=amt_str,
        limit=limit_str,
    )


def render_check(check: CheckResult, lang: str = "de", unit: str = "") -> str:
    """Render a structured CheckResult into a localized message."""
    templates = _load_texts(lang)["checks"]
    if check.message_key == "formula_cache_mismatch":
        eval_val = float(check.params.get("evaluated", check.lhs or 0.0))
        cached_val = float(check.params.get("cached", check.rhs or 0.0))
        cell = str(check.params.get("cell", check.id))
        return str(templates["formula_cache_mismatch"]).format(
            evaluated=fmt_num(eval_val, decimals=2, lang=lang),
            cached=fmt_num(cached_val, decimals=2, lang=lang),
            cell=cell,
        )

    lhs_str = (
        fmt_amount(check.lhs, unit=unit, lang=lang, decimals=0)
        if check.lhs is not None
        else "?"
    )
    rhs_str = (
        fmt_amount(check.rhs, unit=unit, lang=lang, decimals=0)
        if check.rhs is not None
        else "?"
    )
    if check.message_key == "check_skipped":
        reason = str(check.params.get("reason", "no unique item match"))
        return str(templates["check_skipped"]).format(
            id=check.id,
            description=check.description,
            reason=reason,
        )
    if check.message_key == "check_failed":
        return str(templates["check_failed"]).format(
            id=check.id,
            description=check.description,
            lhs=lhs_str,
            rhs=rhs_str,
        )
    return str(templates["check_passed"]).format(
        id=check.id,
        description=check.description,
        lhs=lhs_str,
        rhs=rhs_str,
    )
