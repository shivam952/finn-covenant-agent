"""Locale-aware number tokens in free text, without regular expressions.

Used to check that numbers written by the LLM (summary) or cited by it (covenant limits in
quotes) really occur where they should.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_MINUS_CHARS = "−–-"
_NUMBER_SEPARATORS = ".,/"


def _canonical_number(token: str, lang: str) -> str | None:
    """Turn a localized number token into a canonical, sign-free decimal string."""
    t = token.strip().lstrip(_MINUS_CHARS)
    if not t:
        return None
    if lang.lower().startswith("en"):
        thousands, decimal_sep = ",", "."
    else:
        thousands, decimal_sep = ".", ","
    # A token like "3.37" in German text or "30,7" in English text is still read with the
    # other convention if the primary one does not fit (one separator, not 3 digits after it).
    if thousands in t and decimal_sep not in t:
        head, _, tail = t.rpartition(thousands)
        if len(tail) != 3 or t.count(thousands) > 1 and any(
            len(p) != 3 for p in t.split(thousands)[1:]
        ):
            t = t.replace(thousands, decimal_sep)
    t = t.replace(thousands, "").replace(decimal_sep, ".")
    try:
        d = Decimal(t)
    except InvalidOperation:
        return None
    normalized = d.normalize()
    return format(normalized, "f")


def _scan_numeric_runs(text: str) -> list[str]:
    """Runs of digits joined by '.', ',' or '/' (a separator only counts if a digit follows)."""
    runs: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if not text[i].isdigit():
            i += 1
            continue
        start = i
        while i < n:
            if text[i].isdigit():
                i += 1
            elif text[i] in _NUMBER_SEPARATORS and i + 1 < n and text[i + 1].isdigit():
                i += 1
            else:
                break
        runs.append(text[start:i])
    return runs


def _as_date(run: str) -> str | None:
    """'dd.mm.yyyy' or 'dd/mm/yy' -> 'date:d.m.y'; anything else -> None."""
    sep = "/" if "/" in run else "."
    parts = run.split(sep)
    if len(parts) != 3 or "," in run or any(not p.isdigit() for p in parts):
        return None
    day, month, year = parts
    if not (1 <= len(day) <= 2 and 1 <= len(month) <= 2 and 2 <= len(year) <= 4):
        return None
    return f"date:{int(day)}.{int(month)}.{int(year)}"


def extract_number_tokens(text: str, lang: str) -> list[str]:
    """Canonical number tokens in text (dates first, as single 'date:d.m.y' tokens). Signs are ignored."""
    dates: list[str] = []
    numbers: list[str] = []
    for run in _scan_numeric_runs(text):
        date = _as_date(run)
        if date is not None:
            dates.append(date)
            continue
        if "/" in run:
            parts = run.split("/")
        else:
            parts = [run]
        for part in parts:
            canon = _canonical_number(part, lang)
            if canon is not None:
                numbers.append(canon)
    return dates + numbers


def canonical_value(value: float) -> str:
    """The canonical token for a float, comparable with extract_number_tokens output."""
    return format(Decimal(repr(value)).normalize(), "f")
