"""Tests for LLM covenant extraction, caching, retries, and fallback using FakeLLM (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from finn.config import Settings
from finn.contracts.extract import extract_covenants
from finn.llm import LLMClient, LLMError
from finn.models import ContractCovenants, build_llm_json_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"


def _load_golden() -> dict[str, ContractCovenants]:
    items = json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
    return {item["contract_file"]: ContractCovenants.model_validate(item) for item in items}


def _strip_verified(cc: ContractCovenants) -> dict[str, Any]:
    data = cc.model_dump(mode="json")

    def _clean(obj: Any) -> None:
        if isinstance(obj, dict):
            if "quote" in obj and "page" in obj:
                obj["verified"] = None
                obj["verify_note"] = None
            for v in obj.values():
                _clean(v)
        elif isinstance(obj, list):
            for x in obj:
                _clean(x)

    _clean(data)
    return data


def test_llm_schema_excludes_verified_fields() -> None:
    """The JSON schema sent to the LLM must not include 'verified' or 'verify_note'."""
    schema_dict = build_llm_json_schema(ContractCovenants)
    serialized = json.dumps(schema_dict)
    assert '"verified"' not in serialized
    assert '"verify_note"' not in serialized


def test_extract_caches_and_reuses_without_calling_llm_twice(tmp_path: Path) -> None:
    """First extraction calls LLM and writes cache; second call hits cache; prompt change invalidates cache."""
    golden_map = _load_golden()
    v2_gold = golden_map["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    pdf_path = CONTRACTS_DIR / v2_gold.contract_file
    prompt_file = tmp_path / "extract_covenants.txt"
    prompt_file.write_text("Prompt version A", encoding="utf-8")

    calls: list[str] = []

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> tuple[str, int, int]:
        calls.append(model)
        return v2_gold.model_dump_json(), 1000, 250

    settings = Settings(openrouter_api_key="test", openrouter_model="model-primary")
    llm = LLMClient(settings, transport=fake_transport)

    # First call -> LLM called, cache written
    cc1, meta1 = extract_covenants(
        pdf_path, llm, tmp_path / "cache", prompt_path=prompt_file
    )
    assert len(calls) == 1
    assert meta1["cache_hit"] is False
    assert _strip_verified(cc1) == _strip_verified(v2_gold)
    assert all(q.source.verified is True for q in cc1.quantities)
    assert Path(meta1["cache_file"]).exists()

    # Second call -> cache hit, FakeLLM not called
    cc2, meta2 = extract_covenants(
        pdf_path, llm, tmp_path / "cache", prompt_path=prompt_file
    )
    assert len(calls) == 1
    assert meta2["cache_hit"] is True
    assert cc2 == cc1

    # Changing prompt file content -> cache miss
    prompt_file.write_text("Prompt version B (modified)", encoding="utf-8")
    _cc3, meta3 = extract_covenants(
        pdf_path, llm, tmp_path / "cache", prompt_path=prompt_file
    )
    assert len(calls) == 2
    assert meta3["cache_hit"] is False


def test_invalid_json_then_valid_succeeds_on_second_attempt(tmp_path: Path) -> None:
    """When first response is invalid JSON and second is valid, complete_json succeeds with attempts=2."""
    golden_map = _load_golden()
    v3_gold = golden_map["Vertrag_3_Landesbank_Investitionsdarlehen.pdf"]
    pdf_path = CONTRACTS_DIR / v3_gold.contract_file

    responses = ["{not valid json", v3_gold.model_dump_json()]
    idx = 0

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> tuple[str, int, int]:
        nonlocal idx
        out = responses[idx]
        idx += 1
        return out, 500, 100

    settings = Settings(openrouter_api_key="test", openrouter_model="model-primary")
    llm = LLMClient(settings, transport=fake_transport)

    cc, meta = extract_covenants(pdf_path, llm, tmp_path / "cache")
    assert meta["attempts"] == 2
    assert _strip_verified(cc) == _strip_verified(v3_gold)


def test_fallback_model_used_when_primary_fails_twice_and_raises_without_fallback(
    tmp_path: Path,
) -> None:
    """When primary model fails twice, fallback model is used; without fallback, LLMError is raised."""
    golden_map = _load_golden()
    v4_gold = golden_map["Vertrag_4_Konsortialkredit.pdf"]
    pdf_path = CONTRACTS_DIR / v4_gold.contract_file

    models_called: list[str] = []

    def fake_transport_with_fallback(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> tuple[str, int, int]:
        models_called.append(model)
        if model == "primary-bad":
            return '{"broken": true}', 100, 20
        return v4_gold.model_dump_json(), 800, 200

    settings_with_fb = Settings(
        openrouter_api_key="test",
        openrouter_model="primary-bad",
        openrouter_model_fallback="fallback-good",
    )
    llm_fb = LLMClient(settings_with_fb, transport=fake_transport_with_fallback)
    cc, meta = extract_covenants(pdf_path, llm_fb, tmp_path / "cache_fb")
    assert models_called == ["primary-bad", "primary-bad", "fallback-good"]
    assert meta["model"] == "fallback-good"
    assert meta["attempts"] == 3
    assert _strip_verified(cc) == _strip_verified(v4_gold)

    # Without fallback -> LLMError after 2 attempts
    settings_no_fb = Settings(
        openrouter_api_key="test",
        openrouter_model="primary-bad",
        openrouter_model_fallback="",
    )
    llm_no_fb = LLMClient(settings_no_fb, transport=fake_transport_with_fallback)
    with pytest.raises(LLMError, match="after 2 attempts"):
        extract_covenants(pdf_path, llm_no_fb, tmp_path / "cache_no_fb")


def test_inconsistent_has_covenants_triggers_retry_and_contract_1_no_covenants(
    tmp_path: Path,
) -> None:
    """has_covenants=True with empty covenants is rejected and retried; Contract 1 (no covenants) passes with reason."""
    golden_map = _load_golden()
    v1_gold = golden_map["Vertrag_1_Sparkasse_Investitionsdarlehen.pdf"]
    pdf_path = CONTRACTS_DIR / v1_gold.contract_file

    bad_payload = {
        "contract_file": v1_gold.contract_file,
        "contract_label": v1_gold.contract_label,
        "has_covenants": True,
        "reason_if_none": None,
        "reason_source": None,
        "quantities": [],
        "covenants": [],
    }
    responses = [json.dumps(bad_payload), v1_gold.model_dump_json()]
    call_idx = 0

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> tuple[str, int, int]:
        nonlocal call_idx
        out = responses[call_idx]
        call_idx += 1
        return out, 400, 80

    settings = Settings(openrouter_api_key="test", openrouter_model="model-primary")
    llm = LLMClient(settings, transport=fake_transport)

    cc, meta = extract_covenants(pdf_path, llm, tmp_path / "cache_v1")
    assert meta["attempts"] == 2
    assert cc.has_covenants is False
    assert cc.reason_if_none == v1_gold.reason_if_none
    assert cc.reason_source is not None
    assert cc.reason_source.verified is True
