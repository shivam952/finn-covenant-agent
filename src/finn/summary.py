"""LLM-written summary of the results, guarded by a strict number check in code.

The LLM only receives numbers that are already rendered as strings and is not allowed to
introduce any number that does not occur in those facts. Every number token in its answer
is extracted and compared; a single unknown number discards the summary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from finn.llm import JsonLLM
from finn.numbers import extract_number_tokens

DEFAULT_SUMMARY_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "summary.txt"



class SummarySentence(BaseModel):
    """One summary sentence and the covenants it talks about (attribution is checked by code)."""

    text: str
    covenant_ids: list[str] = Field(default_factory=list)


class Summary(BaseModel):
    """LLM output schema for the reply summary."""

    sentences: list[SummarySentence] = Field(min_length=1)


@dataclass
class SummaryOutcome:
    """Accepted summary sentences plus audit metadata."""

    sentences: list[str]
    source: str  # "llm" or "template"
    meta: dict[str, Any] = field(default_factory=dict)


def allowed_number_set(facts: dict[str, Any], lang: str, small_counts_max: int) -> set[str]:
    """All number tokens that occur anywhere in the facts, plus small counts."""
    allowed: set[str] = set()

    def _walk(node: Any) -> None:
        if isinstance(node, str):
            allowed.update(extract_number_tokens(node, lang))
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            allowed.update(extract_number_tokens(str(node), "en"))

    _walk(facts)
    for i in range(0, small_counts_max + 1):
        allowed.add(str(i))
    return allowed


def find_unsupported_numbers(
    sentences: list[str], allowed: set[str], lang: str
) -> list[str]:
    """Return every number token in the sentences that is not in the allowed set."""
    bad: list[str] = []
    for sentence in sentences:
        for tok in extract_number_tokens(sentence, lang):
            if tok not in allowed and tok not in bad:
                bad.append(tok)
    return bad


def _violates_shape(sentences: list[str], max_sentences: int, max_chars: int) -> list[str]:
    problems: list[str] = []
    if len(sentences) > max_sentences:
        problems.append(f"too many sentences ({len(sentences)} > {max_sentences})")
    for s in sentences:
        if len(s) > max_chars:
            problems.append(f"sentence longer than {max_chars} characters")
        if "**" in s or s.lstrip().startswith(("#", "-", "*")):
            problems.append("markdown is not allowed")
    return problems


def _sentence_problems(
    sentence: SummarySentence,
    facts: dict[str, Any],
    lang: str,
    small_counts_max: int,
    status_claims: dict[str, list[str]],
) -> list[str]:
    """Why a sentence is not backed by the facts it claims to be about (empty list = fine).

    1. Every covenant id must exist.
    2. Every number must occur in the general facts or in the facts of the referenced covenants
       (a number borrowed from another covenant is rejected).
    3. A status word (from the text files, e.g. "breached") may only be used if a referenced
       covenant really has that status.
    """
    problems: list[str] = []
    covs = {c["id"]: c for c in facts.get("covenants", [])}
    general = {k: v for k, v in facts.items() if k != "covenants"}
    allowed = allowed_number_set(general, lang, small_counts_max)
    statuses: set[str] = set()
    for cid in sentence.covenant_ids:
        if cid not in covs:
            problems.append(f"unknown covenant id: {cid}")
            continue
        allowed |= allowed_number_set(covs[cid], lang, 0)
        statuses.add(str(covs[cid].get("status_key", "")))
    for tok in find_unsupported_numbers([sentence.text], allowed, lang):
        problems.append(f"number {tok} does not belong to the referenced covenants {sentence.covenant_ids}")
    low = sentence.text.casefold()
    breach_claimed = any(w.casefold() in low for w in status_claims.get("breach", []))
    if breach_claimed and "breach" not in statuses:
        problems.append("claims a breach, but none of the referenced covenants is breached")
    elif not breach_claimed and any(w.casefold() in low for w in status_claims.get("met", [])):
        if "breach" in statuses:
            problems.append("claims a covenant is met, but a referenced covenant is breached")
    return problems


def write_summary(
    facts: dict[str, Any],
    lang: str,
    llm: JsonLLM | None,
    template_sentences: list[str],
    *,
    max_sentences: int,
    max_sentence_chars: int,
    small_counts_max: int,
    status_claims: dict[str, list[str]] | None = None,
    prompt_path: Path | None = None,
) -> SummaryOutcome:
    """Ask the LLM for a short summary; accept it only if every sentence is backed by its facts.

    Falls back to the deterministic template sentences on any LLM error or failed check,
    so the reply is never blocked by the language model.
    """
    meta: dict[str, Any] = {"attempts": []}
    if llm is None:
        meta["reason"] = "no LLM configured"
        return SummaryOutcome(template_sentences, "template", meta)

    claims = status_claims or {}
    system = (prompt_path or DEFAULT_SUMMARY_PROMPT_PATH).read_text(encoding="utf-8")
    base_user = json.dumps({"language": lang, "facts": facts}, ensure_ascii=False, indent=2)
    user = base_user

    for attempt in range(2):
        try:
            summary, llm_meta = llm.complete_json(
                system=system, user=user, schema=Summary, purpose="summary"
            )
        except Exception as exc:  # the summary must never block the reply
            meta["attempts"].append(
                {"attempt": attempt + 1, "error": f"{type(exc).__name__}: {exc}"}
            )
            break

        sentences = [s for s in summary.sentences if s.text and s.text.strip()]
        texts = [s.text.strip() for s in sentences]
        problems: list[str] = []
        for s in sentences:
            problems += [f"„{s.text[:60]}…“: {p}" for p in _sentence_problems(s, facts, lang, small_counts_max, claims)]
        problems += _violates_shape(texts, max_sentences, max_sentence_chars)
        meta["attempts"].append(
            {
                "attempt": attempt + 1,
                "llm": llm_meta,
                "sentences": [s.model_dump() for s in sentences],
                "problems": problems,
            }
        )
        if texts and not problems:
            return SummaryOutcome(texts, "llm", meta)

        user = (
            base_user
            + "\n\nYour previous answer was rejected by code: "
            + "; ".join(problems)
            + ". Attribute every sentence to the covenant ids it talks about, use only those"
            " covenants' numbers exactly as written, and never claim a status a covenant does not have."
        )

    meta["reason"] = "summary rejected or LLM failed; template used"
    return SummaryOutcome(template_sentences, "template", meta)
