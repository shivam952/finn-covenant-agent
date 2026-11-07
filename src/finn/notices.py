"""Notices: user-facing remarks as (key, params), rendered in the reply language at the very end.

Code that detects something (a corrected page number, an empty plan line, a plan the reader
cannot use) records a key and its parameters, never a sentence. The wording lives in
src/finn/texts/<lang>.toml under [notices], so an English reply never contains German remarks.
"""

from __future__ import annotations

from typing import Any

from finn.render import load_texts


def notice(key: str, **params: Any) -> dict[str, Any]:
    """A language-neutral remark."""
    return {"key": key, "params": params}


def render_notice(item: dict[str, Any] | str, lang: str) -> str:
    """Render a notice in `lang`. Plain strings (e.g. from an older cache) pass through unchanged."""
    if isinstance(item, str):
        return item
    template = load_texts(lang)["notices"].get(item.get("key", ""))
    if template is None:
        return f"{item.get('key')}: {item.get('params')}"
    try:
        return str(template).format(**item.get("params", {}))
    except (KeyError, IndexError):
        return f"{template} {item.get('params')}"
