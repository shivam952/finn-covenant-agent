"""Tests for LLM term mapping, validation rules, double-counting detection, and structure-based caching."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from finn.config import Settings
from finn.llm import LLMClient, TransportReturn
from finn.mapping import MappingError, MappingItem, MappingResponse, map_terms
from finn.models import ContractCovenants, TermMapping
from finn.plan.reader import read_plan

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_XLSX = REPO_ROOT / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"
GOLDEN_MAPPING_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_mapping.json"


def _load_fixtures() -> tuple[dict[str, ContractCovenants], list[TermMapping]]:
    contracts = {
        c["contract_file"]: ContractCovenants.model_validate(c)
        for c in json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
    }
    mappings = [
        TermMapping.model_validate(m)
        for m in json.loads(GOLDEN_MAPPING_PATH.read_text(encoding="utf-8"))
    ]
    return contracts, mappings


def _golden_response_for(
    contract_file: str, mappings: list[TermMapping]
) -> MappingResponse:
    items = [
        MappingItem(
            quantity=m.quantity,
            component=m.component,
            item_ids=list(m.item_ids),
            rationale=m.rationale,
        )
        for m in mappings
        if m.contract_file == contract_file
    ]
    return MappingResponse(mappings=items)


def test_map_terms_golden_caches_and_omits_values_from_prompt(tmp_path: Path) -> None:
    """FakeLLM returning golden mappings succeeds, omits plan numbers from prompt, and caches on second call."""
    contracts, golden_mappings = _load_fixtures()
    plan = read_plan(SAMPLE_XLSX)
    v2 = contracts["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    v2_resp = _golden_response_for(v2.contract_file, golden_mappings)

    user_messages: list[str] = []

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        user_messages.append(messages[-1]["content"])
        return v2_resp.model_dump_json(), 600, 150

    settings = Settings(openrouter_api_key="test", openrouter_model="model-map")
    llm = LLMClient(settings, transport=fake_transport)
    cache_dir = tmp_path / "cache"

    # First call -> LLM called, cache written
    mapped_1, meta_1 = map_terms(v2, plan, llm, cache_dir)
    assert meta_1["cache_hit"] is False
    assert len(user_messages) == 1
    assert mapped_1 == [
        m for m in golden_mappings if m.contract_file == v2.contract_file
    ]

    # User message must NOT contain any plan values
    sent_prompt = user_messages[0]
    assert "313200" not in sent_prompt
    assert "313.200" not in sent_prompt
    assert "40200" not in sent_prompt
    assert "40.200" not in sent_prompt

    # Second call -> cache hit, no extra LLM call
    mapped_2, meta_2 = map_terms(v2, plan, llm, cache_dir)
    assert meta_2["cache_hit"] is True
    assert len(user_messages) == 1
    assert mapped_2 == mapped_1


def test_unknown_item_id_triggers_retry_with_id_in_error(tmp_path: Path) -> None:
    """An unknown item_id triggers a retry mentioning the invalid id, and succeeds when fixed."""
    contracts, golden_mappings = _load_fixtures()
    plan = read_plan(SAMPLE_XLSX)
    v2 = contracts["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    good_resp = _golden_response_for(v2.contract_file, golden_mappings)

    bad_items = [
        item.model_copy(
            update={"item_ids": ["Bilanz!Z99"] if idx == 0 else item.item_ids}
        )
        for idx, item in enumerate(good_resp.mappings)
    ]
    bad_resp = MappingResponse(mappings=bad_items)

    prompts_seen: list[str] = []
    call_count = 0

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        nonlocal call_count
        call_count += 1
        prompts_seen.append(messages[-1]["content"])
        if call_count == 1:
            return bad_resp.model_dump_json(), 500, 120
        return good_resp.model_dump_json(), 550, 120

    settings = Settings(openrouter_api_key="test", openrouter_model="model-map")
    llm = LLMClient(settings, transport=fake_transport)

    mapped, meta = map_terms(v2, plan, llm, tmp_path / "cache")
    assert call_count == 2
    assert meta["attempts"] == 2
    assert "Bilanz!Z99" in prompts_seen[1]
    assert len(mapped) == 3


def test_double_counting_total_and_part_is_flagged_and_missing_component_raises(
    tmp_path: Path,
) -> None:
    """Selecting Bilanz!D22 together with Bilanz!D18 is rejected as double counting; persistent missing component raises MappingError."""
    contracts, golden_mappings = _load_fixtures()
    plan = read_plan(SAMPLE_XLSX)
    v2 = contracts["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    good_resp = _golden_response_for(v2.contract_file, golden_mappings)

    # 1. Double counting: Bilanz!D22 (A. Eigenkapital total) + Bilanz!D18 (Gezeichnetes Kapital part)
    dc_items = [
        item.model_copy(
            update={
                "item_ids": ["Bilanz!D22", "Bilanz!D18"]
                if item.component == "Eigenkapital"
                else item.item_ids
            }
        )
        for item in good_resp.mappings
    ]
    dc_resp = MappingResponse(mappings=dc_items)

    retry_messages: list[str] = []
    step = 0

    def fake_transport_dc(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        nonlocal step
        step += 1
        retry_messages.append(messages[-1]["content"])
        if step == 1:
            return dc_resp.model_dump_json(), 400, 100
        return good_resp.model_dump_json(), 450, 100

    settings = Settings(openrouter_api_key="test", openrouter_model="model-map")
    llm_dc = LLMClient(settings, transport=fake_transport_dc)
    map_terms(v2, plan, llm_dc, tmp_path / "cache_dc")
    assert step == 2
    assert "double counting" in retry_messages[1].lower() or "doppelzählung" in retry_messages[1].lower()
    assert "Bilanz!D22" in retry_messages[1]
    assert "Bilanz!D18" in retry_messages[1]

    # 2. Missing component on both attempts -> raises MappingError
    incomplete_resp = MappingResponse(mappings=good_resp.mappings[:-1])

    def fake_transport_incomplete(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        return incomplete_resp.model_dump_json(), 300, 80

    llm_inc = LLMClient(settings, transport=fake_transport_incomplete)
    with pytest.raises(MappingError, match="Bilanzsumme"):
        map_terms(v2, plan, llm_inc, tmp_path / "cache_inc")


def test_changed_plan_value_hits_cache_while_renamed_label_misses_cache(
    tmp_path: Path,
) -> None:
    """Changing a number in the Excel keeps the same mapping cache key; renaming a row label causes a cache miss."""
    contracts, golden_mappings = _load_fixtures()
    v2 = contracts["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    v2_resp = _golden_response_for(v2.contract_file, golden_mappings)

    calls = 0

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        nonlocal calls
        calls += 1
        return v2_resp.model_dump_json(), 400, 100

    settings = Settings(openrouter_api_key="test", openrouter_model="model-map")
    llm = LLMClient(settings, transport=fake_transport)
    cache_dir = tmp_path / "cache_struct"

    # Initial run on sample plan
    plan_orig = read_plan(SAMPLE_XLSX)
    _, meta1 = map_terms(v2, plan_orig, llm, cache_dir)
    assert meta1["cache_hit"] is False
    assert calls == 1

    # Modify a numeric value in Bilanz!D18 (same row labels/structure) -> cache hit!
    wb_val = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb_val["Bilanz"]["D18"].value = 99999
    xlsx_val_changed = tmp_path / "val_changed.xlsx"
    wb_val.save(xlsx_val_changed)

    plan_val_changed = read_plan(xlsx_val_changed)
    _, meta2 = map_terms(v2, plan_val_changed, llm, cache_dir)
    assert meta2["cache_hit"] is True
    assert calls == 1

    # Rename a row label in Bilanz!A18 -> cache miss!
    wb_lbl = openpyxl.load_workbook(SAMPLE_XLSX, data_only=False)
    wb_lbl["Bilanz"]["A18"].value = "Stammkapital (umbenannt)"
    xlsx_lbl_changed = tmp_path / "lbl_changed.xlsx"
    wb_lbl.save(xlsx_lbl_changed)

    plan_lbl_changed = read_plan(xlsx_lbl_changed)
    _, meta3 = map_terms(v2, plan_lbl_changed, llm, cache_dir)
    assert meta3["cache_hit"] is False
    assert calls == 2

