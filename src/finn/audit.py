"""Audit trail persistence for each covenant verification run (with secret stripping)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from finn.config import Settings

_SECRET_SUBSTRINGS = ("key", "password", "token")


def redact_settings(settings: Settings | dict[str, Any]) -> dict[str, Any]:
    """Dump settings to a JSON-compatible dict, stripping any key containing 'key', 'password', or 'token'."""
    raw = (
        settings.model_dump(mode="json")
        if isinstance(settings, Settings)
        else dict(settings)
    )
    return {
        k: v
        for k, v in raw.items()
        if not any(sub in k.lower() for sub in _SECRET_SUBSTRINGS)
    }


def write_audit(audit: dict[str, Any], runs_dir: Path) -> Path:
    """Write the audit dictionary to runs/<run_id>.json (UTF-8, indent 2) after stripping settings secrets."""
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(audit.get("run_id") or "run")

    safe_audit = dict(audit)
    if "settings" in safe_audit and isinstance(safe_audit["settings"], (Settings, dict)):
        safe_audit["settings"] = redact_settings(safe_audit["settings"])

    audit_path = runs_dir / f"{run_id}.json"
    audit_path.write_text(
        json.dumps(safe_audit, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return audit_path
