"""Excel workbook reader for extracting Plan 2027 items from Wirtschaftsplan_2027.xlsx."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, Field

from finn.fmt import fmt_num
from finn.models import PlanItem


class PlanFormatError(Exception):
    """Raised when the Excel workbook is missing required sheets, columns, or has unsupported formulas."""


class PlanData(BaseModel):
    """Parsed 2027 business plan items, plausibility check warnings, and metadata."""

    items: dict[str, PlanItem]
    checks: list[str] = Field(default_factory=list)
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
        self.warnings: list[str] = []

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
                self.warnings.append(
                    f"Abweichung zwischen berechnetem Formelwert ({fmt_num(total, 2)}) und "
                    f"gespeichertem Cache-Wert ({fmt_num(float(cached), 2)}) in {sheet}!{coord_up}; "
                    f"es wird der berechnete Wert verwendet."
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


def _find_plan_column(ws: Worksheet, sheet_label: str, mode: str) -> tuple[int, int, str]:
    """Scan first 10 rows to locate (header_row, col_index, header_text) for the 2027 plan column."""
    max_scan_row = min(10, ws.max_row)
    for r in range(1, max_scan_row + 1):
        for c in range(1, ws.max_column + 1):
            val = ws.cell(r, c).value
            if not isinstance(val, str):
                continue
            cleaned = val.strip()
            low = cleaned.lower()
            if mode == "guv" and "plan 2027" in low:
                return r, c, cleaned
            if mode == "bilanz" and "2027" in low and "plan" in low:
                return r, c, cleaned

    raise PlanFormatError(
        f"Im Blatt „{sheet_label}“ wurde keine Spalte „Plan 2027“ gefunden."
    )


def _find_finanzierung_headers(ws: Worksheet) -> tuple[int, list[tuple[int, str]]]:
    """Locate the header row and numeric columns (B..G) on the Finanzierung sheet."""
    max_scan_row = min(10, ws.max_row)
    for r in range(1, max_scan_row + 1):
        col_a = ws.cell(r, 1).value
        headers: list[tuple[int, str]] = []
        for c in range(2, min(ws.max_column, 7) + 1):
            h_val = ws.cell(r, c).value
            if isinstance(h_val, str) and h_val.strip():
                headers.append((c, h_val.strip()))
        if (isinstance(col_a, str) and "darlehen" in col_a.lower() and headers) or len(headers) >= 3:
            return r, headers

    raise PlanFormatError(
        "Im Blatt „Finanzierung“ wurde keine gültige Kopfzeile für den Darlehensspiegel 2027 gefunden."
    )


def _is_numeric_or_formula(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    if isinstance(val, str) and val.strip().startswith("="):
        return True
    return False


def _find_item_by_keywords(
    items: dict[str, PlanItem],
    sheet: str,
    include_keywords: list[str],
    exclude_keywords: list[str] | None = None,
) -> PlanItem | None:
    excl = [k.lower() for k in (exclude_keywords or [])]
    incl = [k.lower() for k in include_keywords]
    for item in items.values():
        if item.sheet != sheet:
            continue
        lbl = item.label.lower()
        if all(k in lbl for k in incl) and not any(k in lbl for k in excl):
            return item
    return None


def _run_plausibility_checks(items: dict[str, PlanItem], checks: list[str]) -> None:
    """Run the 4 domain plausibility checks and append German warnings on mismatch or missing rows."""
    # a) Bilanz: Summe Aktiva == Summe Passiva
    aktiva = _find_item_by_keywords(items, "Bilanz", ["summe aktiva"])
    passiva = _find_item_by_keywords(items, "Bilanz", ["summe passiva"])
    if aktiva is None or passiva is None:
        checks.append(
            "Plausibilitätsprüfung (a) übersprungen: „Summe Aktiva“ oder „Summe Passiva“ in der Bilanz nicht gefunden."
        )
    elif abs(aktiva.value - passiva.value) > 1e-2:
        checks.append(
            f"Bilanz-Ungleichgewicht: Summe Aktiva ({fmt_num(aktiva.value, 0)} T€) weicht von "
            f"Summe Passiva ({fmt_num(passiva.value, 0)} T€) ab."
        )

    # b) Finanzierung: "Summe Kreditinstitute – Stand 31.12.2027" == Bilanz "Verbindlichkeiten gegenüber Kreditinstituten"
    fin_ki = _find_item_by_keywords(
        items, "Finanzierung", ["summe kreditinstitute", "stand 31.12.2027"]
    )
    bil_ki = _find_item_by_keywords(
        items, "Bilanz", ["verbindlichkeiten gegenüber kreditinstituten"]
    )
    if fin_ki is None or bil_ki is None:
        checks.append(
            "Plausibilitätsprüfung (b) übersprungen: Abgleichzeilen für Verbindlichkeiten gegenüber Kreditinstituten nicht gefunden."
        )
    elif abs(fin_ki.value - bil_ki.value) > 1e-2:
        checks.append(
            f"Abweichung Kreditinstitute: Darlehensspiegel ({fmt_num(fin_ki.value, 0)} T€) != "
            f"Bilanz ({fmt_num(bil_ki.value, 0)} T€)."
        )

    # c) GuV "Zinsen und ähnliche Aufwendungen" minus "davon … Aufzinsung" == Finanzierung "Summe Darlehen gesamt – Zinsaufwand 2027"
    guv_zins = _find_item_by_keywords(
        items, "GuV", ["zinsen und ähnliche aufwendungen"], ["abgleich"]
    )
    guv_aufz = _find_item_by_keywords(items, "GuV", ["davon", "aufzinsung"])
    fin_zins = _find_item_by_keywords(
        items, "Finanzierung", ["summe darlehen gesamt", "zinsaufwand 2027"]
    )
    if guv_zins is None or guv_aufz is None or fin_zins is None:
        checks.append(
            "Plausibilitätsprüfung (c) übersprungen: Zeilen für den Zinsaufwands-Abgleich zwischen GuV und Finanzierung nicht gefunden."
        )
    elif abs((guv_zins.value - guv_aufz.value) - fin_zins.value) > 1e-2:
        checks.append(
            f"Abweichung Zinsaufwand: GuV Zinsaufwand abzgl. Aufzinsung "
            f"({fmt_num(guv_zins.value - guv_aufz.value, 0)} T€) != "
            f"Darlehensspiegel Zinsaufwand ({fmt_num(fin_zins.value, 0)} T€)."
        )

    # d) EBITDA identity: Jahresüberschuss + Steuern + Zinsaufwand − Zinserträge + Abschreibungen == Betriebsergebnis + Abschreibungen
    jue = _find_item_by_keywords(items, "GuV", ["jahresüberschuss"])
    steuern = _find_item_by_keywords(items, "GuV", ["steuern vom einkommen"])
    zinsertr = _find_item_by_keywords(items, "GuV", ["zinsen und ähnliche erträge"])
    abschr = _find_item_by_keywords(items, "GuV", ["abschreibungen"])
    ebit = _find_item_by_keywords(items, "GuV", ["betriebsergebnis"])
    if any(x is None for x in (jue, steuern, guv_zins, zinsertr, abschr, ebit)):
        checks.append(
            "Plausibilitätsprüfung (d) übersprungen: Nicht alle GuV-Zeilen für die EBITDA-Identität gefunden."
        )
    else:
        assert jue and steuern and guv_zins and zinsertr and abschr and ebit
        lhs = jue.value + steuern.value + guv_zins.value - zinsertr.value + abschr.value
        rhs = ebit.value + abschr.value
        if abs(lhs - rhs) > 1e-2:
            checks.append(
                f"EBITDA-Identität verletzt: Bottom-up ({fmt_num(lhs, 0)} T€) != "
                f"Top-down ({fmt_num(rhs, 0)} T€)."
            )


def read_plan(path: Path) -> PlanData:
    """Read Wirtschaftsplan_2027.xlsx, evaluate formulas, normalise signs, and run plausibility checks."""
    path = Path(path)
    if not path.exists():
        raise PlanFormatError(f"Die Wirtschaftsplan-Datei „{path}“ wurde nicht gefunden.")

    try:
        wb_f = openpyxl.load_workbook(path, data_only=False)
        wb_v = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise PlanFormatError(
            f"Die Wirtschaftsplan-Datei „{path.name}“ konnte nicht als Excel-Arbeitsmappe geöffnet werden: {exc}"
        ) from exc

    required_sheets = ["GuV", "Bilanz", "Finanzierung"]
    lower_to_actual = {name.lower(): name for name in wb_f.sheetnames}

    canonical_map: dict[str, str] = {}
    sheets_f: dict[str, Worksheet] = {}
    sheets_v: dict[str, Worksheet] = {}

    for req in required_sheets:
        actual = lower_to_actual.get(req.lower())
        if actual is None:
            raise PlanFormatError(f"Im Wirtschaftsplan fehlt das Blatt „{req}“.")
        canonical_map[req.lower()] = req
        canonical_map[actual.lower()] = req
        sheets_f[req] = wb_f[actual]
        sheets_v[req] = wb_v[actual]

    evaluator = _WorkbookEvaluator(sheets_f, sheets_v, canonical_map)
    items: dict[str, PlanItem] = {}

    # 1. GuV
    ws_guv = sheets_f["GuV"]
    guv_header_row, guv_col, guv_header = _find_plan_column(ws_guv, "GuV", "guv")
    guv_col_letter = get_column_letter(guv_col)
    for r in range(guv_header_row + 1, ws_guv.max_row + 1):
        label_raw = ws_guv.cell(r, 1).value
        if not isinstance(label_raw, str) or not label_raw.strip():
            continue
        cell_val = ws_guv.cell(r, guv_col).value
        if not _is_numeric_or_formula(cell_val):
            continue
        coord = f"{guv_col_letter}{r}"
        raw_value = evaluator.eval_cell("GuV", coord)
        is_formula = isinstance(cell_val, str) and cell_val.strip().startswith("=")
        is_expense = (not is_formula) and (raw_value < 0)
        norm_value = abs(raw_value) if is_expense else raw_value
        item_id = f"GuV!{coord}"
        items[item_id] = PlanItem(
            id=item_id,
            label=label_raw.strip(),
            sheet="GuV",
            cell=coord,
            raw_value=raw_value,
            value=norm_value,
            is_expense=is_expense,
            is_total=is_formula,
        )

    # 2. Bilanz
    ws_bil = sheets_f["Bilanz"]
    bil_header_row, bil_col, bil_header = _find_plan_column(ws_bil, "Bilanz", "bilanz")
    bil_col_letter = get_column_letter(bil_col)
    for r in range(bil_header_row + 1, ws_bil.max_row + 1):
        label_raw = ws_bil.cell(r, 1).value
        if not isinstance(label_raw, str) or not label_raw.strip():
            continue
        cell_val = ws_bil.cell(r, bil_col).value
        if not _is_numeric_or_formula(cell_val):
            continue
        coord = f"{bil_col_letter}{r}"
        raw_value = evaluator.eval_cell("Bilanz", coord)
        is_formula = isinstance(cell_val, str) and cell_val.strip().startswith("=")
        item_id = f"Bilanz!{coord}"
        items[item_id] = PlanItem(
            id=item_id,
            label=label_raw.strip(),
            sheet="Bilanz",
            cell=coord,
            raw_value=raw_value,
            value=raw_value,
            is_expense=False,
            is_total=is_formula,
        )

    # 3. Finanzierung
    ws_fin = sheets_f["Finanzierung"]
    fin_header_row, fin_headers = _find_finanzierung_headers(ws_fin)
    for r in range(fin_header_row + 1, ws_fin.max_row + 1):
        label_raw = ws_fin.cell(r, 1).value
        if not isinstance(label_raw, str) or not label_raw.strip():
            continue
        row_label = label_raw.strip()
        for c, col_header in fin_headers:
            cell_val = ws_fin.cell(r, c).value
            if not _is_numeric_or_formula(cell_val):
                continue
            coord = f"{get_column_letter(c)}{r}"
            raw_value = evaluator.eval_cell("Finanzierung", coord)
            is_formula = isinstance(cell_val, str) and cell_val.strip().startswith("=")
            item_id = f"Finanzierung!{coord}"
            items[item_id] = PlanItem(
                id=item_id,
                label=f"{row_label} – {col_header}",
                sheet="Finanzierung",
                cell=coord,
                raw_value=raw_value,
                value=raw_value,
                is_expense=False,
                is_total=is_formula,
            )

    # Populate transitive depends_on for all formula PlanItems
    known_ids = set(items.keys())
    for item_id, item in list(items.items()):
        if item.is_total:
            deps = evaluator.transitive_dependencies(item.sheet, item.cell, known_ids)
            items[item_id] = item.model_copy(update={"depends_on": deps})

    checks = list(evaluator.warnings)
    _run_plausibility_checks(items, checks)

    meta: dict[str, Any] = {
        "file": path.name,
        "guv_plan_column": f"{guv_col_letter} ({guv_header})",
        "bilanz_plan_column": f"{bil_col_letter} ({bil_header})",
        "finanzierung_columns": [f"{get_column_letter(c)} ({h})" for c, h in fin_headers],
        "sheet_headers": {
            "GuV": f"GuV – Spalte {guv_header} (Spalte {guv_col_letter})",
            "Bilanz": f"Bilanz – Spalte {bil_header} (Spalte {bil_col_letter})",
            "Finanzierung": "Finanzierung – Darlehensspiegel 2027 (Spalten "
            + ", ".join(f"{get_column_letter(c)}: {h}" for c, h in fin_headers)
            + ")",
        },
        "item_count": len(items),
    }

    return PlanData(items=items, checks=checks, meta=meta)


def plan_item_catalog(plan: PlanData, include_values: bool = False) -> str:
    """Format PlanItems grouped by sheet. Default omits numeric values so LLM maps by meaning only."""
    sheet_headers: dict[str, str] = plan.meta.get("sheet_headers", {})
    by_sheet: dict[str, list[PlanItem]] = {}
    for item in plan.items.values():
        by_sheet.setdefault(item.sheet, []).append(item)

    blocks: list[str] = []
    for sheet, sheet_items in by_sheet.items():
        header = sheet_headers.get(sheet, f"{sheet} – Spalte Plan 2027")
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
