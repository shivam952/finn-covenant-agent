"""Tests for the Excel plan reader and formula evaluator."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from finn.plan.reader import PlanFormatError, plan_item_catalog, read_plan

SAMPLE_XLSX = (
    Path(__file__).resolve().parents[1] / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
)


def test_reads_sample_values_and_plausibility_checks() -> None:
    """Reads expected cells from the sample workbook with normalized expense signs and zero warnings."""
    plan = read_plan(SAMPLE_XLSX)

    # GuV!D17 raw -3200 -> value 3200, is_expense True
    guv_d17 = plan.items["GuV!D17"]
    assert guv_d17.raw_value == -3200.0
    assert guv_d17.value == 3200.0
    assert guv_d17.is_expense is True

    # GuV!D18 (Jahresüberschuss) = 10700 (formula cell)
    guv_d18 = plan.items["GuV!D18"]
    assert guv_d18.value == 10700.0
    assert guv_d18.is_expense is False

    # Bilanz and Finanzierung cells
    assert plan.items["Bilanz!D15"].value == 313200.0
    assert plan.items["Bilanz!D22"].value == 96100.0
    assert plan.items["Finanzierung!E8"].value == 3000.0
    assert plan.items["Finanzierung!G15"].value == 4930.0

    # All 4 plausibility checks pass
    assert plan.checks == []

    catalog = plan_item_catalog(plan)
    assert "GuV!D18 | Jahresüberschuss | 10.700" in catalog
    assert "Bilanz!D15 | Summe Aktiva (Bilanzsumme) | 313.200" in catalog


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
    assert plan.checks == []


def test_missing_sheet_raises_plan_format_error(tmp_path: Path) -> None:
    """Deleting a required sheet raises PlanFormatError naming the missing sheet."""
    copy_path = tmp_path / "missing_bilanz.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    del wb["Bilanz"]
    wb.save(copy_path)

    with pytest.raises(PlanFormatError, match="Bilanz"):
        read_plan(copy_path)


def test_renamed_plan_2027_header_raises_plan_format_error(tmp_path: Path) -> None:
    """Renaming the 'Plan 2027' column header raises PlanFormatError."""
    copy_path = tmp_path / "renamed_header.xlsx"
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb["GuV"]["D3"].value = "Budget 2028"
    wb.save(copy_path)

    with pytest.raises(PlanFormatError, match="Plan 2027"):
        read_plan(copy_path)
