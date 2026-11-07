"""Excel workbook reader for extracting business plan items and evaluating formulas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import openpyxl
from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import Token
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import coordinate_from_string, range_boundaries
from openpyxl.worksheet.worksheet import Worksheet
from pydantic import BaseModel, Field

from finn.fmt import fmt_num
from finn.models import CheckResult, PlanItem
from finn.plan.checks import evaluate_checks
from finn.notices import notice, render_notice
from finn.plan.layout import PlanLayoutConfig, RoleConfig, load_plan_layout


class PlanFormatError(Exception):
    """The workbook cannot be used. Carries a notice key + params; the wording is in texts/*.toml."""

    def __init__(self, key: str, **params: Any) -> None:
        self.key = key
        self.params = params
        super().__init__(render_notice(notice(key, **params), "de"))

    def render(self, lang: str) -> str:
        return render_notice(notice(self.key, **self.params), lang)


class PlanData(BaseModel):
    """Parsed business plan items, structured check results, and metadata."""

    items: dict[str, PlanItem]
    checks: list[CheckResult] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


_SUPPORTED_FUNCTIONS = frozenset({"SUM"})


def _split_ref(ref: str) -> tuple[str | None, str]:
    """Split 'Sheet!A1:B2' / "'My Sheet'!A1" / 'A1' into (sheet or None, area without $)."""
    sheet_part, sep, area = ref.rpartition("!")
    sheet = None
    if sep:
        sheet = sheet_part
        if sheet.startswith("'") and sheet.endswith("'"):
            sheet = sheet[1:-1].replace("''", "'")
    return sheet, area.replace("$", "")


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
            raise PlanFormatError("plan_unsupported_cell", cell=origin_cell, detail=f"{name}!…")
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
            raise PlanFormatError("plan_unsupported_cell", cell=f"{sheet}!{coord_up}", detail="↻")

        ws_f = self.sheets_f[sheet]
        ws_v = self.sheets_v[sheet]
        raw = ws_f[coord_up].value
        cached = ws_v[coord_up].value

        if raw is None:
            self.direct_deps[key] = []
            return 0.0
        if isinstance(raw, bool):
            raise PlanFormatError("plan_unsupported_cell", cell=f"{sheet}!{coord_up}", detail=str(raw))
        if isinstance(raw, (int, float)):
            val = float(raw)
            self.memo[key] = val
            self.direct_deps[key] = []
            return val
        if not isinstance(raw, str):
            raise PlanFormatError("plan_unsupported_cell", cell=f"{sheet}!{coord_up}", detail=type(raw).__name__)

        text = raw.strip()
        if not text.startswith("="):
            raise PlanFormatError("plan_unsupported_cell", cell=f"{sheet}!{coord_up}", detail=text)

        next_visiting = visiting | {key}
        tokens: list[Token] = []
        try:
            tokens = [
                t for t in Tokenizer(text).items if t.type != Token.WSPACE
            ]
            parser = _FormulaParser(self, sheet, coord_up, text, tokens, next_visiting)
            total = parser.parse()
            deps = parser.deps
        except PlanFormatError:
            # Unsupported function (e.g. ROUND) or syntax: use the value Excel itself saved, if
            # there is one, and say so. Without a saved value the cell cannot be trusted.
            if not isinstance(cached, (int, float)) or isinstance(cached, bool):
                raise
            total = float(cached)
            deps = self._referenced_cells(sheet, coord_up, tokens)
            self.cache_checks.append(
                CheckResult(
                    id=f"formula_cached_{sheet}_{coord_up}",
                    description=f"{sheet}!{coord_up}",
                    passed=False,  # shown as a note in the reply; not blocking
                    skipped=False,
                    message_key="formula_unsupported_cached",
                    params={"cell": f"{sheet}!{coord_up}", "formula": text, "cached": total},
                )
            )
            self.direct_deps[key] = deps
            self.memo[key] = total
            return total
        except Exception as exc:  # openpyxl raises a plain error on malformed formulas
            raise PlanFormatError("plan_unsupported_cell", cell=f"{sheet}!{coord_up}", detail=text) from exc

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

    def _referenced_cells(
        self, sheet: str, coord: str, tokens: list[Token]
    ) -> list[tuple[str, str]]:
        """Cells a formula refers to (without evaluating them), for dependency tracking."""
        cells: list[tuple[str, str]] = []
        for tok in tokens:
            if tok.type != Token.OPERAND or tok.subtype != Token.RANGE:
                continue
            ref_sheet, area = _split_ref(tok.value)
            try:
                target = self._resolve_sheet(ref_sheet, f"{sheet}!{coord}") if ref_sheet else sheet
                min_col, min_row, max_col, max_row = range_boundaries(area)
            except (PlanFormatError, ValueError, TypeError):
                continue
            if None in (min_col, min_row, max_col, max_row):
                continue
            for r in range(int(min_row), int(max_row) + 1):
                for c in range(int(min_col), int(max_col) + 1):
                    cells.append((target, f"{get_column_letter(c)}{r}"))
        return cells

    def eval_ref(
        self,
        sheet: str,
        origin: str,
        ref: str,
        visiting: frozenset[tuple[str, str]],
    ) -> tuple[float, list[tuple[str, str]]]:
        """Value of a cell or the sum of an area; also returns the cells it depends on."""
        ref_sheet, area = _split_ref(ref)
        target_sheet = self._resolve_sheet(ref_sheet, origin) if ref_sheet else sheet
        try:
            min_col, min_row, max_col, max_row = range_boundaries(area)
        except (ValueError, TypeError) as exc:
            raise PlanFormatError("plan_unsupported_cell", cell=origin, detail=ref) from exc
        if None in (min_col, min_row, max_col, max_row):
            raise PlanFormatError("plan_unsupported_cell", cell=origin, detail=ref)
        acc = 0.0
        deps: list[tuple[str, str]] = []
        for r in range(int(min_row), int(max_row) + 1):
            for c in range(int(min_col), int(max_col) + 1):
                c_coord = f"{get_column_letter(c)}{r}"
                if self.sheets_f[target_sheet][c_coord].value is None:
                    continue
                acc += self.eval_cell(target_sheet, c_coord, visiting)
                deps.append((target_sheet, c_coord))
        return acc, deps

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


class _FormulaParser:
    """Recursive-descent evaluator over openpyxl's formula tokens.

    Grammar: expr := term (('+'|'-') term)* ; term := factor (('*'|'/') factor)* ;
    factor := ('+'|'-') factor | NUMBER | RANGE | '(' expr ')' | SUM '(' expr (',' expr)* ')'.
    Anything else raises PlanFormatError naming the cell, so an unsupported formula is never
    silently mis-evaluated.
    """

    def __init__(
        self,
        evaluator: _WorkbookEvaluator,
        sheet: str,
        coord: str,
        text: str,
        tokens: list[Token],
        visiting: frozenset[tuple[str, str]],
    ) -> None:
        self.ev = evaluator
        self.sheet = sheet
        self.origin = f"{sheet}!{coord}"
        self.text = text
        self.tokens = tokens
        self.pos = 0
        self.visiting = visiting
        self.deps: list[tuple[str, str]] = []

    def _fail(self) -> PlanFormatError:
        return PlanFormatError("plan_unsupported_cell", cell=self.origin, detail=self.text)

    def _peek(self) -> Token | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> Token:
        tok = self._peek()
        if tok is None:
            raise self._fail()
        self.pos += 1
        return tok

    def parse(self) -> float:
        value = self._expr()
        if self._peek() is not None:
            raise self._fail()
        return value

    def _expr(self) -> float:
        value = self._term()
        while (tok := self._peek()) is not None and tok.type == Token.OP_IN and tok.value in "+-":
            self.pos += 1
            rhs = self._term()
            value = value + rhs if tok.value == "+" else value - rhs
        return value

    def _term(self) -> float:
        value = self._factor()
        while (tok := self._peek()) is not None and tok.type == Token.OP_IN and tok.value in "*/":
            self.pos += 1
            rhs = self._factor()
            if tok.value == "*":
                value *= rhs
            else:
                if rhs == 0:
                    raise PlanFormatError("plan_unsupported_cell", cell=self.origin, detail=self.text)
                value /= rhs
        return value

    def _factor(self) -> float:
        tok = self._next()
        if tok.type == Token.OP_PRE and tok.value in "+-":
            inner = self._factor()
            return -inner if tok.value == "-" else inner
        if tok.type == Token.OPERAND and tok.subtype == Token.NUMBER:
            return float(tok.value)
        if tok.type == Token.OPERAND and tok.subtype == Token.RANGE:
            value, deps = self.ev.eval_ref(self.sheet, self.origin, tok.value, self.visiting)
            self.deps.extend(deps)
            return value
        if tok.type == Token.PAREN and tok.subtype == Token.OPEN:
            value = self._expr()
            close = self._next()
            if close.type != Token.PAREN or close.subtype != Token.CLOSE:
                raise self._fail()
            return value
        if tok.type == Token.FUNC and tok.subtype == Token.OPEN:
            name = tok.value[:-1].upper()
            if name not in _SUPPORTED_FUNCTIONS:
                raise self._fail()
            total = self._expr()
            while True:
                nxt = self._next()
                if nxt.type == Token.SEP and nxt.value == ",":
                    total += self._expr()
                    continue
                if nxt.type == Token.FUNC and nxt.subtype == Token.CLOSE:
                    return total
                raise self._fail()
        raise self._fail()


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
        raise PlanFormatError("plan_role_sheet_missing", role=role_name, sheets=found)

    best_score = max(scores.values())
    winners = [s for s, sc in scores.items() if sc == best_score]
    if len(winners) > 1:
        found = ", ".join(wb_f.sheetnames)
        raise PlanFormatError("plan_role_sheet_ambiguous", role=role_name, candidates=winners)
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


def _header_words(text: str) -> list[str]:
    """Lower-case words of a header: every non-alphanumeric character separates words."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in text.casefold())
    return cleaned.split()


def _header_years(words: list[str]) -> set[int]:
    return {int(w) for w in words if len(w) == 4 and w.isdigit()}


def _is_plan_header(words: list[str], keywords: list[str]) -> bool:
    kws = [k.casefold() for k in keywords]
    return any(w.startswith(k) for w in words for k in kws)


def _detect_years_on_sheet(ws: Worksheet, keywords: list[str]) -> set[int]:
    """All years that appear in a plan-column header (year + plan keyword) in the top rows of ws."""
    years: set[int] = set()
    for _, _, text in _scan_sheet_headers(ws):
        words = _header_words(text)
        if _is_plan_header(words, keywords):
            years |= _header_years(words)
    return years


def _find_plan_column_for_year(
    ws: Worksheet,
    sheet_name: str,
    plan_year: int,
    keywords: list[str],
) -> tuple[int, int, str]:
    """Locate (header_row, col_index, header_text) of the plan column for plan_year on ws."""
    headers = _scan_sheet_headers(ws)
    for r, c, text in headers:
        words = _header_words(text)
        if plan_year in _header_years(words) and _is_plan_header(words, keywords):
            return r, c, text

    found_headers = [t for _, col, t in headers if col >= 2]
    raise PlanFormatError(
        "plan_year_column_missing", sheet=sheet_name, year=plan_year,
        headers=", ".join(found_headers) if found_headers else "–",
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
        raise PlanFormatError("plan_no_header_row", sheet=sheet_name)
    return best_row, best_headers


def _detect_amount_unit(
    wb_f: openpyxl.Workbook,
    layout: PlanLayoutConfig,
) -> str:
    """Return configured amount_unit, or detect from cover sheet if layout.amount_unit is empty."""
    if layout.amount_unit:
        return layout.amount_unit
    markers = [m.casefold() for m in layout.cover_unit_markers]
    candidates = sorted(layout.unit_candidates, key=len, reverse=True)
    if markers and candidates:
        for sheet_name in wb_f.sheetnames:
            ws = wb_f[sheet_name]
            for r in range(1, min(25, ws.max_row) + 1):
                for c in range(1, min(10, ws.max_column) + 1):
                    val = ws.cell(r, c).value
                    if not isinstance(val, str):
                        continue
                    low = val.casefold()
                    for marker in markers:
                        idx = low.find(marker)
                        if idx < 0:
                            continue
                        rest = val[idx + len(marker):]
                        hits = [(rest.find(u), u) for u in candidates if rest.find(u) >= 0]
                        if hits:
                            return min(hits)[1]
    return ""


def read_plan(
    path: Path,
    plan_year: int | None = None,
    layout_path: Path | None = None,
) -> PlanData:
    """Read a business plan workbook, evaluate formulas, normalise signs, and run checks."""
    path = Path(path)
    if not path.exists():
        raise PlanFormatError("plan_file_missing", file=path.name)

    layout = load_plan_layout(layout_path)

    try:
        wb_f = openpyxl.load_workbook(path, data_only=False)
        wb_v = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:
        raise PlanFormatError("plan_file_unreadable", file=path.name) from exc

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
            year_sets.append(_detect_years_on_sheet(ws, layout.plan_column.keywords))
            found_headers_by_sheet[sname] = [
                t for _, col, t in _scan_sheet_headers(ws) if col >= 2
            ]
        common_years = set.intersection(*year_sets) if year_sets else set()
        if not common_years:
            details = "; ".join(
                f"{s}: {', '.join(h) if h else 'keine'}"
                for s, h in found_headers_by_sheet.items()
            )
            raise PlanFormatError("plan_no_common_year", headers=details)
        # Several plan years (e.g. a medium-term plan): the first plan year is the one the
        # Wirtschaftsplan is about. The caller passes the year from the order email if one is named.
        target_year = min(common_years)
        available_years = sorted(common_years)
    else:
        target_year = plan_year
        available_years = [plan_year]

    evaluator = _WorkbookEvaluator(sheets_f, sheets_v, canonical_map)
    items: dict[str, PlanItem] = {}
    sheet_headers: dict[str, str] = {}
    role_column_meta: dict[str, Any] = {}

    for role_name, role_cfg in layout.roles.items():
        sheet_name = role_to_sheet[role_name]
        ws = sheets_f[sheet_name]

        if role_cfg.column_mode == "plan_year":
            header_row, col_idx, header_text = _find_plan_column_for_year(
                ws, sheet_name, target_year, layout.plan_column.keywords
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
                # An input line is any cell that does not refer to other cells; a typed
                # "=-4930-170" is an input too. Only subtotals (cells built from other cells)
                # keep their sign, so the rule does not depend on how a number was typed.
                is_formula = bool(evaluator.direct_deps.get((sheet_name, coord.upper())))
                is_expense = False  # decided after all cells are read (structure-based)
                norm_value = raw_value
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
                    is_formula = bool(evaluator.direct_deps.get((sheet_name, coord.upper())))
                    is_expense = False  # decided after all cells are read (structure-based)
                    norm_value = raw_value
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

    # Sign convention: on a sheet that enters expenses as negative numbers, a negative input line
    # is an expense if a subtotal on that sheet adds it up, or if it is a "davon" sub-line.
    # Standalone typed lines (e.g. a result typed over its formula) keep their sign, so an
    # inconsistent sheet is caught by the plausibility checks instead of being "repaired".
    summed: set[tuple[str, str]] = set()
    for (dep_sheet, dep_cell), deps in evaluator.direct_deps.items():
        for d in deps:
            if d[0] == dep_sheet:
                summed.add(d)
    for item_id, item in list(items.items()):
        role_cfg = layout.roles.get(item.role)
        if role_cfg is None or not role_cfg.normalise_expenses or item.is_total or item.raw_value >= 0:
            continue
        label = item.label.casefold()
        is_sub_item = any(label.startswith(p.casefold()) for p in role_cfg.sub_item_prefixes)
        if (item.sheet, item.cell.upper()) in summed or is_sub_item:
            items[item_id] = item.model_copy(update={"is_expense": True, "value": abs(item.raw_value)})

    # A "davon" sub-line is contained in the nearest non-sub line above it in the same column.
    by_column: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for item_id, item in items.items():
        col, row = coordinate_from_string(item.cell.upper())
        by_column.setdefault((item.sheet, col), []).append((row, item_id))
    for rows in by_column.values():
        parent: str | None = None
        for _, item_id in sorted(rows):
            item = items[item_id]
            role_cfg = layout.roles.get(item.role)
            prefixes = role_cfg.sub_item_prefixes if role_cfg is not None else []
            if any(item.label.casefold().startswith(p.casefold()) for p in prefixes):
                if parent is not None:
                    items[item_id] = item.model_copy(update={"part_of": parent})
            else:
                parent = item_id

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
        "available_plan_years": available_years,
        "flow_roles": [name for name, cfg in layout.roles.items() if cfg.measure == "flow"],
        "test_date": layout.test_date_template.format(year=target_year),
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
