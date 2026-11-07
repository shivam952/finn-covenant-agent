"""Shared offline fake LLM that returns the hand-checked golden fixtures (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from finn.llm import LLMError
from finn.mapping import MappingItem, MappingResponse
from finn.models import ContractCovenants, TermMapping
from finn.intake import OrderIntent
from finn.summary import Summary, SummarySentence

T = TypeVar("T", bound=BaseModel)

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_COVENANTS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_covenants.json"
GOLDEN_KINDS_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_kinds.json"
GOLDEN_MAPPING_PATH = REPO_ROOT / "tests" / "fixtures" / "golden_mapping.json"


class GoldenFakeLLM:
    """Fake JsonLLM that returns golden extraction (with kinds overlay) and mapping fixtures without network access."""

    model_name = "fake-golden-llm"

    def __init__(
        self,
        summary_answers: list[list[str]] | None = None,
        intent: "OrderIntent | None" = None,
    ) -> None:
        # Answer for purpose "intake"; None makes the intake call fail (tests the fallback).
        self.intent = intent
        # Each call with purpose "summary" pops the next answer (a list of sentences).
        self.summary_answers = list(summary_answers or [])
        self.summary_calls: list[str] = []
        raw_contracts = json.loads(GOLDEN_COVENANTS_PATH.read_text(encoding="utf-8"))
        kinds_overlay = json.loads(GOLDEN_KINDS_PATH.read_text(encoding="utf-8"))
        for c_dict in raw_contracts:
            c_kinds = kinds_overlay.get(c_dict["contract_file"], {})
            for q_dict in c_dict["quantities"]:
                if q_dict["name"] in c_kinds:
                    q_dict["kind"] = c_kinds[q_dict["name"]]
        self.contracts = {
            c["contract_file"]: ContractCovenants.model_validate(c)
            for c in raw_contracts
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

        if purpose == "intake":
            if self.intent is None:
                raise LLMError("no fake intent configured")
            return self.intent, meta  # type: ignore[return-value]

        if purpose == "summary":
            self.summary_calls.append(user)
            if not self.summary_answers:
                raise LLMError("no fake summary configured")
            answer = self.summary_answers.pop(0)
            sentences = [
                SummarySentence(text=a, covenant_ids=[]) if isinstance(a, str)
                else SummarySentence(text=a[0], covenant_ids=list(a[1]))
                for a in answer
            ]
            return Summary(sentences=sentences), meta  # type: ignore[return-value]

        raise AssertionError(f"Unexpected purpose: {purpose}")
