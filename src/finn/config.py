"""Configuration loading from environment variables and .env file."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict


class Settings(BaseModel):
    """Application settings loaded from environment variables."""

    model_config = ConfigDict(frozen=True)

    openrouter_api_key: str = ""
    openrouter_model: str = ""
    openrouter_model_fallback: str = ""
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
    def from_env(cls, env_file: str | Path | None = None) -> Settings:
        """Load .env (if present) and construct frozen Settings without crashing on missing keys."""
        load_dotenv(dotenv_path=env_file, override=False)
        return cls(
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "").strip(),
            openrouter_model_fallback=os.getenv("OPENROUTER_MODEL_FALLBACK", "").strip(),
            finn_mail_address=os.getenv("FINN_MAIL_ADDRESS", "").strip(),
            imap_host=os.getenv("IMAP_HOST", "imap.gmail.com").strip() or "imap.gmail.com",
            imap_port=int(os.getenv("IMAP_PORT", "993") or 993),
            smtp_host=os.getenv("SMTP_HOST", "smtp.gmail.com").strip() or "smtp.gmail.com",
            smtp_port=int(os.getenv("SMTP_PORT", "465") or 465),
            mail_user=os.getenv("MAIL_USER", "").strip(),
            mail_app_password=os.getenv("MAIL_APP_PASSWORD", "").strip(),
            poll_seconds=int(os.getenv("POLL_SECONDS", "20") or 20),
            contracts_dir=Path(os.getenv("CONTRACTS_DIR", "data/contracts") or "data/contracts"),
            cache_dir=Path(os.getenv("CACHE_DIR", "cache") or "cache"),
            runs_dir=Path(os.getenv("RUNS_DIR", "runs") or "runs"),
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
        if missing:
            raise ValueError(
                f"Missing required mail environment variables: {', '.join(missing)}"
            )


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Convenience function to load application settings."""
    return Settings.from_env(env_file=env_file)

