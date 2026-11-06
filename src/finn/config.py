"""Configuration loading from environment variables and .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


def _clean_env(name: str, default: str = "") -> str:
    """Read an environment variable, strip whitespace, and treat values starting with '#' as empty."""
    raw = os.getenv(name, "").strip()
    if raw.startswith("#"):
        return default
    return raw or default


class Settings(BaseModel):
    """Application settings loaded from environment variables."""

    model_config = ConfigDict(frozen=True)

    openrouter_api_key: str = ""
    openrouter_model: str = ""
    openrouter_model_fallback: str = ""
    app_url: str = ""
    llm_time_budget_s: float = 180.0
    llm_max_tokens: int = 12000
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

        mail_user = _clean_env("MAIL_USER")
        finn_mail_address = _clean_env("FINN_MAIL_ADDRESS") or mail_user

        return cls(
            openrouter_api_key=_clean_env("OPENROUTER_API_KEY"),
            openrouter_model=_clean_env("OPENROUTER_MODEL"),
            openrouter_model_fallback=_clean_env("OPENROUTER_MODEL_FALLBACK"),
            app_url=_clean_env("APP_URL"),
            llm_time_budget_s=float(_clean_env("LLM_TIME_BUDGET_S", "180")),
            llm_max_tokens=int(_clean_env("LLM_MAX_TOKENS", "12000")),
            finn_mail_address=finn_mail_address,
            imap_host=_clean_env("IMAP_HOST", "imap.gmail.com"),
            imap_port=int(_clean_env("IMAP_PORT", "993")),
            smtp_host=_clean_env("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=int(_clean_env("SMTP_PORT", "465")),
            mail_user=mail_user,
            mail_app_password=_clean_env("MAIL_APP_PASSWORD"),
            poll_seconds=int(_clean_env("POLL_SECONDS", "20")),
            contracts_dir=Path(_clean_env("CONTRACTS_DIR", "data/contracts")),
            cache_dir=Path(_clean_env("CACHE_DIR", "cache")),
            runs_dir=Path(_clean_env("RUNS_DIR", "runs")),
        )

    def require_llm(self) -> None:
        """Raise ValueError if required OpenRouter LLM configuration is missing."""
        missing: list[str] = []
        if not self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if not self.openrouter_model:
            missing.append("OPENROUTER_MODEL")
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
