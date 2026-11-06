"""Tests for the Excel plan reader, layout configuration, year detection, and formula evaluator."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from finn.calc import evaluate_contract
from finn.models import ContractCovenants, TermMapping
from finn.plan.reader import PlanFormatError, plan_item_catalog, read_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_XLSX = REPO_ROOT / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"
GOLDEN_KINDS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_kinds.json"
GOLDEN_MAPPING_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_mapping.json"


def _load_contracts_and_mappings() -> tuple[list[ContractCovenants], list[TermMapping]]:
    raw_contracts = json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
    kinds_overlay = json.loads(GOLDEN_KINDS_PATH.read_text(encoding="utf-8"))
    for c_dict in raw_contracts:
        c_kinds = kinds_overlay.get(c_dict["contract_file"], {})
        for q_dict in c_dict["quantities"]:
            if q_dict["name"] in c_kinds:
                q_dict["kind"] = c_kinds[q_dict["name"]]
    contracts = [ContractCovenants.model_validate(c) for c in raw_contracts]
    mappings = [
        TermMapping.model_validate(m)
        for m in json.loads(GOLDEN_MAPPING_PATH.read_text(encoding="utf-8"))
    ]
    return contracts, mappings


def test_reads_sample_values_and_plausibility_checks() -> None:
    """Reads expected cells from the sample workbook with normalized expense signs and passing checks."""
    plan = read_plan(SAMPLE_XLSX)

    assert plan.meta["plan_year"] == 2027
    assert plan.meta["test_date"] == "31.12.2027"
    assert plan.meta["amount_unit"] == "T€"

    # GuV!D17 raw -3200 -> value 3200, is_expense True, role income_statement
    guv_d17 = plan.items["GuV!D17"]
    assert guv_d17.raw_value == -3200.0
    assert guv_d17.value == 3200.0
    assert guv_d17.is_expense is True
    assert guv_d17.role == "income_statement"

    # GuV!D18 (Jahresüberschuss) = 10700 (formula cell)
    guv_d18 = plan.items["GuV!D18"]
    assert guv_d18.value == 10700.0
    assert guv_d18.is_expense is False

    # Bilanz and Finanzierung cells
    assert plan.items["Bilanz!D15"].value == 313200.0
    assert plan.items["Bilanz!D15"].role == "balance_sheet"
    assert plan.items["Bilanz!D22"].value == 96100.0
    assert plan.items["Finanzierung!E8"].value == 3000.0
    assert plan.items["Finanzierung!E8"].role == "loan_schedule"
    assert plan.items["Finanzierung!G15"].value == 4930.0

    # All 4 plausibility checks pass and none are skipped
    assert len(plan.checks) == 4
    assert all(c.passed and not c.skipped for c in plan.checks)

    # Formula totals have is_total=True and transitive depends_on
    assert plan.items["Bilanz!D22"].is_total is True
    assert "Bilanz!D18" in plan.items["Bilanz!D22"].depends_on
    assert plan.items["Bilanz!D15"].is_total is True
    assert "Bilanz!D5" in plan.items["Bilanz!D15"].depends_on
    assert plan.items["GuV!D17"].is_total is False
    assert plan.items["GuV!D17"].depends_on == []

    # Default catalog has no values and marks totals with [Summe]
    catalog_no_vals = plan_item_catalog(plan)
    assert "GuV!D18 | GuV | Jahresüberschuss | [Summe]" in catalog_no_vals
    assert "313.200" not in catalog_no_vals

    # Catalog with include_values=True includes values
    catalog_with_vals = plan_item_catalog(plan, include_values=True)
    assert "GuV!D18 | GuV | Jahresüberschuss | 10.700 | [Summe]" in catalog_with_vals
    assert "Bilanz!D15 | Bilanz | Summe Aktiva (Bilanzsumme) | 313.200 | [Summe]" in catalog_with_vals


def test_no_cache_workbook_evaluates_formulas_from_scratch(tmp_path: Path) -> None:
    """When openpyxl loads and saves a workbook, cached formula values are dropped; reader evaluates them."""
    copy_path = tmp_path / "no_cache.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb.save(copy_path)

    # Confirm openpyxl data_only=True now sees None on formula cells
    wb_v = openpyxl.load_workbook(copy_path, data_only=True)
    assert wb_v["Bilanz"]["D15"].value is None
    assert wb_v["GuV"]["D18"].value is None

    plan = read_plan(copy_path)
    assert plan.items["Bilanz!D15"].value == 313200.0
    assert plan.items["GuV!D18"].value == 10700.0
    assert all(c.passed and not c.skipped for c in plan.checks)


def test_missing_sheet_raises_plan_format_error(tmp_path: Path) -> None:
    """Deleting a required sheet raises PlanFormatError naming the missing role and sheets found."""
    copy_path = tmp_path / "missing_bilanz.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    del wb["Bilanz"]
    wb.save(copy_path)

    with pytest.raises(PlanFormatError, match="balance_sheet"):
        read_plan(copy_path)


def test_renamed_plan_header_raises_plan_format_error(tmp_path: Path) -> None:
    """Renaming the 'Plan 2027' column header raises PlanFormatError when no common year or requested year exists."""
    copy_path = tmp_path / "renamed_header.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb["GuV"]["D3"].value = "Budget 2028"
    wb.save(copy_path)

    with pytest.raises(PlanFormatError, match="2027"):
        read_plan(copy_path, plan_year=2027)


def test_renamed_sheets_give_identical_results(tmp_path: Path) -> None:
    """A copy of the workbook with sheets renamed to 'Erfolgsplan', 'Planbilanz', 'Darlehensspiegel' gives identical results."""
    copy_path = tmp_path / "renamed_sheets.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb["GuV"].title = "Erfolgsplan"
    wb["Bilanz"].title = "Planbilanz"
    wb["Finanzierung"].title = "Darlehensspiegel"
    wb.save(copy_path)

    base_plan = read_plan(SAMPLE_XLSX)
    renamed_plan = read_plan(copy_path)

    # Real sheet names are preserved in PlanItem IDs
    assert "Erfolgsplan!D17" in renamed_plan.items
    assert "Planbilanz!D15" in renamed_plan.items
    assert "Darlehensspiegel!E8" in renamed_plan.items
    assert len(renamed_plan.checks) == 4
    assert all(c.passed and not c.skipped for c in renamed_plan.checks)

    contracts, mappings = _load_contracts_and_mappings()
    sheet_rename_map = {
        "GuV!": "Erfolgsplan!",
        "Bilanz!": "Planbilanz!",
        "Finanzierung!": "Darlehensspiegel!",
    }
    renamed_mappings = []
    for m in mappings:
        new_ids = []
        for cid in m.item_ids:
            for old_p, new_p in sheet_rename_map.items():
                if cid.startswith(old_p):
                    cid = new_p + cid[len(old_p) :]
                    break
            new_ids.append(cid)
        renamed_mappings.append(m.model_copy(update={"item_ids": new_ids}))

    for contract in contracts:
        base_res = evaluate_contract(contract, mappings, base_plan)
        ren_res = evaluate_contract(contract, renamed_mappings, renamed_plan)
        assert len(base_res) == len(ren_res)
        for r1, r2 in zip(base_res, ren_res, strict=True):
            assert r1.value_raw == pytest.approx(r2.value_raw, abs=1e-9)
            assert r1.value_reported == pytest.approx(r2.value_reported, abs=1e-9)
            assert r1.headroom == pytest.approx(r2.headroom, abs=1e-9)
            assert r1.stress_value == pytest.approx(r2.stress_value, abs=1e-9)


def test_headers_with_2028_detects_year_2028_and_runs_all_checks(tmp_path: Path) -> None:
    """Replacing every '2027' in headers with '2028' detects plan_year 2028, produces identical results, and runs all checks."""
    copy_path = tmp_path / "plan_2028.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and "2027" in cell.value:
                    cell.value = cell.value.replace("2027", "2028")
    wb.save(copy_path)

    base_plan = read_plan(SAMPLE_XLSX)
    plan_2028 = read_plan(copy_path)

    assert plan_2028.meta["plan_year"] == 2028
    assert plan_2028.meta["test_date"] == "31.12.2028"
    assert len(plan_2028.checks) == 4
    assert all(c.passed and not c.skipped for c in plan_2028.checks)

    contracts, mappings = _load_contracts_and_mappings()
    for contract in contracts:
        r_base = evaluate_contract(contract, mappings, base_plan)
        r_2028 = evaluate_contract(contract, mappings, plan_2028)
        for a, b in zip(r_base, r_2028, strict=True):
            assert a.value_raw == pytest.approx(b.value_raw, abs=1e-9)
            assert a.stress_value == pytest.approx(b.stress_value, abs=1e-9)


def test_extra_numeric_column_on_loan_schedule_is_read(tmp_path: Path) -> None:
    """Adding an extra numeric column H on the loan schedule is read into plan.items (no column cap)."""
    copy_path = tmp_path / "extra_col.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    ws_fin = wb["Finanzierung"]
    ws_fin["H3"].value = "Restlaufzeit (Jahre)"
    ws_fin["H6"].value = 8
    ws_fin["H15"].value = 12
    wb.save(copy_path)

    plan = read_plan(copy_path)
    assert "Finanzierung!H6" in plan.items
    assert plan.items["Finanzierung!H6"].value == 8.0
    assert "Restlaufzeit (Jahre)" in plan.items["Finanzierung!H6"].label
    assert "Finanzierung!H15" in plan.items
    assert plan.items["Finanzierung!H15"].value == 12.0
