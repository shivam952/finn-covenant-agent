"""Tests for deterministic contract quote verification."""

from __future__ import annotations

import json
from pathlib import Path

from finn.contracts.pdf_text import extract_pages
from finn.contracts.verify import verify_contract, verify_source
from finn.models import ContractCovenants, Source

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"


def test_every_source_in_golden_covenants_verifies_true() -> None:
    """Every Source across all 4 contracts in golden_covenants.json verifies as True against the real PDFs."""
    golden = [
        ContractCovenants.model_validate(item)
        for item in json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
    ]

    total_checked = 0
    for contract in golden:
        pages = extract_pages(CONTRACTS_DIR / contract.contract_file)
        verified_cc = verify_contract(contract, pages)

        if verified_cc.reason_source is not None:
            assert verified_cc.reason_source.verified is True
            assert verified_cc.reason_source.verify_note is None
            total_checked += 1

        for qty in verified_cc.quantities:
            assert qty.source.verified is True, f"Failed: {qty.source}"
            assert qty.source.verify_note is None
            total_checked += 1

        for cov in verified_cc.covenants:
            assert cov.source.verified is True, f"Failed: {cov.source}"
            assert cov.source.verify_note is None
            total_checked += 1
            for ds in cov.definition_sources:
                assert ds.verified is True, f"Failed: {ds}"
                assert ds.verify_note is None
                total_checked += 1

    assert total_checked == 26


def test_tampered_quote_wrong_page_and_typographic_quotes() -> None:
    """Tampered quote -> False; wrong page -> True + corrected page + verify_note; typographic quotes -> True."""
    pages = extract_pages(CONTRACTS_DIR / "Vertrag_2_Foerderbank_Foerderdarlehen.pdf")

    # 1. Tampered quote (one word changed)
    tampered = Source(
        page=6,
        section="§ 13 Abs. 1",
        quote="eine Wirtschaftliche Eigenkapitalquote von mindestens 99,5 % einzuhalten",
    )
    res_tampered = verify_source(tampered, pages)
    assert res_tampered.verified is False
    assert res_tampered.verify_note == "Zitat im Vertrag nicht gefunden"

    # 2. Correct quote on wrong page (page 11 instead of page 6)
    wrong_page = Source(
        page=11,
        section="§ 13 Abs. 1",
        quote="eine Wirtschaftliche Eigenkapitalquote von mindestens 27,5 % einzuhalten",
    )
    res_wrong_page = verify_source(wrong_page, pages)
    assert res_wrong_page.verified is True
    assert res_wrong_page.page == 6
    assert res_wrong_page.verify_note == "Seitenangabe korrigiert: S. 6 statt S. 11"

    # 3. Typographic quote variant ("..." instead of „...“)
    ascii_quotes = Source(
        page=2,
        section="§ 1 Abs. 1",
        quote='"Wirtschaftliches Eigenkapital" bezeichnet das Eigenkapital zuzüglich 50 % (fünfzig Prozent) der Sonderposten.',
    )
    res_ascii = verify_source(ascii_quotes, pages)
    assert res_ascii.verified is True
    assert res_ascii.verify_note is None


def test_hyphenation_tolerant_quote_verification_on_contract_3_page_2() -> None:
    """Quote 'Bilanzierungs- und Bewertungsmethoden' verifies against contract 3 page 2 even when PDF text has 'Bilanzierungsund'."""
    pages = extract_pages(
        CONTRACTS_DIR / "Vertrag_3_Landesbank_Investitionsdarlehen.pdf"
    )
    src = Source(
        page=2,
        section="§ 1 Abs. 3",
        quote="Bilanzierungs- und Bewertungsmethoden",
    )
    res_direct = verify_source(src, pages)
    assert res_direct.verified is True
    assert res_direct.page == 2

    # Simulate PyMuPDF dehyphenation ('Bilanzierungs-\nund' -> 'Bilanzierungsund') on page 2
    dehyphenated_pages = [
        p.model_copy(
            update={"text": p.text.replace("Bilanzierungs-\nund", "Bilanzierungsund")}
        )
        if p.page == 2
        else p
        for p in pages
    ]
    assert "Bilanzierungsund" in dehyphenated_pages[1].text
    res_dehyph = verify_source(src, dehyphenated_pages)
    assert res_dehyph.verified is True
    assert res_dehyph.page == 2
    assert res_dehyph.verify_note == "Abgleich ohne Satzzeichen/Trennstriche"
