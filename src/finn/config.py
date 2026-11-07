"""Configuration loading from environment variables and .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_PROVIDER_PARAMS_JSON = '{"require_parameters": true}'


def project_path(value: str | Path) -> Path:
    """Relative paths are resolved against the project root, so Finn works from any working directory."""
    p = Path(value)
    return p if p.is_absolute() else (PROJECT_ROOT / p)


def _clean_env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, strip whitespace, and treat values starting with '#' as empty."""
    if name not in os.environ:
        return default
    raw = os.environ[name].strip()
    if raw.startswith("#"):
        return default if default is not None else ""
    return raw


class Settings(BaseModel):
    """Application settings loaded from environment variables."""

    model_config = ConfigDict(frozen=True)

    llm_base_url: str = DEFAULT_LLM_BASE_URL
    llm_api_key: str = ""
    llm_model: str = ""
    llm_model_fallback: str = ""
    llm_provider_params_json: str = DEFAULT_PROVIDER_PARAMS_JSON
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    openrouter_model_fallback: str = ""
    app_url: str = ""
    llm_time_budget_s: float = 180.0
    llm_max_tokens: int = 24000
    reply_language: str = "de"
    plan_layout_path: Path = Path("config/plan_layout.toml")
    finn_mail_address: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 465
    mail_user: str = ""
    mail_app_password: str = ""
    poll_seconds: int = 20
    contracts_dir: Path = Path("data/contracts")
    cache_dir: Path = Path("cache")
    runs_dir: Path = Path("runs")

    @model_validator(mode="before")
    @classmethod
    def _sync_llm_aliases(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        out = dict(data)
        api_key = out.get("llm_api_key") or out.get("openrouter_api_key") or ""
        model = out.get("llm_model") or out.get("openrouter_model") or ""
        fallback = (
            out.get("llm_model_fallback") or out.get("openrouter_model_fallback") or ""
        )
        out["llm_api_key"] = api_key
        out["openrouter_api_key"] = api_key
        out["llm_model"] = model
        out["openrouter_model"] = model
        out["llm_model_fallback"] = fallback
        out["openrouter_model_fallback"] = fallback
        return out

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = None,
        *,
        load_env_file: bool = True,
    ) -> Settings:
        """Load .env (if requested) and construct frozen Settings without crashing on missing keys."""
        if load_env_file:
            load_dotenv(dotenv_path=env_file, override=False)

        api_key = _clean_env("LLM_API_KEY") or _clean_env("OPENROUTER_API_KEY") or ""
        model = _clean_env("LLM_MODEL") or _clean_env("OPENROUTER_MODEL") or ""
        fallback = (
            _clean_env("LLM_MODEL_FALLBACK")
            or _clean_env("OPENROUTER_MODEL_FALLBACK")
            or ""
        )
        base_url = _clean_env("LLM_BASE_URL") or DEFAULT_LLM_BASE_URL
        provider_params_raw = _clean_env("LLM_PROVIDER_PARAMS_JSON")
        provider_params_json = (
            DEFAULT_PROVIDER_PARAMS_JSON
            if provider_params_raw is None
            else provider_params_raw
        )

        mail_user = _clean_env("MAIL_USER") or ""
        finn_mail_address = _clean_env("FINN_MAIL_ADDRESS") or mail_user

        return cls(
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            llm_model_fallback=fallback,
            llm_provider_params_json=provider_params_json,
            app_url=_clean_env("APP_URL") or "",
            llm_time_budget_s=float(_clean_env("LLM_TIME_BUDGET_S") or "180"),
            llm_max_tokens=int(_clean_env("LLM_MAX_TOKENS") or "24000"),
            reply_language=_clean_env("REPLY_LANGUAGE") or "de",
            plan_layout_path=project_path(
                _clean_env("PLAN_LAYOUT") or "config/plan_layout.toml"
            ),
            finn_mail_address=finn_mail_address,
            imap_host=_clean_env("IMAP_HOST") or "imap.gmail.com",
            imap_port=int(_clean_env("IMAP_PORT") or "993"),
            smtp_host=_clean_env("SMTP_HOST") or "smtp.gmail.com",
            smtp_port=int(_clean_env("SMTP_PORT") or "465"),
            mail_user=mail_user,
            mail_app_password=_clean_env("MAIL_APP_PASSWORD") or "",
            poll_seconds=int(_clean_env("POLL_SECONDS") or "20"),
            contracts_dir=project_path(_clean_env("CONTRACTS_DIR") or "data/contracts"),
            cache_dir=project_path(_clean_env("CACHE_DIR") or "cache"),
            runs_dir=project_path(_clean_env("RUNS_DIR") or "runs"),
        )

    def require_llm(self) -> None:
        """Raise ValueError if required LLM configuration is missing."""
        missing: list[str] = []
        if not self.llm_api_key:
            missing.append("LLM_API_KEY (or OPENROUTER_API_KEY)")
        if not self.llm_model:
            missing.append("LLM_MODEL (or OPENROUTER_MODEL)")
        if missing:
            raise ValueError(
                f"Missing required LLM environment variables: {', '.join(missing)}"
            )

    def require_mail(self) -> None:
        """Raise ValueError if required IMAP/SMTP mail configuration is missing."""
        missing: list[str] = []
        if not self.imap_host:
            missing.append("IMAP_HOST")
        if not self.smtp_host:
            missing.append("SMTP_HOST")
        if not self.mail_user:
            missing.append("MAIL_USER")
        if not self.mail_app_password:
            missing.append("MAIL_APP_PASSWORD")
        if not self.finn_mail_address:
            missing.append("FINN_MAIL_ADDRESS")
        if missing:
            raise ValueError(
                f"Missing required mail environment variables: {', '.join(missing)}"
            )


def load_settings(
    env_file: str | Path | None = None,
    *,
    load_env_file: bool = True,
) -> Settings:
    """Convenience function to load application settings."""
    return Settings.from_env(env_file=env_file, load_env_file=load_env_file)
