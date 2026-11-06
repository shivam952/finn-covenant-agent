"""Verification of extracted contract quotes and page citations against PDF page text."""

from __future__ import annotations

import re
import unicodedata

from finn.contracts.pdf_text import PageText
from finn.models import ContractCovenants, Source

_QUOTE_TRANSLATION = str.maketrans(
    {
        "„": '"',
        "“": '"',
        "”": '"',
        "«": '"',
        "»": '"',
        "‚": '"',
        "‘": '"',
        "’": '"',
        "'": '"',
        "–": "-",
        "—": "-",
        "‑": "-",
        "−": "-",
        "\u00ad": None,
    }
)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize(s: str) -> str:
    """Normalize text for quote matching: NFKC, unify quotes/dashes, remove soft hyphens, collapse spaces, casefold."""
    nfkc = unicodedata.normalize("NFKC", s)
    translated = nfkc.translate(_QUOTE_TRANSLATION)
    collapsed = _WHITESPACE_RE.sub(" ", translated).strip()
    return collapsed.casefold()


def verify_source(source: Source, pages: list[PageText]) -> Source:
    """Verify that source.quote appears on source.page (or another page) and return an updated copy."""
    norm_quote = normalize(source.quote)
    if len(norm_quote) < 15:
        return source.model_copy(
            update={
                "verified": False,
                "verify_note": "Zitat zu kurz für eine Prüfung",
            }
        )

    page_map = {p.page: normalize(p.text) for p in pages}
    sorted_page_nums = sorted(page_map.keys())

    # a) Check cited page
    cited_text = page_map.get(source.page)
    if cited_text is not None and norm_quote in cited_text:
        return source.model_copy(update={"verified": True, "verify_note": None})

    # c) Check cited page + next page joined (page break wrap)
    next_text = page_map.get(source.page + 1)
    if cited_text is not None and next_text is not None:
        if norm_quote in f"{cited_text} {next_text}":
            return source.model_copy(update={"verified": True, "verify_note": None})

    # b) Check all other single pages
    other_single_matches = [
        p_num
        for p_num in sorted_page_nums
        if p_num != source.page and norm_quote in page_map[p_num]
    ]
    if len(other_single_matches) == 1:
        found_page = other_single_matches[0]
        return source.model_copy(
            update={
                "page": found_page,
                "verified": True,
                "verify_note": f"Seitenangabe korrigiert: S. {found_page} statt S. {source.page}",
            }
        )

    # Also check other page-break pairs if not found on single pages
    if len(other_single_matches) == 0:
        other_pair_matches = [
            p_num
            for p_num in sorted_page_nums
            if p_num != source.page
            and (p_num + 1) in page_map
            and norm_quote in f"{page_map[p_num]} {page_map[p_num + 1]}"
        ]
        if len(other_pair_matches) == 1:
            found_page = other_pair_matches[0]
            return source.model_copy(
                update={
                    "page": found_page,
                    "verified": True,
                    "verify_note": f"Seitenangabe korrigiert: S. {found_page} statt S. {source.page}",
                }
            )

    # d) Not found (or ambiguous across multiple non-cited pages)
    return source.model_copy(
        update={
            "verified": False,
            "verify_note": "Zitat im Vertrag nicht gefunden",
        }
    )


def verify_contract(cc: ContractCovenants, pages: list[PageText]) -> ContractCovenants:
    """Apply verify_source to every Source inside a ContractCovenants instance."""
    new_reason_source = (
        verify_source(cc.reason_source, pages) if cc.reason_source is not None else None
    )
    new_quantities = [
        q.model_copy(update={"source": verify_source(q.source, pages)})
        for q in cc.quantities
    ]
    new_covenants = [
        c.model_copy(
            update={
                "source": verify_source(c.source, pages),
                "definition_sources": [
                    verify_source(ds, pages) for ds in c.definition_sources
                ],
            }
        )
        for c in cc.covenants
    ]
    return cc.model_copy(
        update={
            "reason_source": new_reason_source,
            "quantities": new_quantities,
            "covenants": new_covenants,
        }
    )
