"""Guard test enforcing the project's no-hardcoding rule across src/finn/**/*.py."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

SRC_FINN_DIR = Path(__file__).resolve().parents[1] / "src" / "finn"

YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
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

                for m in YEAR_RE.finditer(val):
                    violations.append(
                        f"{rel_path}:{lineno}: 4-digit year '{m.group(0)}' in string literal {val!r}"
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
