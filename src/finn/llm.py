"""OpenRouter client wrapper for structured JSON schema outputs with retry and fallback."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Protocol, TypeVar, runtime_checkable

from openai import OpenAI
from pydantic import BaseModel, ValidationError

from finn.config import Settings
from finn.models import build_llm_json_schema

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


class LLMError(Exception):
    """Raised when the LLM fails to return valid structured JSON after retries and fallback."""


@runtime_checkable
class JsonLLM(Protocol):
    """Protocol for structured JSON completion clients (real or fake in tests)."""

    model_name: str

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        purpose: str,
    ) -> tuple[T, dict[str, Any]]:
        """Complete a prompt and return a validated Pydantic model instance and metadata dict."""
        ...


def strip_markdown_fences(text: str) -> str:
    """Strip leading/trailing ```json ... ``` markdown code fences if present."""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    if m:
        return m.group(1).strip()
    return stripped


class LLMClient:
    """OpenRouter client using the OpenAI SDK with structured outputs, 1 retry, and model fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Callable[[str, list[dict[str, str]], dict[str, Any]], tuple[str, int, int]]
        | None = None,
    ) -> None:
        if transport is None:
            settings.require_llm()
        self._settings = settings
        self.model_name: str = settings.openrouter_model or "unconfigured-model"
        self.fallback_model_name: str = settings.openrouter_model_fallback
        self._transport = transport
        self._client: OpenAI | None = None
        if transport is None:
            self._client = OpenAI(
                base_url="https://openrouter.ai/api/v1",
                api_key=settings.openrouter_api_key,
                timeout=90.0,
                default_headers={
                    "HTTP-Referer": "https://stadtwerke-musterstadt.de",
                    "X-Title": "Finn Covenant Agent",
                },
            )

    def _call_provider(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        schema_name: str,
    ) -> tuple[str, int, int]:
        if self._transport is not None:
            return self._transport(model, messages, json_schema)

        assert self._client is not None
        response = self._client.chat.completions.create(
            model=model,
            temperature=0,
            messages=messages,  # type: ignore[arg-type]
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": False,
                },
            },
            extra_body={"provider": {"require_parameters": True}},
            timeout=90.0,
        )
        content = response.choices[0].message.content or ""
        usage = response.usage
        p_tok = int(usage.prompt_tokens) if usage and usage.prompt_tokens else 0
        c_tok = int(usage.completion_tokens) if usage and usage.completion_tokens else 0
        return content, p_tok, c_tok

    def complete_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        purpose: str,
    ) -> tuple[T, dict[str, Any]]:
        """Execute structured JSON completion with up to 2 attempts per model (primary + optional fallback)."""
        clean_schema = build_llm_json_schema(schema)
        models_to_try = [self.model_name]
        if self.fallback_model_name and self.fallback_model_name != self.model_name:
            models_to_try.append(self.fallback_model_name)

        t0 = time.monotonic()
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_attempts = 0
        last_error: str = "Unknown error"

        for model in models_to_try:
            messages: list[dict[str, str]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            for _attempt in range(2):
                total_attempts += 1
                raw_content = ""
                try:
                    raw_content, p_tok, c_tok = self._call_provider(
                        model, messages, clean_schema, schema.__name__
                    )
                    total_prompt_tokens += p_tok
                    total_completion_tokens += c_tok
                    cleaned = strip_markdown_fences(raw_content)
                    parsed_json = json.loads(cleaned)
                    validated = schema.model_validate(parsed_json)
                    latency_s = round(time.monotonic() - t0, 3)
                    meta: dict[str, Any] = {
                        "purpose": purpose,
                        "model": model,
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "latency_s": latency_s,
                        "attempts": total_attempts,
                    }
                    return validated, meta
                except (json.JSONDecodeError, ValidationError, ValueError) as val_exc:
                    last_error = f"{type(val_exc).__name__}: {val_exc}"
                    if raw_content:
                        messages.append({"role": "assistant", "content": raw_content})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"Your previous answer was invalid: {last_error}. "
                                "Return only valid JSON for the schema."
                            ),
                        }
                    )
                except Exception as net_exc:
                    last_error = f"{type(net_exc).__name__}: {net_exc}"

        raise LLMError(
            f"LLM structured completion failed for '{purpose}' after {total_attempts} attempts: {last_error}"
        )
