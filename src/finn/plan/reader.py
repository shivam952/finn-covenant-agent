"""Excel workbook reader for extracting business plan items and evaluating formulas."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, Field

from finn.fmt import fmt_num
from finn.models import CheckResult, PlanItem
from finn.plan.checks import evaluate_checks
from finn.plan.layout import PlanLayoutConfig, RoleConfig, load_plan_layout


class PlanFormatError(Exception):
    """Raised when the Excel workbook is missing required sheets, columns, or has unsupported formulas."""


class PlanData(BaseModel):
    """Parsed business plan items, structured check results, and metadata."""

    items: dict[str, PlanItem]
    checks: list[CheckResult] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


_CELL_REF_RE = re.compile(
    r"^(?:(?:'(?P<squote>[^']+)'|(?P<sheet>[A-Za-z0-9_ÄÖÜäöüß]+))!)?"
    r"\$?(?P<col>[A-Za-z]{1,3})\$?(?P<row>\d+)$"
)
_SUM_RE = re.compile(
    r"^SUM\(\s*"
    r"(?:(?:'(?P<squote>[^']+)'|(?P<sheet>[A-Za-z0-9_ÄÖÜäöüß]+))!)?"
    r"\$?(?P<col1>[A-Za-z]{1,3})\$?(?P<row1>\d+)"
    r"\s*:\s*"
    r"\$?(?P<col2>[A-Za-z]{1,3})\$?(?P<row2>\d+)"
    r"\s*\)$",
    re.IGNORECASE,
)
_NUMBER_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _split_top_level_add_sub(expr: str) -> list[tuple[int, str]] | None:
    """Split an expression into (sign, term_str) along top-level '+' and '-' operators."""
    expr = expr.strip()
    if not expr:
        return None

    terms: list[tuple[int, str]] = []
    depth = 0
    in_quote = False
    current_sign = 1
    start = 0
    i = 0

    if expr[0] in "+-":
        current_sign = -1 if expr[0] == "-" else 1
        i = 1
        start = 1

    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            in_quote = not in_quote
        elif not in_quote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return None
            elif depth == 0 and ch in "+-":
                part = expr[start:i].strip()
                if not part:
                    return None
                terms.append((current_sign, part))
                current_sign = -1 if ch == "-" else 1
                start = i + 1
        i += 1

    if depth != 0 or in_quote:
        return None

    last_part = expr[start:].strip()
    if not last_part:
        return None
    terms.append((current_sign, last_part))
    return terms


class _WorkbookEvaluator:
    """Minimal recursive Excel formula evaluator for SUM ranges, +/- chains, and cross-sheet refs."""

    def __init__(
        self,
        sheets_f: dict[str, Worksheet],
        sheets_v: dict[str, Worksheet],
        canonical_map: dict[str, str],
    ) -> None:
        self.sheets_f = sheets_f
        self.sheets_v = sheets_v
        self.canonical_map = canonical_map
        self.memo: dict[tuple[str, str], float] = {}
        self.direct_deps: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.checked_cache: set[tuple[str, str]] = set()
        self.cache_checks: list[CheckResult] = []

    def _resolve_sheet(self, name: str, origin_cell: str) -> str:
        key = name.strip().lower()
        if key not in self.canonical_map:
            raise PlanFormatError(
                f"Unbekanntes Tabellenblatt „{name}“ in Formel der Zelle {origin_cell}."
            )
        return self.canonical_map[key]

    def eval_cell(
        self,
        sheet: str,
        coord: str,
        visiting: frozenset[tuple[str, str]] = frozenset(),
    ) -> float:
        coord_up = coord.upper()
        key = (sheet, coord_up)
        if key in self.memo:
            return self.memo[key]
        if key in visiting:
            raise PlanFormatError(
                f"Zyklische Formelreferenz erkannt bei Zelle {sheet}!{coord_up}."
            )

        ws_f = self.sheets_f[sheet]
        ws_v = self.sheets_v[sheet]
        raw = ws_f[coord_up].value
        cached = ws_v[coord_up].value

        if raw is None:
            self.direct_deps[key] = []
            return 0.0
        if isinstance(raw, bool):
            raise PlanFormatError(f"Ungültiger Wahrheitswert in Zelle {sheet}!{coord_up}.")
        if isinstance(raw, (int, float)):
            val = float(raw)
            self.memo[key] = val
            self.direct_deps[key] = []
            return val
        if not isinstance(raw, str):
            raise PlanFormatError(
                f"Nicht unterstützter Zelltyp in {sheet}!{coord_up}: {type(raw).__name__}."
            )

        text = raw.strip()
        if not text.startswith("="):
            raise PlanFormatError(
                f"Nicht-numerischer Text in {sheet}!{coord_up}: „{text}“."
            )

        expr = text[1:].strip()
        next_visiting = visiting | {key}
        terms = _split_top_level_add_sub(expr)
        if not terms:
            raise PlanFormatError(
                f"Nicht unterstützte Formel in {sheet}!{coord_up}: {text}"
            )

        total = 0.0
        deps: list[tuple[str, str]] = []
        for sign, term_str in terms:
            t_val, t_deps = self._eval_term(sheet, coord_up, term_str, next_visiting)
            total += sign * t_val
            deps.extend(t_deps)

        self.direct_deps[key] = deps

        if (
            key not in self.checked_cache
            and cached is not None
            and isinstance(cached, (int, float))
            and not isinstance(cached, bool)
        ):
            self.checked_cache.add(key)
            if abs(total - float(cached)) > 0.5:
                cell_ref = f"{sheet}!{coord_up}"
                self.cache_checks.append(
                    CheckResult(
                        id=f"cache_{sheet}_{coord_up}",
                        description=f"Formel-Cache {cell_ref}",
                        passed=False,
                        skipped=False,
                        lhs=total,
                        rhs=float(cached),
                        message_key="formula_cache_mismatch",
                        params={
                            "cell": cell_ref,
                            "evaluated": total,
                            "cached": float(cached),
                        },
                    )
                )

        self.memo[key] = total
        return total

    def _eval_term(
        self,
        sheet: str,
        coord: str,
        term: str,
        visiting: frozenset[tuple[str, str]],
    ) -> tuple[float, list[tuple[str, str]]]:
        origin = f"{sheet}!{coord}"
        m_sum = _SUM_RE.match(term)
        if m_sum:
            target_sheet_raw = m_sum.group("squote") or m_sum.group("sheet")
            target_sheet = (
                self._resolve_sheet(target_sheet_raw, origin)
                if target_sheet_raw
                else sheet
            )
            col1 = column_index_from_string(m_sum.group("col1").upper())
            row1 = int(m_sum.group("row1"))
            col2 = column_index_from_string(m_sum.group("col2").upper())
            row2 = int(m_sum.group("row2"))
            if col1 > col2 or row1 > row2:
                raise PlanFormatError(
                    f"Ungültiger Bereich in SUM-Formel in {origin}: ={term}"
                )
            acc = 0.0
            deps: list[tuple[str, str]] = []
            for r in range(row1, row2 + 1):
                for c in range(col1, col2 + 1):
                    c_coord = f"{get_column_letter(c)}{r}"
                    cell_val = self.sheets_f[target_sheet][c_coord].value
                    if cell_val is None:
                        continue
                    acc += self.eval_cell(target_sheet, c_coord, visiting)
                    deps.append((target_sheet, c_coord))
            return acc, deps

        m_ref = _CELL_REF_RE.match(term)
        if m_ref:
            target_sheet_raw = m_ref.group("squote") or m_ref.group("sheet")
            target_sheet = (
                self._resolve_sheet(target_sheet_raw, origin)
                if target_sheet_raw
                else sheet
            )
            target_coord = f"{m_ref.group('col').upper()}{int(m_ref.group('row'))}"
            val = self.eval_cell(target_sheet, target_coord, visiting)
            return val, [(target_sheet, target_coord)]

        if _NUMBER_RE.match(term):
            return float(term), []

        raise PlanFormatError(f"Nicht unterstützte Formel in {origin}: ={term}")

    def transitive_dependencies(
        self,
        sheet: str,
        coord: str,
        known_item_ids: set[str],
    ) -> list[str]:
        """Return all transitive PlanItem IDs that (sheet, coord) depends on."""
        start_key = (sheet, coord.upper())
        visited: set[tuple[str, str]] = set()
        ordered: list[str] = []

        def _walk(curr: tuple[str, str]) -> None:
            for dep in self.direct_deps.get(curr, []):
                if dep in visited:
                    continue
                visited.add(dep)
                dep_id = f"{dep[0]}!{dep[1]}"
                if dep_id in known_item_ids:
                    ordered.append(dep_id)
                _walk(dep)

        _walk(start_key)
        return ordered


def _is_numeric_or_formula(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str) and val.strip().startswith("="):
        return True
    return False


def _resolve_role_sheet(
    role_name: str,
    role_cfg: RoleConfig,
    wb_f: openpyxl.Workbook,
) -> str:
    """Resolve the workbook sheet name for a configured role via aliases or content keywords."""
    lower_to_actual = {name.lower(): name for name in wb_f.sheetnames}
    for alias in role_cfg.sheet_aliases:
        actual = lower_to_actual.get(alias.lower())
        if actual is not None:
            return actual

    # Fallback: score each sheet by how many content_keywords appear in column A
    keywords = [kw.lower() for kw in role_cfg.content_keywords]
    scores: dict[str, int] = {}
    for sheet_name in wb_f.sheetnames:
        ws = wb_f[sheet_name]
        col_a_texts: list[str] = []
        for r in range(1, ws.max_row + 1):
            val = ws.cell(r, 1).value
            if isinstance(val, str) and val.strip():
                col_a_texts.append(val.strip().lower())
        joined = "\n".join(col_a_texts)
        score = sum(1 for kw in keywords if kw in joined)
        if score > 0:
            scores[sheet_name] = score

    if not scores:
        found = ", ".join(wb_f.sheetnames)
        raise PlanFormatError(
            f"Für die Rolle „{role_name}“ konnte kein passendes Tabellenblatt ermittelt werden "
            f"(vorhandene Blätter: {found})."
        )

    best_score = max(scores.values())
    winners = [s for s, sc in scores.items() if sc == best_score]
    if len(winners) > 1:
        found = ", ".join(wb_f.sheetnames)
        raise PlanFormatError(
            f"Mehrdeutige Blatt-Zuordnung für Rolle „{role_name}“ zwischen {winners} "
            f"(vorhandene Blätter: {found})."
        )
    return winners[0]


def _scan_sheet_headers(ws: Worksheet, max_rows: int = 10) -> list[tuple[int, int, str]]:
    """Return (row, col, text) for all non-empty string cells in the first max_rows rows."""
    out: list[tuple[int, int, str]] = []
    limit = min(max_rows, ws.max_row)
    for r in range(1, limit + 1):
        for c in range(1, ws.max_column + 1):
            val = ws.cell(r, c).value
            if isinstance(val, str) and val.strip():
                out.append((r, c, val.strip()))
    return out


def _detect_years_on_sheet(ws: Worksheet, patterns: list[str]) -> set[int]:
    """Extract all 4-digit years matching any plan-column pattern in the top rows of ws."""
    compiled = [
        re.compile(pat.replace("{year}", r"(?P<year>\d{4})"), re.IGNORECASE)
        for pat in patterns
    ]
    years: set[int] = set()
    for _, _, text in _scan_sheet_headers(ws):
        for rx in compiled:
            for m in rx.finditer(text):
                years.add(int(m.group("year")))
    return years


def _find_plan_column_for_year(
    ws: Worksheet,
    sheet_name: str,
    plan_year: int,
    patterns: list[str],
) -> tuple[int, int, str]:
    """Locate (header_row, col_index, header_text) matching plan_year on ws."""
    year_str = re.escape(str(plan_year))
    compiled = [
        re.compile(pat.replace("{year}", year_str), re.IGNORECASE)
        for pat in patterns
    ]
    headers = _scan_sheet_headers(ws)
    for r, c, text in headers:
        if any(rx.search(text) for rx in compiled):
            return r, c, text

    found_headers = [t for _, col, t in headers if col >= 2]
    raise PlanFormatError(
        f"Im Blatt „{sheet_name}“ wurde keine Planspalte für das Jahr {plan_year} gefunden "
        f"(gefundene Spaltenköpfe: {', '.join(found_headers) if found_headers else 'keine'})."
    )


def _find_all_numeric_columns(
    ws: Worksheet,
    sheet_name: str,
) -> tuple[int, list[tuple[int, str]]]:
    """Locate the header row and all columns (c >= 2) with headers and numeric/formula cells below."""
    max_scan_row = min(10, ws.max_row)
    best_row: int | None = None
    best_headers: list[tuple[int, str]] = []

    for r in range(1, max_scan_row + 1):
        candidate: list[tuple[int, str]] = []
        for c in range(2, ws.max_column + 1):
            h_val = ws.cell(r, c).value
            if not isinstance(h_val, str) or not h_val.strip():
                continue
            has_numeric_below = any(
                _is_numeric_or_formula(ws.cell(data_r, c).value)
                for data_r in range(r + 1, ws.max_row + 1)
            )
            if has_numeric_below:
                candidate.append((c, h_val.strip()))
        if len(candidate) > len(best_headers):
            best_row = r
            best_headers = candidate

    if best_row is None or not best_headers:
        raise PlanFormatError(
            f"Im Blatt „{sheet_name}“ wurde keine gültige Kopfzeile mit numerischen Datenspalten gefunden."
        )
    return best_row, best_headers


def _detect_amount_unit(
    wb_f: openpyxl.Workbook,
    layout: PlanLayoutConfig,
) -> str:
    """Return configured amount_unit, or detect from cover sheet if layout.amount_unit is empty."""
    if layout.amount_unit:
        return layout.amount_unit
    if layout.cover_unit_pattern:
        rx = re.compile(layout.cover_unit_pattern, re.IGNORECASE)
        for sheet_name in wb_f.sheetnames:
            ws = wb_f[sheet_name]
            for r in range(1, min(25, ws.max_row) + 1):
                for c in range(1, min(10, ws.max_column) + 1):
                    val = ws.cell(r, c).value
                    if isinstance(val, str):
                        m = rx.search(val)
                        if m:
                            return m.group(1).strip()
    return ""


def read_plan(
    path: Path,
    plan_year: int | None = None,
    layout_path: Path | None = None,
) -> PlanData:
    """Read a business plan workbook, evaluate formulas, normalise signs, and run checks."""
    path = Path(path)
    if not path.exists():
        raise PlanFormatError(f"Die Wirtschaftsplan-Datei „{path}“ wurde nicht gefunden.")

    layout = load_plan_layout(layout_path)

    try:
        wb_f = openpyxl.load_workbook(path, data_only=False)
        wb_v = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise PlanFormatError(
            f"Die Wirtschaftsplan-Datei „{path.name}“ konnte nicht als Excel-Arbeitsmappe geöffnet werden: {exc}"
        ) from exc

    canonical_map: dict[str, str] = {}
    sheets_f: dict[str, Worksheet] = {}
    sheets_v: dict[str, Worksheet] = {}
    for sname in wb_f.sheetnames:
        canonical_map[sname.lower()] = sname
        sheets_f[sname] = wb_f[sname]
        sheets_v[sname] = wb_v[sname]

    role_to_sheet: dict[str, str] = {}
    for role_name, role_cfg in layout.roles.items():
        actual_sheet = _resolve_role_sheet(role_name, role_cfg, wb_f)
        role_to_sheet[role_name] = actual_sheet
        for alias in role_cfg.sheet_aliases:
            canonical_map.setdefault(alias.lower(), actual_sheet)

    # Determine plan_year across all roles with column_mode == "plan_year"
    plan_year_roles = [
        (r_name, role_to_sheet[r_name])
        for r_name, r_cfg in layout.roles.items()
        if r_cfg.column_mode == "plan_year"
    ]
    if plan_year is None:
        year_sets: list[set[int]] = []
        found_headers_by_sheet: dict[str, list[str]] = {}
        for _, sname in plan_year_roles:
            ws = sheets_f[sname]
            year_sets.append(_detect_years_on_sheet(ws, layout.plan_column.patterns))
            found_headers_by_sheet[sname] = [
                t for _, col, t in _scan_sheet_headers(ws) if col >= 2
            ]
        common_years = set.intersection(*year_sets) if year_sets else set()
        if not common_years:
            details = "; ".join(
                f"{s}: {', '.join(h) if h else 'keine'}"
                for s, h in found_headers_by_sheet.items()
            )
            raise PlanFormatError(
                f"Es konnte kein gemeinsames Planjahr erkannt werden (gefundene Spaltenköpfe: {details})."
            )
        target_year = max(common_years)
    else:
        target_year = plan_year

    evaluator = _WorkbookEvaluator(sheets_f, sheets_v, canonical_map)
    items: dict[str, PlanItem] = {}
    sheet_headers: dict[str, str] = {}
    role_column_meta: dict[str, Any] = {}

    for role_name, role_cfg in layout.roles.items():
        sheet_name = role_to_sheet[role_name]
        ws = sheets_f[sheet_name]

        if role_cfg.column_mode == "plan_year":
            header_row, col_idx, header_text = _find_plan_column_for_year(
                ws, sheet_name, target_year, layout.plan_column.patterns
            )
            col_letter = get_column_letter(col_idx)
            sheet_headers[sheet_name] = (
                f"{sheet_name} – Spalte {header_text} (Spalte {col_letter})"
            )
            role_column_meta[role_name] = f"{col_letter} ({header_text})"

            for r in range(header_row + 1, ws.max_row + 1):
                label_raw = ws.cell(r, 1).value
                if not isinstance(label_raw, str) or not label_raw.strip():
                    continue
                cell_val = ws.cell(r, col_idx).value
                if not _is_numeric_or_formula(cell_val):
                    continue
                coord = f"{col_letter}{r}"
                raw_value = evaluator.eval_cell(sheet_name, coord)
                is_formula = isinstance(cell_val, str) and cell_val.strip().startswith("=")
                is_expense = (
                    role_cfg.normalise_expenses
                    and (not is_formula)
                    and (raw_value < 0)
                )
                norm_value = abs(raw_value) if is_expense else raw_value
                item_id = f"{sheet_name}!{coord}"
                items[item_id] = PlanItem(
                    id=item_id,
                    label=label_raw.strip(),
                    sheet=sheet_name,
                    role=role_name,
                    cell=coord,
                    raw_value=raw_value,
                    value=norm_value,
                    is_expense=is_expense,
                    is_total=is_formula,
                )
        else:
            header_row, col_headers = _find_all_numeric_columns(ws, sheet_name)
            sheet_headers[sheet_name] = (
                f"{sheet_name} – (Spalten "
                + ", ".join(f"{get_column_letter(c)}: {h}" for c, h in col_headers)
                + ")"
            )
            role_column_meta[role_name] = [
                f"{get_column_letter(c)} ({h})" for c, h in col_headers
            ]

            for r in range(header_row + 1, ws.max_row + 1):
                label_raw = ws.cell(r, 1).value
                if not isinstance(label_raw, str) or not label_raw.strip():
                    continue
                row_label = label_raw.strip()
                for c, col_header in col_headers:
                    cell_val = ws.cell(r, c).value
                    if not _is_numeric_or_formula(cell_val):
                        continue
                    coord = f"{get_column_letter(c)}{r}"
                    raw_value = evaluator.eval_cell(sheet_name, coord)
                    is_formula = isinstance(cell_val, str) and cell_val.strip().startswith("=")
                    is_expense = (
                        role_cfg.normalise_expenses
                        and (not is_formula)
                        and (raw_value < 0)
                    )
                    norm_value = abs(raw_value) if is_expense else raw_value
                    item_id = f"{sheet_name}!{coord}"
                    items[item_id] = PlanItem(
                        id=item_id,
                        label=f"{row_label} – {col_header}",
                        sheet=sheet_name,
                        role=role_name,
                        cell=coord,
                        raw_value=raw_value,
                        value=norm_value,
                        is_expense=is_expense,
                        is_total=is_formula,
                    )

    known_ids = set(items.keys())
    for item_id, item in list(items.items()):
        if item.is_total:
            deps = evaluator.transitive_dependencies(item.sheet, item.cell, known_ids)
            items[item_id] = item.model_copy(update={"depends_on": deps})

    checks: list[CheckResult] = list(evaluator.cache_checks)
    checks.extend(evaluate_checks(items, layout.checks, target_year))

    amount_unit = _detect_amount_unit(wb_f, layout)

    meta: dict[str, Any] = {
        "file": path.name,
        "plan_year": target_year,
        "test_date": f"31.12.{target_year}",
        "amount_unit": amount_unit,
        "role_sheets": role_to_sheet,
        "role_columns": role_column_meta,
        "sheet_headers": sheet_headers,
        "item_count": len(items),
    }

    return PlanData(items=items, checks=checks, meta=meta)


def plan_item_catalog(plan: PlanData, include_values: bool = False) -> str:
    """Format PlanItems grouped by sheet. Default omits numeric values so LLM maps by meaning only."""
    sheet_headers: dict[str, str] = plan.meta.get("sheet_headers", {})
    plan_year = plan.meta.get("plan_year", "")
    by_sheet: dict[str, list[PlanItem]] = {}
    for item in plan.items.values():
        by_sheet.setdefault(item.sheet, []).append(item)

    blocks: list[str] = []
    for sheet, sheet_items in by_sheet.items():
        header = sheet_headers.get(sheet, f"{sheet} – Plan {plan_year}".strip())
        lines: list[str] = [f"=== {header} ==="]
        for item in sheet_items:
            parts = [item.id, item.sheet, item.label]
            if include_values:
                val_str = fmt_num(
                    item.value, 0 if float(item.value).is_integer() else 2
                )
                parts.append(val_str)
            if item.is_total:
                parts.append("[Summe]")
            lines.append(" | ".join(parts))
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
