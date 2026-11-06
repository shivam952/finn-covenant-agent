"""Tests for the deterministic covenant calculator against the hand-checked answer key."""

from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from finn.calc import CalcError, evaluate_contract
from finn.fmt import round_kaufmaennisch
from finn.models import (
    Component,
    ContractCovenants,
    Covenant,
    Quantity,
    Source,
    TermMapping,
)
from finn.plan.reader import read_plan

TESTS_DIR = Path(__file__).resolve().parent
SAMPLE_XLSX = TESTS_DIR.parent / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
EXPECTED_PATH = TESTS_DIR / "expected_2027.json"
GOLDEN_COVENANTS_PATH = TESTS_DIR / "fixtures" / "golden_covenants.json"
GOLDEN_MAPPING_PATH = TESTS_DIR / "fixtures" / "golden_mapping.json"


def _load_fixtures() -> tuple[list[ContractCovenants], list[TermMapping], dict]:
    contracts = [
        ContractCovenants.model_validate(c)
        for c in json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
    ]
    mappings = [
        TermMapping.model_validate(m)
        for m in json.loads(GOLDEN_MAPPING_PATH.read_text(encoding="utf-8"))
    ]
    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    return contracts, mappings, expected


def test_golden_covenants_and_mappings_match_expected_2027() -> None:
    """Every covenant matches expected_2027.json on numerator, denominator, value_raw, value_reported, headroom, met, stress."""
    contracts, mappings, expected = _load_fixtures()
    plan = read_plan(SAMPLE_XLSX)

    all_results = []
    for contract in contracts:
        exp_contract = expected["contracts"][contract.contract_file]
        assert contract.has_covenants == exp_contract["has_covenants"]
        results = evaluate_contract(contract, mappings, plan)
        all_results.extend(results)

        if not contract.has_covenants:
            assert results == []
            continue

        for res in results:
            exp_cov = exp_contract["covenants"][res.covenant_name]
            assert res.numerator_value == pytest.approx(exp_cov["numerator"], abs=1e-6)
            assert res.denominator_value == pytest.approx(exp_cov["denominator"], abs=1e-6)
            assert res.value_raw == pytest.approx(exp_cov["value_raw"], abs=1e-4)
            assert round(res.value_reported, 4) == pytest.approx(
                exp_cov["value_reported"], abs=1e-6
            )
            assert round(res.headroom, 4) == pytest.approx(
                exp_cov["headroom"], abs=1e-6
            )
            assert res.met is exp_cov["met"]

            expected_stress = exp_cov.get(
                "stress_ebitda_drop_pct", exp_cov.get("stress_equity_loss_teur")
            )
            assert res.stress_value == pytest.approx(expected_stress, abs=0.01)

    assert len(all_results) == 4

    # None of the trap values equals any value_reported
    trap_values = list(expected["trap_values_that_must_NOT_appear"].values())
    for res in all_results:
        for trap in trap_values:
            assert round(res.value_reported, 2) != pytest.approx(trap, abs=1e-6)


def test_headroom_sign_for_both_operators() -> None:
    """Headroom is positive when met and negative when breached for both >= and <= limits."""
    plan = read_plan(SAMPLE_XLSX)
    src = Source(page=1, section="§ 1", quote="Test")
    qty = Quantity(
        name="Q",
        components=[
            Component(name="C", sign=1, weight=1.0, definition="Test")
        ],
        source=src,
    )
    mapping = [
        TermMapping(
            contract_file="test.pdf",
            quantity="Q",
            component="C",
            item_ids=["GuV!D12"],  # value = 300
            rationale="Test",
        )
    ]

    def _make_contract(op: str, limit: float) -> ContractCovenants:
        return ContractCovenants(
            contract_file="test.pdf",
            contract_label="Test",
            has_covenants=True,
            reason_if_none=None,
            reason_source=None,
            quantities=[qty],
            covenants=[
                Covenant(
                    id="T1",
                    name="TestCov",
                    numerator="Q",
                    denominator="Q",
                    operator=op,  # type: ignore[arg-type]
                    limit=limit,
                    unit="ratio",
                    rounding_decimals=2,
                    test_basis="stichtag",
                    source=src,
                    definition_sources=[],
                    plan_basis_note=None,
                )
            ],
        )

    # Ratio is 300 / 300 = 1.00
    r_ge_met = evaluate_contract(_make_contract(">=", 0.80), mapping, plan)[0]
    assert r_ge_met.met is True
    assert r_ge_met.headroom == pytest.approx(0.20)

    r_ge_breach = evaluate_contract(_make_contract(">=", 1.25), mapping, plan)[0]
    assert r_ge_breach.met is False
    assert r_ge_breach.headroom == pytest.approx(-0.25)

    r_le_met = evaluate_contract(_make_contract("<=", 1.50), mapping, plan)[0]
    assert r_le_met.met is True
    assert r_le_met.headroom == pytest.approx(0.50)

    r_le_breach = evaluate_contract(_make_contract("<=", 0.75), mapping, plan)[0]
    assert r_le_breach.met is False
    assert r_le_breach.headroom == pytest.approx(-0.25)


def test_kaufmaennisch_rounding() -> None:
    """Round half up (kaufmännisch) rounds 1.565 -> 1.57, 30.65 -> 30.7, and strips float noise."""
    assert round_kaufmaennisch(1.565, 2) == 1.57
    assert round_kaufmaennisch(30.65, 1) == 30.7
    assert round_kaufmaennisch(30.649999999999999, 1) == 30.7


def test_changed_numbers_in_workbook(tmp_path: Path) -> None:
    """Modifying Sondertilgung, Gesellschafterdarlehen, and Bankverbindlichkeiten updates results as expected."""
    contracts, mappings, _ = _load_fixtures()
    baseline_plan = read_plan(SAMPLE_XLSX)
    baseline_by_id = {
        r.covenant_id: r
        for c in contracts
        for r in evaluate_contract(c, mappings, baseline_plan)
    }

    # 1. Set Finanzierung!E8 (Sondertilgung) to 0 -> no covenant changes
    wb = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb["Finanzierung"]["E8"].value = 0
    path_1 = tmp_path / "plan_mod1.xlsx"
    wb.save(path_1)

    plan_1 = read_plan(path_1)
    mod1_by_id = {
        r.covenant_id: r
        for c in contracts
        for r in evaluate_contract(c, mappings, plan_1)
    }
    for cov_id, base_res in baseline_by_id.items():
        assert mod1_by_id[cov_id].value_raw == pytest.approx(base_res.value_raw, abs=1e-9)

    # 2. Set Bilanz!D29 (Gesellschafterdarlehen) higher by 1000 -> no change in V4-DYNVG
    wb["Bilanz"]["D29"].value = 8000 + 1000
    path_2 = tmp_path / "plan_mod2.xlsx"
    wb.save(path_2)

    plan_2 = read_plan(path_2)
    v4_contract = next(c for c in contracts if c.contract_file == "Vertrag_4_Konsortialkredit.pdf")
    v4_res_2 = {r.covenant_id: r for r in evaluate_contract(v4_contract, mappings, plan_2)}
    assert v4_res_2["V4-DYNVG"].value_raw == pytest.approx(
        baseline_by_id["V4-DYNVG"].value_raw, abs=1e-9
    )

    # 3. Increase Bilanz!D28 by 5000 -> V4-DYNVG value_raw = (153200 - 12600) / 40200
    wb["Bilanz"]["D28"].value = 148200 + 5000
    path_3 = tmp_path / "plan_mod3.xlsx"
    wb.save(path_3)

    plan_3 = read_plan(path_3)
    v4_res_3 = {r.covenant_id: r for r in evaluate_contract(v4_contract, mappings, plan_3)}
    expected_dynvg_raw = (153200.0 - 12600.0) / 40200.0
    assert v4_res_3["V4-DYNVG"].value_raw == pytest.approx(expected_dynvg_raw, abs=1e-9)
    # Bilanz total Passiva is a formula and must have been recomputed (+1000 + 5000 = +6000)
    assert plan_3.items["Bilanz!D34"].value == pytest.approx(313200.0 + 6000.0)
    assert len(plan_3.checks) > 0
    # Plan plausibility warnings belong in RunReport.warnings, not copied into Result.warnings
    assert v4_res_3["V4-DYNVG"].warnings == []


def test_missing_mapping_and_cyclic_ref_raise_calc_error() -> None:
    """Missing TermMapping raises CalcError naming the component; cyclic ref raises CalcError."""
    contracts, mappings, _ = _load_fixtures()
    plan = read_plan(SAMPLE_XLSX)

    v2 = next(c for c in contracts if c.contract_file == "Vertrag_2_Foerderbank_Foerderdarlehen.pdf")
    filtered_mappings = [m for m in mappings if m.component != "Sonderposten"]
    with pytest.raises(CalcError, match="Sonderposten"):
        evaluate_contract(v2, filtered_mappings, plan)

    src = Source(page=1, section="§ 1", quote="Test")
    q_a = Quantity(
        name="A",
        components=[Component(name="RefB", sign=1, weight=1.0, ref="B", definition="Ref B")],
        source=src,
    )
    q_b = Quantity(
        name="B",
        components=[Component(name="RefA", sign=1, weight=1.0, ref="A", definition="Ref A")],
        source=src,
    )
    cyclic_contract = ContractCovenants(
        contract_file="cyclic.pdf",
        contract_label="Cyclic",
        has_covenants=True,
        reason_if_none=None,
        reason_source=None,
        quantities=[q_a, q_b],
        covenants=[
            Covenant(
                id="CYC",
                name="CyclicCov",
                numerator="A",
                denominator="B",
                operator=">=",
                limit=1.0,
                unit="ratio",
                rounding_decimals=2,
                test_basis="stichtag",
                source=src,
                definition_sources=[],
                plan_basis_note=None,
            )
        ],
    )
    with pytest.raises(CalcError, match="Zyklische Referenz"):
        evaluate_contract(cyclic_contract, [], plan)

