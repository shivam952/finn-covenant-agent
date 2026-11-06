"""German number, percentage, and currency (T€) formatting helpers."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP

_NOISE_QUANTIZER = Decimal("1.000000000")


def _strip_float_noise(value: float) -> Decimal:
    """First quantize Decimal(repr(value)) to 9 decimals with ROUND_HALF_EVEN to strip IEEE-754 noise."""
    return Decimal(repr(value)).quantize(_NOISE_QUANTIZER, rounding=ROUND_HALF_EVEN)


def round_kaufmaennisch(value: float, decimals: int) -> float:
    """Round half up (kaufmännisch) after stripping float representation noise at 9 decimal places."""
    cleaned = _strip_float_noise(value)
    quantizer = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    rounded = cleaned.quantize(quantizer, rounding=ROUND_HALF_UP)
    return float(rounded)


def fmt_num(x: float, decimals: int = 2) -> str:
    """Format a number in German notation ('.' thousands separator, ',' decimal separator)."""
    cleaned = _strip_float_noise(x)
    quantizer = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    d = cleaned.quantize(quantizer, rounding=ROUND_HALF_UP)
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
