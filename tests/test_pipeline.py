"""End-to-end offline tests for the pipeline and `finn run --eml` CLI using FakeLLM."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

import pytest
from pydantic import BaseModel

import finn.main
import finn.pipeline
from finn.audit import write_audit
from finn.config import Settings
from finn.main import main
from finn.mapping import MappingItem, MappingResponse
from finn.models import ContractCovenants, TermMapping
from finn.pipeline import RunContext, run_check

T = TypeVar("T", bound=BaseModel)

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_XLSX = REPO_ROOT / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
SAMPLES_DIR = REPO_ROOT / "data" / "samples"
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"
EXPECTED_PATH = REPO_ROOT / "tests" / "expected_2027.json"
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"
GOLDEN_MAPPING_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_mapping.json"


class GoldenFakeLLM:
    """Fake JsonLLM that returns golden extraction and mapping fixtures without network access."""

    model_name = "fake-golden-llm"

    def __init__(self) -> None:
        self.contracts = {
            c["contract_file"]: ContractCovenants.model_validate(c)
            for c in json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
        }
        self.mappings = [
            TermMapping.model_validate(m)
            for m in json.loads(GOLDEN_MAPPING_PATH.read_text(encoding="utf-8"))
        ]

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        purpose: str,
    ) -> tuple[T, dict[str, Any]]:
        meta = {
            "purpose": purpose,
            "model": self.model_name,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "latency_s": 0.01,
            "attempts": 1,
        }
        if purpose.startswith("extract_covenants:"):
            c_file = purpose.split(":", 1)[1]
            return self.contracts[c_file], meta  # type: ignore[return-value]

        if purpose.startswith("map_terms:"):
            c_file = purpose.split(":", 1)[1]
            items = [
                MappingItem(
                    quantity=m.quantity,
                    component=m.component,
                    item_ids=list(m.item_ids),
                    rationale=m.rationale,
                )
                for m in self.mappings
                if m.contract_file == c_file
            ]
            return MappingResponse(mappings=items), meta  # type: ignore[return-value]

        raise AssertionError(f"Unexpected purpose: {purpose}")


def test_run_check_produces_expected_results_and_redacts_secrets_in_audit(
    tmp_path: Path,
) -> None:
    """run_check matches expected_2027.json, reports 1 contract without covenants, and strips secrets from audit."""
    secret_key = "sk-or-v1-TOP-SECRET-DO-NOT-LEAK-99999"
    secret_pw = "gmail-app-password-SUPER-SECRET-88888"
    settings = Settings(
        openrouter_api_key=secret_key,
        openrouter_model="fake-golden-llm",
        mail_app_password=secret_pw,
        contracts_dir=CONTRACTS_DIR,
        cache_dir=tmp_path / "cache",
        runs_dir=tmp_path / "runs",
    )
    fake_llm = GoldenFakeLLM()
    ctx = RunContext.create(settings=settings, llm=fake_llm, run_id="test_run_001")

    outcome = run_check(SAMPLE_XLSX, ctx)
    report = outcome.report

    assert len(report.results) == 4
    assert len(report.contracts_without_covenants) == 1
    assert (
        report.contracts_without_covenants[0].contract_file
        == "Vertrag_1_Sparkasse_Investitionsdarlehen.pdf"
    )

    expected = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    by_name = {r.covenant_name: r for r in report.results}

    for c_info in expected["contracts"].values():
        if not c_info["has_covenants"]:
            continue
        for cov_name, exp_cov in c_info["covenants"].items():
            res = by_name[cov_name]
            assert round(res.value_reported, 4) == pytest.approx(
                exp_cov["value_reported"], abs=1e-6
            )
            assert round(res.headroom, 4) == pytest.approx(
                exp_cov["headroom"], abs=1e-6
            )
            assert res.met is exp_cov["met"]

    # Audit log contains every step and zero secrets
    steps = outcome.audit["steps"]
    assert set(steps.keys()) == {
        "read_plan",
        "extract_covenants",
        "map_terms",
        "calculate",
    }
    assert "313.200" in steps["read_plan"]["catalog_with_values"]

    audit_path = write_audit(outcome.audit, settings.runs_dir)
    audit_text = audit_path.read_text(encoding="utf-8")
    assert secret_key not in audit_text
    assert secret_pw not in audit_text
    assert "openrouter_api_key" not in audit_text
    assert "mail_app_password" not in audit_text


def test_cli_finn_run_eml_and_no_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`finn run --eml order.eml` exits 0 and prints table; `no_attachment.eml` exits 2."""
    fake_settings = Settings(
        openrouter_api_key="dummy-secret",
        openrouter_model="fake-golden-llm",
        contracts_dir=CONTRACTS_DIR,
        cache_dir=tmp_path / "cache",
        runs_dir=tmp_path / "runs",
    )
    fake_llm = GoldenFakeLLM()

    monkeypatch.setattr(finn.main, "load_settings", lambda *a, **kw: fake_settings)
    monkeypatch.setattr(finn.main, "make_llm", lambda _s: fake_llm)
    monkeypatch.setattr(finn.pipeline, "make_llm", lambda _s: fake_llm)

    code_ok = main(
        [
            "run",
            "--eml",
            str(SAMPLES_DIR / "order.eml"),
            "--compare-expected",
        ]
    )
    assert code_ok == 0
    out_ok = capsys.readouterr().out
    assert "Wirtschaftliche Eigenkapitalquote" in out_ok
    assert "Schuldendienstdeckungsgrad" in out_ok
    assert "Dynamischer Verschuldungsgrad" in out_ok
    assert "Audit-Log:" in out_ok

    code_no_att = main(["run", "--eml", str(SAMPLES_DIR / "no_attachment.eml")])
    assert code_no_att == 2
    out_no_att = capsys.readouterr().out
    assert ".xlsx" in out_no_att

    code_wrong_att = main(["run", "--eml", str(SAMPLES_DIR / "wrong_attachment.eml")])
    assert code_wrong_att == 2
