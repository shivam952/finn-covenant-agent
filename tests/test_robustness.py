"""Regression tests for the failure modes seen in the first live run with a weaker model.

1. A coverage ratio whose numerator also contains the denominator (plus quantities no covenant
   uses) -> one corrective re-extraction, and a refusal if it stays wrong.
2. Excluded items listed with weight 0 -> dropped without a warning.
3. An exclusion modelled as a carve-out while the chosen plan cells already leave it out ->
   not subtracted a second time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from finn.calc import evaluate_contract
from finn.config import Settings
from finn.contracts.extract import (
    ExtractionStructureError,
    extract_covenants,
    normalize_contract,
    structural_problems,
)
from finn.llm import LLMClient, TransportReturn
from finn.mapping import apply_carve_outs
from finn.models import Component, ContractCovenants, Quantity, TermMapping
from finn.plan.reader import read_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"
SAMPLE_XLSX = REPO_ROOT / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"
GOLDEN_MAPPING = REPO_ROOT / "tests" / "fixtures" / "golden_mapping.json"
EXPECTED = json.loads((REPO_ROOT / "tests" / "expected_2027.json").read_text(encoding="utf-8"))


def _golden() -> dict[str, ContractCovenants]:
    return {c["contract_file"]: ContractCovenants.model_validate(c)
            for c in json.loads(GOLDEN.read_text(encoding="utf-8"))}


def _golden_mappings() -> list[TermMapping]:
    return [TermMapping.model_validate(m) for m in json.loads(GOLDEN_MAPPING.read_text(encoding="utf-8"))]


def _strip(cc: ContractCovenants) -> dict[str, Any]:
    data = cc.model_dump(mode="json")

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k in ("verified", "verify_note", "original_page"):
                o.pop(k, None)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return data


def _coverage_contract() -> ContractCovenants:
    """The golden contract whose covenant has a single debt-service-like denominator."""
    for cc in _golden().values():
        if cc.has_covenants and len(cc.covenants) == 1:
            q = {x.name: x for x in cc.quantities}
            cov = cc.covenants[0]
            if any(c.ref for c in q[cov.numerator].components):
                return cc
    raise AssertionError("no coverage-ratio contract in fixture")


def _broken_coverage(cc: ContractCovenants) -> ContractCovenants:
    """Numerator also adds the denominator; plus an unused quantity and a weight-0 line."""
    bad = cc.model_copy(deep=True)
    cov = bad.covenants[0]
    num = next(q for q in bad.quantities if q.name == cov.numerator)
    num.components.append(Component(name=cov.denominator, sign=1, ref=cov.denominator,
                                    definition="(falsch modelliert)"))
    bad.quantities.append(Quantity(name="Unbenutzt", components=[
        Component(name="Dokument", sign=1, definition="nur ein Dokument")], source=num.source))
    return bad


def test_golden_fixtures_are_structurally_valid_and_unchanged_by_normalisation() -> None:
    for cc in _golden().values():
        assert structural_problems(cc) == [], cc.contract_file
        normalized, changes = normalize_contract(cc)
        assert changes == [] and normalized == cc, cc.contract_file


def test_normalisation_drops_weight_zero_lines_and_unused_quantities() -> None:
    cc = _coverage_contract().model_copy(deep=True)
    cov = cc.covenants[0]
    num = next(q for q in cc.quantities if q.name == cov.numerator)
    num.components.append(Component(name="Ausgeschlossen", sign=1, weight=0.0, definition="ausdrücklich nicht"))
    cc.quantities.append(Quantity(name="Unbenutzt", components=[
        Component(name="x", sign=1, definition="x")], source=num.source))
    normalized, changes = normalize_contract(cc)
    assert len(changes) == 2
    assert all(c.weight != 0.0 for q in normalized.quantities for c in q.components)
    assert "Unbenutzt" not in {q.name for q in normalized.quantities}


def _run_extraction(answers: list[str], tmp_path: Path, file_name: str) -> tuple[ContractCovenants, dict, list[str]]:
    calls: list[str] = []

    def transport(model: str, messages: list[dict[str, str]], schema: dict[str, Any]) -> TransportReturn:
        calls.append(messages[-1]["content"])
        return answers.pop(0), 10, 10

    llm = LLMClient(Settings(llm_api_key="x", llm_model="m"), transport=transport)
    cc, meta = extract_covenants(CONTRACTS_DIR / file_name, llm, tmp_path)
    return cc, meta, calls


def test_numerator_containing_denominator_triggers_correction(tmp_path: Path) -> None:
    good = _coverage_contract()
    bad = _broken_coverage(good)
    assert structural_problems(bad)
    cc, meta, calls = _run_extraction([json.dumps(_strip(bad)), json.dumps(_strip(good))], tmp_path,
                                      good.contract_file)
    assert len(calls) == 2 and "contains the denominator" in calls[1]
    assert structural_problems(cc) == []
    assert meta["correction_retry"]["structural_after"] == []


def test_numerator_containing_denominator_twice_is_refused(tmp_path: Path) -> None:
    good = _coverage_contract()
    bad = json.dumps(_strip(_broken_coverage(good)))
    with pytest.raises(ExtractionStructureError):
        _run_extraction([bad, bad], tmp_path, good.contract_file)


def _carve_out_variant(include_carved_cell_in_base: bool) -> tuple[ContractCovenants, list[TermMapping]]:
    """A debt definition modelled as 'loans, minus the carved-out subordinated loan'.

    The base is mapped either to the bank-loan line only (already without the subordinated loan)
    or to bank loans plus the subordinated-loan line.
    """
    plan = read_plan(SAMPLE_XLSX)
    golden = _golden()
    cc = next(c for c in golden.values() if any(q.kind == "debt" or "netto" in q.name.casefold()
                                                for q in c.quantities) and len(c.covenants) == 2)
    cc = cc.model_copy(deep=True)
    mappings = [m for m in _golden_mappings() if m.contract_file == cc.contract_file]
    debt_q = next(q for q in cc.quantities if any(c.sign == -1 for c in q.components)
                  and q.name not in {"EBITDA"} and len(q.components) == 2)
    base_comp = next(c for c in debt_q.components if c.sign == 1)
    base_map = next(m for m in mappings if m.quantity == debt_q.name and m.component == base_comp.name)
    sub_cell = next(i for i, it in plan.items.items()
                    if it.sheet == plan.items[base_map.item_ids[0]].sheet and i not in base_map.item_ids
                    and it.value == EXPECTED["base_values"]["gesellschafterdarlehen"])
    carve = Component(name="Nachrangige Gesellschafterdarlehen", sign=-1, carve_out=True,
                      definition="ausgenommen nachrangige Gesellschafterdarlehen")
    debt_q.components.insert(1, carve)
    base_ids = base_map.item_ids + ([sub_cell] if include_carved_cell_in_base else [])
    new_maps = []
    for m in mappings:
        if m is base_map:
            new_maps.append(m.model_copy(update={"item_ids": base_ids}))
        else:
            new_maps.append(m)
    new_maps.append(TermMapping(contract_file=cc.contract_file, quantity=debt_q.name, component=carve.name,
                                item_ids=[sub_cell], rationale="Gesellschafterdarlehen"))
    return cc, new_maps


@pytest.mark.parametrize("include_carved_cell_in_base", [False, True])
def test_carve_out_is_subtracted_only_from_cells_that_contain_it(include_carved_cell_in_base: bool) -> None:
    plan = read_plan(SAMPLE_XLSX)
    cc, mappings = _carve_out_variant(include_carved_cell_in_base)
    adjusted, notes = apply_carve_outs(cc, mappings, plan)
    assert (len(notes) == 0) is include_carved_cell_in_base
    exp = EXPECTED["contracts"][cc.contract_file]["covenants"]
    for res in evaluate_contract(cc, adjusted, plan):
        assert res.numerator_value == pytest.approx(exp[res.covenant_name]["numerator"])
        assert res.value_reported == pytest.approx(exp[res.covenant_name]["value_reported"])
