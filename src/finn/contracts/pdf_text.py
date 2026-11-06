"""PDF text extraction using PyMuPDF (pymupdf)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf
from pydantic import BaseModel


class PageText(BaseModel):
    """Extracted text of a single PDF page (1-based page number)."""

    page: int
    text: str


_INLINE_SPACES_RE = re.compile(r"[^\S\r\n]+")
_MULTI_BLANK_LINES_RE = re.compile(r"\n{3,}")


def _clean_page_text(raw_text: str) -> str:
    """Join words on each line with single spaces, keep line breaks, and collapse repeated blank lines."""
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cleaned_lines = [_INLINE_SPACES_RE.sub(" ", line).strip() for line in lines]
    joined = "\n".join(cleaned_lines).strip()
    return _MULTI_BLANK_LINES_RE.sub("\n\n", joined)


def extract_pages(path: Path) -> list[PageText]:
    """Extract all pages from a PDF file with 1-based page numbering."""
    path = Path(path)
    pages: list[PageText] = []
    with pymupdf.open(path) as doc:
        for idx, page in enumerate(doc, start=1):
            raw_text = page.get_text("text")
            pages.append(PageText(page=idx, text=_clean_page_text(raw_text)))
    return pages


def render_for_llm(pages: list[PageText]) -> str:
    """Render pages into a single string with '=== Seite N ===' markers before each page."""
    blocks = [f"=== Seite {p.page} ===\n{p.text}" for p in pages]
    return "\n\n".join(blocks)


def file_sha256(path: Path) -> str:
    """Compute the hex SHA-256 digest of a file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
