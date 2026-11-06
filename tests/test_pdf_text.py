"""Tests for PDF page text extraction and rendering."""

from __future__ import annotations

import json
from pathlib import Path

from finn.contracts.pdf_text import extract_pages, file_sha256, render_for_llm
from finn.models import ContractCovenants

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"


def test_extract_pages_and_markers_on_all_four_contracts() -> None:
    """Every contract PDF has 13 pages, page markers are rendered, and known quotes appear on their cited pages."""
    pdf_files = sorted(CONTRACTS_DIR.glob("*.pdf"))
    assert len(pdf_files) == 4

    for pdf_path in pdf_files:
        pages = extract_pages(pdf_path)
        assert len(pages) == 13, f"Expected 13 pages in {pdf_path.name}, got {len(pages)}"
        assert [p.page for p in pages] == list(range(1, 14))

        rendered = render_for_llm(pages)
        for p_num in range(1, 14):
            assert f"=== Seite {p_num} ===" in rendered

        digest = file_sha256(pdf_path)
        assert len(digest) == 64

    golden = [
        ContractCovenants.model_validate(item)
        for item in json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
    ]
    v2 = next(c for c in golden if c.contract_file == "Vertrag_2_Foerderbank_Foerderdarlehen.pdf")
    v2_pages = {p.page: p.text for p in extract_pages(CONTRACTS_DIR / v2.contract_file)}
    cov_src = v2.covenants[0].source
    assert cov_src.quote in v2_pages[cov_src.page]
