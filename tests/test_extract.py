"""Tests for LLM covenant extraction, caching, retries, and fallback using FakeLLM (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from finn.config import Settings
from finn.contracts.extract import extract_covenants
from finn.llm import LLMClient, LLMError, TransportReturn
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
    ) -> TransportReturn:
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
    ) -> TransportReturn:
        nonlocal idx
        out = responses[idx]
        idx += 1
        return out, 500, 100

    settings = Settings(openrouter_api_key="test", openrouter_model="model-primary")
    llm = LLMClient(settings, transport=fake_transport)

    cc, meta = extract_covenants(pdf_path, llm, tmp_path / "cache")
    assert meta["attempts"] == 2
    assert len(meta["attempt_log"]) == 2
    assert meta["attempt_log"][0]["status"] == "error"
    assert meta["attempt_log"][1]["status"] == "ok"
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
    ) -> TransportReturn:
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
    ) -> TransportReturn:
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


def test_http_401_skips_same_model_retry_and_uses_fallback_while_budget_exceeded_raises(
    tmp_path: Path,
) -> None:
    """HTTP 401 does not retry on the same model and goes straight to fallback; exceeding time budget raises LLMError."""
    golden_map = _load_golden()
    v2_gold = golden_map["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    pdf_path = CONTRACTS_DIR / v2_gold.contract_file

    class FakeHTTPError(Exception):
        def __init__(self, status_code: int, msg: str) -> None:
            super().__init__(msg)
            self.status_code = status_code

    models_called: list[str] = []
    sleeps: list[float] = []

    def fake_transport_401(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        models_called.append(model)
        if model == "model-401":
            raise FakeHTTPError(401, "Unauthorized / invalid model access")
        return v2_gold.model_dump_json(), 600, 150

    settings = Settings(
        openrouter_api_key="test",
        openrouter_model="model-401",
        openrouter_model_fallback="model-fallback",
    )
    llm = LLMClient(
        settings,
        transport=fake_transport_401,
        sleep_fn=lambda s: sleeps.append(s),
    )
    cc, meta = extract_covenants(pdf_path, llm, tmp_path / "cache_401")
    # Only 1 attempt on model-401 (no retry on 401, no sleep), then 1 attempt on model-fallback
    assert models_called == ["model-401", "model-fallback"]
    assert sleeps == []
    assert meta["attempts"] == 2
    assert meta["model"] == "model-fallback"
    assert meta["attempt_log"][0]["status_code"] == 401
    assert _strip_verified(cc) == _strip_verified(v2_gold)

    # Time budget exceeded -> raises LLMError immediately
    fake_now = 0.0

    def advancing_clock() -> float:
        return fake_now

    def slow_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        nonlocal fake_now
        fake_now += 200.0
        return v2_gold.model_dump_json(), 100, 50

    budget_settings = Settings(
        openrouter_api_key="test",
        openrouter_model="slow-model",
        llm_time_budget_s=180.0,
    )
    llm_budget = LLMClient(
        budget_settings,
        transport=slow_transport,
        clock_fn=advancing_clock,
    )
    with pytest.raises(LLMError, match="time budget"):
        extract_covenants(pdf_path, llm_budget, tmp_path / "cache_budget")


def test_finish_reason_length_triggers_retry(tmp_path: Path) -> None:
    """When finish_reason is 'length', LLMClient treats it as a truncated failure and retries."""
    golden_map = _load_golden()
    v2_gold = golden_map["Vertrag_2_Foerderbank_Foerderdarlehen.pdf"]
    pdf_path = CONTRACTS_DIR / v2_gold.contract_file

    attempt = 0

    def fake_transport_length(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return v2_gold.model_dump_json()[:120], 900, 12000, "length"
        return v2_gold.model_dump_json(), 950, 300, "stop"

    settings = Settings(openrouter_api_key="test", openrouter_model="model-primary")
    llm = LLMClient(settings, transport=fake_transport_length)
    cc, meta = extract_covenants(pdf_path, llm, tmp_path / "cache_len")
    assert meta["attempts"] == 2
    assert "Antwort abgeschnitten (max_tokens)" in meta["attempt_log"][0]["error"]
    assert _strip_verified(cc) == _strip_verified(v2_gold)


class _NoEndpointError(Exception):
    """Mimics the gateway error raised when no endpoint honours the provider parameters."""

    status_code = 404

    def __init__(self) -> None:
        super().__init__("No endpoints found that can handle the requested parameters.")


def test_provider_params_rejection_retries_same_model_without_them() -> None:
    """A 404 'no endpoints for the requested parameters' retries the SAME model once without provider params."""
    settings = Settings(llm_api_key="dummy", llm_model="primary/model", llm_model_fallback="")
    golden = _load_golden()["Vertrag_1_Sparkasse_Investitionsdarlehen.pdf"]
    payload = json.dumps(_strip_verified(golden))
    seen_params: list[Any] = []
    llm: LLMClient

    def fake_transport(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        seen_params.append(llm.current_provider_params)
        if llm.current_provider_params is not None:
            raise _NoEndpointError()
        return payload, 10, 10

    llm = LLMClient(settings, transport=fake_transport)
    result, meta = llm.complete_json("sys", "user", ContractCovenants, purpose="t")

    assert result.has_covenants is False
    assert meta["model"] == "primary/model"
    assert seen_params[0] == {"require_parameters": True}
    assert seen_params[1] is None
    assert any(a.get("action") == "require_parameters disabled" for a in meta["attempt_log"])


def test_provider_params_rejection_is_relaxed_only_once_per_model() -> None:
    """If the model still fails after relaxing the params, the client falls back instead of looping."""
    settings = Settings(llm_api_key="dummy", llm_model="primary/model", llm_model_fallback="")
    llm: LLMClient
    calls: list[str] = []

    def always_404(
        model: str, messages: list[dict[str, str]], schema: dict[str, Any]
    ) -> TransportReturn:
        calls.append(model)
        raise _NoEndpointError()

    llm = LLMClient(settings, transport=always_404)
    with pytest.raises(LLMError):
        llm.complete_json("sys", "user", ContractCovenants, purpose="t")
    assert len(calls) == 2


def test_golden_limits_all_appear_in_their_quotes() -> None:
    from finn.contracts.extract import limit_not_in_quote

    for cc in _load_golden().values():
        assert limit_not_in_quote(cc) == [], cc.contract_file


def test_limit_missing_from_quote_triggers_one_corrective_retry(tmp_path: Path) -> None:
    """A limit read from the wrong clause (e.g. a pricing grid) is caught: the quote does not contain it."""
    golden = _load_golden()["Vertrag_4_Konsortialkredit.pdf"]
    wrong = golden.model_copy(deep=True)
    wrong.covenants[1].limit = 3.0  # e.g. a step of the margin grid instead of the 3,50 maximum
    answers = [json.dumps(_strip_verified(wrong)), json.dumps(_strip_verified(golden))]
    calls: list[str] = []

    def transport(model: str, messages: list[dict[str, str]], schema: dict[str, Any]) -> TransportReturn:
        calls.append(messages[-1]["content"])
        return answers.pop(0), 10, 10

    settings = Settings(llm_api_key="x", llm_model="m")
    cc, meta = extract_covenants(CONTRACTS_DIR / "Vertrag_4_Konsortialkredit.pdf",
                                 LLMClient(settings, transport=transport), tmp_path)
    assert len(calls) == 2 and "V4-DYNVG" in calls[1]
    assert cc.covenants[1].limit == 3.5
    assert meta["correction_retry"]["missing_limits_after"] == []
