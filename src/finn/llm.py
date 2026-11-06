"""OpenAI-compatible client wrapper for structured JSON schema outputs with bounded latency, retry, and fallback."""

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
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 402, 403, 404})


class LLMError(Exception):
    """Raised when the LLM fails to return valid structured JSON after retries, fallback, or time budget."""


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


TransportReturn = tuple[str, int, int] | tuple[str, int, int, str | None]


class LLMClient:
    """OpenAI-compatible client with structured outputs, 1 retry, and model fallback."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: Callable[[str, list[dict[str, str]], dict[str, Any]], TransportReturn]
        | None = None,
        clock_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        if transport is None:
            settings.require_llm()
        self._settings = settings
        self.base_url: str = settings.llm_base_url
        self.model_name: str = settings.llm_model or "unconfigured-model"
        self.fallback_model_name: str = settings.llm_model_fallback
        self.time_budget_s: float = settings.llm_time_budget_s
        self.max_tokens: int = settings.llm_max_tokens
        self.provider_params: dict[str, Any] | None = None
        raw_params = settings.llm_provider_params_json.strip()
        if raw_params:
            parsed_params = json.loads(raw_params)
            if isinstance(parsed_params, dict) and parsed_params:
                self.provider_params = parsed_params

        self._transport = transport
        self._clock_fn = clock_fn
        self._sleep_fn = sleep_fn
        self._client: OpenAI | None = None
        if transport is None:
            headers: dict[str, str] = {"X-Title": "Finn Covenant Agent"}
            if settings.app_url:
                headers["HTTP-Referer"] = settings.app_url
            self._client = OpenAI(
                base_url=self.base_url,
                api_key=settings.llm_api_key,
                max_retries=0,
                timeout=60.0,
                default_headers=headers,
            )

    def _call_provider(
        self,
        model: str,
        messages: list[dict[str, str]],
        json_schema: dict[str, Any],
        schema_name: str,
    ) -> tuple[str, int, int, str | None]:
        if self._transport is not None:
            res = self._transport(model, messages, json_schema)
            if len(res) == 4:
                return res[0], res[1], res[2], res[3]
            return res[0], res[1], res[2], "stop"

        assert self._client is not None
        kwargs: dict[str, Any] = {
            "model": model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "schema": json_schema,
                    "strict": False,
                },
            },
            "timeout": 60.0,
        }
        if self.provider_params is not None:
            kwargs["extra_body"] = {"provider": self.provider_params}

        response = self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        content = choice.message.content or ""
        finish_reason = choice.finish_reason
        usage = response.usage
        p_tok = int(usage.prompt_tokens) if usage and usage.prompt_tokens else 0
        c_tok = int(usage.completion_tokens) if usage and usage.completion_tokens else 0
        return content, p_tok, c_tok, finish_reason

    def _check_budget(self, t0: float, purpose: str, total_attempts: int) -> None:
        elapsed = self._clock_fn() - t0
        if elapsed > self.time_budget_s:
            raise LLMError(
                f"LLM time budget ({self.time_budget_s}s) exceeded for '{purpose}' "
                f"after {elapsed:.1f}s and {total_attempts} attempts."
            )

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

        t0 = self._clock_fn()
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_attempts = 0
        attempt_log: list[dict[str, Any]] = []
        last_error: str = "Unknown error"

        for model in models_to_try:
            messages: list[dict[str, str]] = [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
            for attempt_idx in range(2):
                self._check_budget(t0, purpose, total_attempts)
                total_attempts += 1
                raw_content = ""
                try:
                    raw_content, p_tok, c_tok, finish_reason = self._call_provider(
                        model, messages, clean_schema, schema.__name__
                    )
                    total_prompt_tokens += p_tok
                    total_completion_tokens += c_tok
                    self._check_budget(t0, purpose, total_attempts)

                    if finish_reason == "length":
                        raise ValueError("Antwort abgeschnitten (max_tokens)")
                    if not raw_content or not raw_content.strip():
                        raise ValueError("Leere Antwort oder Verweigerung vom Modell")

                    cleaned = strip_markdown_fences(raw_content)
                    parsed_json = json.loads(cleaned)
                    validated = schema.model_validate(parsed_json)
                    attempt_log.append(
                        {"attempt": total_attempts, "model": model, "status": "ok"}
                    )
                    latency_s = round(self._clock_fn() - t0, 3)
                    meta: dict[str, Any] = {
                        "purpose": purpose,
                        "model": model,
                        "prompt_tokens": total_prompt_tokens,
                        "completion_tokens": total_completion_tokens,
                        "latency_s": latency_s,
                        "attempts": total_attempts,
                        "attempt_log": attempt_log,
                    }
                    return validated, meta
                except LLMError:
                    raise
                except (json.JSONDecodeError, ValidationError, ValueError) as val_exc:
                    last_error = f"{type(val_exc).__name__}: {val_exc}"
                    attempt_log.append(
                        {
                            "attempt": total_attempts,
                            "model": model,
                            "status": "error",
                            "error": last_error,
                        }
                    )
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
                    status_code = getattr(net_exc, "status_code", None)
                    last_error = (
                        f"HTTP {status_code}: {net_exc}"
                        if status_code is not None
                        else f"{type(net_exc).__name__}: {net_exc}"
                    )
                    attempt_log.append(
                        {
                            "attempt": total_attempts,
                            "model": model,
                            "status": "error",
                            "status_code": status_code,
                            "error": last_error,
                        }
                    )
                    if status_code in _NON_RETRYABLE_STATUS_CODES:
                        # Do not retry on the same model for 400/401/402/403/404
                        break
                    if attempt_idx == 0:
                        self._check_budget(t0, purpose, total_attempts)
                        self._sleep_fn(2.0)

        raise LLMError(
            f"LLM structured completion failed for '{purpose}' after {total_attempts} attempts: {last_error}"
        )


def make_llm(settings: Settings) -> JsonLLM:
    """Factory function to create an LLMClient (can be monkeypatched in tests)."""
    return LLMClient(settings)
