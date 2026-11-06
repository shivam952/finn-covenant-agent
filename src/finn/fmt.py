"""Locale-aware number, percentage, and unit amount formatting helpers."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, ROUND_HALF_UP

_NOISE_QUANTIZER = Decimal("1.000000000")


def _strip_float_noise(value: float) -> Decimal:
    """First quantize Decimal(repr(value)) to 9 decimals with ROUND_HALF_EVEN to strip float noise."""
    return Decimal(repr(value)).quantize(_NOISE_QUANTIZER, rounding=ROUND_HALF_EVEN)


def round_kaufmaennisch(value: float, decimals: int) -> float:
    """Round half up (commercial rounding) after stripping float representation noise at 9 decimal places."""
    cleaned = _strip_float_noise(value)
    quantizer = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    rounded = cleaned.quantize(quantizer, rounding=ROUND_HALF_UP)
    return float(rounded)


def fmt_num(x: float, decimals: int = 2, lang: str = "de") -> str:
    """Format a number in locale notation (de: '.' thousands and ',' decimal; en: ',' thousands and '.' decimal)."""
    cleaned = _strip_float_noise(x)
    quantizer = Decimal("1") if decimals == 0 else Decimal(f"1.{'0' * decimals}")
    d = cleaned.quantize(quantizer, rounding=ROUND_HALF_UP)
    if d == Decimal(0):
        d = abs(d)
    sign = "-" if d < 0 else ""
    d_abs = abs(d)
    formatted = f"{d_abs:,.{decimals}f}"
    if lang.lower().startswith("en"):
        return f"{sign}{formatted}"
    if decimals > 0:
        int_part, frac_part = formatted.split(".")
        return f"{sign}{int_part.replace(',', '.')},{frac_part}"
    return f"{sign}{formatted.replace(',', '.')}"


def fmt_amount(x: float, unit: str, lang: str = "de", decimals: int = 0) -> str:
    """Format an amount with the given unit and locale."""
    num_str = fmt_num(x, decimals=decimals, lang=lang)
    return f"{num_str} {unit}".strip() if unit else num_str


def fmt_pct(x: float, decimals: int = 1, lang: str = "de") -> str:
    """Format a percentage value using locale number notation."""
    return f"{fmt_num(x, decimals=decimals, lang=lang)} %"
