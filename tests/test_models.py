"""Tests for Pydantic models and the hand-checked 2027 answer key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from finn.config import Settings
from finn.models import (
    Component,
    ContractCovenants,
    Covenant,
    Quantity,
    Source,
)

EXPECTED_PATH = Path(__file__).parent / "expected_2027.json"


def test_expected_2027_loads_and_ratios_match() -> None:
    """expected_2027.json loads and every covenant numerator/denominator matches value_raw within 1e-3 relative."""
    data = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))
    assert data["test_date"] == "31.12.2027"
    assert "base_values" in data
    assert "contracts" in data

    contracts = data["contracts"]
    assert contracts["Vertrag_1_Sparkasse_Investitionsdarlehen.pdf"]["has_covenants"] is False

    checked_count = 0
    for contract_name, contract_info in contracts.items():
        if not contract_info["has_covenants"]:
            continue
        for cov_name, cov in contract_info["covenants"].items():
            numerator = float(cov["numerator"])
            denominator = float(cov["denominator"])
            value_raw = float(cov["value_raw"])
            unit = cov["unit"]
            assert unit in ("percent", "ratio")
            scale = 100.0 if unit == "percent" else 1.0
            computed = (numerator / denominator) * scale
            assert computed == pytest.approx(value_raw, rel=1e-3), (
                f"Mismatch for {contract_name} / {cov_name}: {computed} != {value_raw}"
            )
            checked_count += 1

    assert checked_count == 4


def test_contract_covenants_validates_and_invalid_sign_rejected() -> None:
    """A minimal ContractCovenants with one Quantity/Covenant validates; a Component with sign 2 is rejected."""
    source = Source(
        page=4,
        section="§ 8 Abs. 1",
        quote="Der Darlehensnehmer verpflichtet sich, eine Eigenkapitalquote von mindestens 30,0 % einzuhalten.",
    )
    qty_ek = Quantity(
        name="Eigenkapital",
        components=[
            Component(
                name="Eigenkapital",
                sign=1,
                weight=1.0,
                definition="Ausgewiesenes Eigenkapital laut Bilanz",
            )
        ],
        source=source,
    )
    cov = Covenant(
        id="V4-EKQ",
        name="Eigenkapitalquote",
        numerator="Eigenkapital",
        denominator="Bilanzsumme",
        operator=">=",
        limit=30.0,
        unit="percent",
        rounding_decimals=1,
        test_basis="stichtag",
        source=source,
        definition_sources=[source],
        plan_basis_note=None,
    )
    contract = ContractCovenants(
        contract_file="Vertrag_4_Konsortialkredit.pdf",
        contract_label="Vertrag 4 – Konsortialkredit",
        has_covenants=True,
        reason_if_none=None,
        reason_source=None,
        quantities=[qty_ek],
        covenants=[cov],
    )
    assert contract.has_covenants is True
    assert len(contract.quantities) == 1
    assert len(contract.covenants) == 1
    assert contract.quantities[0].components[0].sign == 1

    with pytest.raises(ValidationError):
        Component(
            name="Invalid Sign Component",
            sign=2,  # type: ignore[arg-type]
            definition="Must fail validation because sign is not 1 or -1",
        )


def test_settings_loads_without_crashing_on_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """config.py does not crash when LLM or mail variables are unset, and raises only when required."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("FINN_MAIL_ADDRESS", raising=False)
    monkeypatch.delenv("MAIL_USER", raising=False)
    monkeypatch.delenv("MAIL_APP_PASSWORD", raising=False)

    settings = Settings.from_env(load_env_file=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        settings.require_llm()
    with pytest.raises(ValueError, match="MAIL_USER"):
        settings.require_mail()


def test_settings_treats_hash_comment_as_empty_and_defaults_finn_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Values starting with '#' are treated as empty; FINN_MAIL_ADDRESS defaults to MAIL_USER."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "# inline comment that should be ignored")
    monkeypatch.delenv("FINN_MAIL_ADDRESS", raising=False)
    monkeypatch.setenv("MAIL_USER", "finn.bot@gmail.com")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "app-secret")

    settings = Settings.from_env(load_env_file=False)
    assert settings.openrouter_model == ""
    assert settings.finn_mail_address == "finn.bot@gmail.com"
    settings.require_mail()

    with pytest.raises(ValueError, match="OPENROUTER_MODEL"):
        settings.require_llm()
