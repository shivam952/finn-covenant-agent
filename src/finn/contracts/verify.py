"""Verification of extracted contract quotes and page citations against PDF page text."""

from __future__ import annotations

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


def normalize(s: str) -> str:
    """Normalize text for quote matching: NFKC, unify quotes/dashes, remove soft hyphens, collapse spaces, casefold."""
    nfkc = unicodedata.normalize("NFKC", s)
    translated = nfkc.translate(_QUOTE_TRANSLATION)
    collapsed = " ".join(translated.split())
    return collapsed.casefold()


def _alnum_only(norm_s: str) -> str:
    """Keep only letters and digits from an already normalized string (for line-break hyphenation tolerance)."""
    return "".join(ch for ch in norm_s if ch.isalnum())


def verify_source(source: Source, pages: list[PageText]) -> Source:
    """Verify that source.quote appears on source.page (or another page) and return an updated copy."""
    norm_quote = normalize(source.quote)
    if len(norm_quote) < 15:
        return source.model_copy(
            update={
                "verified": False,
                "verify_note": "quote_too_short",
            }
        )

    alnum_quote = _alnum_only(norm_quote)
    page_map = {p.page: normalize(p.text) for p in pages}
    alnum_map = {p_num: _alnum_only(txt) for p_num, txt in page_map.items()}
    sorted_page_nums = sorted(page_map.keys())

    # a) Check cited page (normal, then alnum-only)
    cited_text = page_map.get(source.page)
    if cited_text is not None:
        if norm_quote in cited_text:
            return source.model_copy(update={"verified": True, "verify_note": None})
        if alnum_quote and alnum_quote in alnum_map[source.page]:
            return source.model_copy(
                update={"verified": True, "verify_note": "quote_loose_match"}
            )

    # c) Check cited page + next page joined (normal, then alnum-only)
    next_text = page_map.get(source.page + 1)
    if cited_text is not None and next_text is not None:
        if norm_quote in f"{cited_text} {next_text}":
            return source.model_copy(update={"verified": True, "verify_note": None})
        if alnum_quote and alnum_quote in (
            alnum_map[source.page] + alnum_map[source.page + 1]
        ):
            return source.model_copy(
                update={"verified": True, "verify_note": "quote_loose_match"}
            )

    # b) Check all other single pages (normal first, then alnum-only)
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
                "verify_note": "quote_page_corrected", "original_page": source.page,
            }
        )

    if len(other_single_matches) == 0 and alnum_quote:
        other_alnum_matches = [
            p_num
            for p_num in sorted_page_nums
            if p_num != source.page and alnum_quote in alnum_map[p_num]
        ]
        if len(other_alnum_matches) == 1:
            found_page = other_alnum_matches[0]
            return source.model_copy(
                update={
                    "page": found_page,
                    "verified": True,
                    "verify_note": "quote_page_corrected", "original_page": source.page,
                }
            )

    # Check other page-break pairs (normal first, then alnum-only)
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
                    "verify_note": "quote_page_corrected", "original_page": source.page,
                }
            )
        if len(other_pair_matches) == 0 and alnum_quote:
            other_pair_alnum = [
                p_num
                for p_num in sorted_page_nums
                if p_num != source.page
                and (p_num + 1) in alnum_map
                and alnum_quote in (alnum_map[p_num] + alnum_map[p_num + 1])
            ]
            if len(other_pair_alnum) == 1:
                found_page = other_pair_alnum[0]
                return source.model_copy(
                    update={
                        "page": found_page,
                        "verified": True,
                        "verify_note": "quote_page_corrected", "original_page": source.page,
                    }
                )

    # d) Not found (or ambiguous across multiple non-cited pages)
    return source.model_copy(
        update={
            "verified": False,
            "verify_note": "quote_not_found",
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
