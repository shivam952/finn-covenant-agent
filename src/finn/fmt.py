"""German number, percentage, and currency (T€) formatting helpers."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def round_kaufmaennisch(value: float, decimals: int) -> float:
    """Round half up (kaufmännisch) using Decimal(str(value))."""
    quantizer = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    rounded = Decimal(str(value)).quantize(quantizer, rounding=ROUND_HALF_UP)
    return float(rounded)


def fmt_num(x: float, decimals: int = 2) -> str:
    """Format a number in German notation ('.' thousands separator, ',' decimal separator)."""
    quantizer = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    d = Decimal(str(x)).quantize(quantizer, rounding=ROUND_HALF_UP)
    if d == Decimal(0):
        d = abs(d)
    sign = "-" if d < 0 else ""
    d_abs = abs(d)
    formatted = f"{d_abs:,.{decimals}f}"
    if decimals > 0:
        int_part, frac_part = formatted.split(".")
        return f"{sign}{int_part.replace(',', '.')},{frac_part}"
    return f"{sign}{formatted.replace(',', '.')}"


def fmt_teur(x: float, decimals: int = 0) -> str:
    """Format an amount in T€ using German number notation."""
    return f"{fmt_num(x, decimals)} T€"


def fmt_pct(x: float, decimals: int = 1) -> str:
    """Format a percentage value using German number notation."""
    return f"{fmt_num(x, decimals)} %"
