"""Guard test enforcing the project's no-hardcoding rule across src/finn/**/*.py."""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

SRC_FINN_DIR = Path(__file__).resolve().parents[1] / "src" / "finn"



def _years_in(text: str) -> list[str]:
    """4-digit runs starting with 19 or 20 that are not part of a longer number or word."""
    found: list[str] = []
    for i in range(len(text) - 3):
        chunk = text[i : i + 4]
        before = text[i - 1] if i > 0 else " "
        after = text[i + 4] if i + 4 < len(text) else " "
        if chunk.isdigit() and chunk[:2] in ("19", "20") and not before.isalnum() and not after.isalnum():
            found.append(chunk)
    return found

FORBIDDEN_LITERALS = (
    "GuV",
    "Bilanz",
    "Finanzierung",
    "EBITDA",
    "T€",
    "openrouter.ai",
)


def test_no_hardcoded_years_labels_sheets_units_or_vendors_in_src() -> None:
    """Scan src/finn/**/*.py and fail if 4-digit years or forbidden domain/vendor strings appear outside comments."""
    py_files = sorted(SRC_FINN_DIR.rglob("*.py"))
    assert py_files, f"No Python files found under {SRC_FINN_DIR}"

    violations: list[str] = []

    for path in py_files:
        rel_path = path.relative_to(SRC_FINN_DIR).as_posix()
        source_text = path.read_text(encoding="utf-8")

        # 1. AST string literal scan (includes docstrings and all string constants)
        tree = ast.parse(source_text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value
                lineno = getattr(node, "lineno", 0)

                for year in _years_in(val):
                    violations.append(
                        f"{rel_path}:{lineno}: 4-digit year '{year}' in string literal {val!r}"
                    )

                for forbidden in FORBIDDEN_LITERALS:
                    if forbidden == "openrouter.ai" and rel_path == "config.py":
                        continue
                    if forbidden in val:
                        violations.append(
                            f"{rel_path}:{lineno}: forbidden string '{forbidden}' in string literal {val!r}"
                        )

        # 2. Token scan outside # comments (catches identifiers or non-comment tokens)
        tokens = tokenize.generate_tokens(io.StringIO(source_text).readline)
        for tok in tokens:
            if tok.type in (tokenize.COMMENT, tokenize.STRING, tokenize.ENCODING, tokenize.ENDMARKER):
                continue
            tok_str = tok.string
            lineno = tok.start[0]
            for forbidden in FORBIDDEN_LITERALS:
                if forbidden == "openrouter.ai" and rel_path == "config.py":
                    continue
                if forbidden in tok_str:
                    violations.append(
                        f"{rel_path}:{lineno}: forbidden token '{forbidden}' in code token {tok_str!r}"
                    )

    assert not violations, "Hardcoding violations found in src/finn:\n" + "\n".join(violations)


def test_no_regular_expressions_in_src() -> None:
    """Project rule: no regex in src/. Parse with real parsers (openpyxl tokenizer) or plain string methods."""
    offenders: list[str] = []
    for path in sorted(SRC_FINN_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in ("re", "regex"):
                    offenders.append(f"{path.relative_to(SRC_FINN_DIR).as_posix()}:{node.lineno}: import {name}")
    assert not offenders, "Regex modules imported in src/finn:\n" + "\n".join(offenders)
