"""LLM-assisted mapping of contract quantity components to Excel PlanItem cells with validation and cache."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from finn.llm import JsonLLM
from finn.models import Component, ContractCovenants, Quantity, TermMapping
from finn.plan.reader import PlanData, plan_item_catalog

DEFAULT_MAP_PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "prompts" / "map_terms.txt"
)


class MappingError(Exception):
    """Raised when LLM term mappings fail deterministic validation even after retry."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors: list[str] = errors or []


class MappingItem(BaseModel):
    """Single component mapping item returned by the LLM."""

    quantity: str
    component: str
    item_ids: list[str]
    rationale: str


class MappingResponse(BaseModel):
    """Structured output schema for term mapping."""

    mappings: list[MappingItem]


_EMPTY_RATIONALE_KEYWORDS = (
    "nicht",
    "kein",
    "keine",
    "keinen",
    "keiner",
    "entfällt",
    "fehlt",
    "weder",
    "ohne",
    "null",
    "0",
    "no ",
    "not ",
    "none",
)


def _leaf_components(contract: ContractCovenants) -> list[tuple[Quantity, Component]]:
    leaves: list[tuple[Quantity, Component]] = []
    for qty in contract.quantities:
        for comp in qty.components:
            if comp.ref is None:
                leaves.append((qty, comp))
    return leaves


def _contract_canonical_json(contract: ContractCovenants) -> str:
    """Serialize contract covenant/quantity structure (excluding verification flags) for cache key stability."""
    data = contract.model_dump(mode="json")

    def _strip_verify(obj: Any) -> None:
        if isinstance(obj, dict):
            obj.pop("verified", None)
            obj.pop("verify_note", None)
            for v in obj.values():
                _strip_verify(v)
        elif isinstance(obj, list):
            for x in obj:
                _strip_verify(x)

    _strip_verify(data)
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


def _compute_mapping_cache_key(
    contract: ContractCovenants,
    catalog_no_values: str,
    prompt_bytes: bytes,
    model_name: str,
) -> str:
    c_hash = hashlib.sha256(_contract_canonical_json(contract).encode("utf-8")).hexdigest()
    cat_hash = hashlib.sha256(catalog_no_values.encode("utf-8")).hexdigest()
    p_hash = hashlib.sha256(prompt_bytes).hexdigest()
    combined = f"{c_hash}:{cat_hash}:{p_hash}:{model_name}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _build_mapping_user_message(
    contract: ContractCovenants,
    leaves: list[tuple[Quantity, Component]],
    catalog_no_values: str,
) -> str:
    lines: list[str] = [
        f"Vertrag: {contract.contract_label} ({contract.contract_file})",
        "",
    ]
    if contract.covenants:
        lines.append("Ergänzende Vertragsregeln / Definitionshinweise:")
        for cov in contract.covenants:
            lines.append(f"- Covenant {cov.name} ({cov.id}, test_basis={cov.test_basis}): {cov.source.quote}")
            for ds in cov.definition_sources:
                lines.append(f"  * [{ds.section}] {ds.quote}")
        lines.append("")

    lines.append("Zu mappende Komponenten (Leaf Components):")
    for idx, (qty, comp) in enumerate(leaves, start=1):
        lines.append(
            f"{idx}. quantity=\"{qty.name}\" | component=\"{comp.name}\" | "
            f"sign={comp.sign:+d} | weight={comp.weight}\n"
            f"   Komponenten-Definition: {comp.definition}\n"
            f"   Größen-Zitat ({qty.source.section}): {qty.source.quote}"
        )

    lines.append("")
    lines.append("Wirtschaftsplan-Katalog 2027 (ohne Zahlenwerte):")
    lines.append(catalog_no_values)
    return "\n".join(lines)


def validate_mappings(
    contract: ContractCovenants,
    items: list[MappingItem],
    plan: PlanData,
) -> list[str]:
    """Validate LLM-returned MappingItems against rules (a)-(e); return all error strings."""
    errors: list[str] = []
    leaves = _leaf_components(contract)
    expected_keys = [(q.name, c.name) for q, c in leaves]
    expected_set = set(expected_keys)

    seen_counts: dict[tuple[str, str], int] = {}
    for m in items:
        key = (m.quantity, m.component)
        seen_counts[key] = seen_counts.get(key, 0) + 1
        if key not in expected_set:
            errors.append(
                f"Unbekannte oder zusätzliche Komponente: quantity='{m.quantity}', component='{m.component}'."
            )

    for q_name, c_name in expected_keys:
        cnt = seen_counts.get((q_name, c_name), 0)
        if cnt == 0:
            errors.append(
                f"Fehlende Zuordnung für Komponente: quantity='{q_name}', component='{c_name}'."
            )
        elif cnt > 1:
            errors.append(
                f"Doppelte Zuordnung ({cnt}x) für Komponente: quantity='{q_name}', component='{c_name}'."
            )

    for m in items:
        prefix = f"[{m.quantity} / {m.component}]"
        seen_ids: set[str] = set()
        valid_ids: list[str] = []

        for item_id in m.item_ids:
            if item_id in seen_ids:
                errors.append(
                    f"{prefix}: Zell-ID '{item_id}' wird innerhalb derselben Komponente mehrfach verwendet."
                )
            seen_ids.add(item_id)

            if item_id not in plan.items:
                errors.append(
                    f"{prefix}: Unbekannte Zell-ID '{item_id}' (nicht im Wirtschaftsplan-Katalog vorhanden)."
                )
            else:
                valid_ids.append(item_id)

        # d) Double counting check: total + any item in its transitive depends_on
        valid_id_set = set(valid_ids)
        for item_id in valid_ids:
            p_item = plan.items[item_id]
            if p_item.is_total:
                overlap = [dep for dep in p_item.depends_on if dep in valid_id_set]
                for dep_id in overlap:
                    errors.append(
                        f"{prefix}: Doppelzählung (double counting) – Summenzelle '{item_id}' "
                        f"darf nicht zusammen mit ihrer Teilzelle '{dep_id}' gewählt werden."
                    )

        # e) Empty item_ids only allowed if rationale explains the plan has no such line
        if len(m.item_ids) == 0:
            rat_low = m.rationale.strip().lower()
            if not rat_low or not any(kw in rat_low for kw in _EMPTY_RATIONALE_KEYWORDS):
                errors.append(
                    f"{prefix}: Leere item_ids sind nur zulässig, wenn rationale begründet, "
                    f"dass diese Position im Wirtschaftsplan nicht vorhanden ist."
                )

    return errors


def _mapping_warnings(
    contract: ContractCovenants,
    mappings: list[TermMapping],
    plan: PlanData,
) -> list[str]:
    """Generate non-fatal semantic warnings for term mappings."""
    warnings: list[str] = []
    qty_map = {q.name: q for q in contract.quantities}

    # 1. Same cell used with opposite signs within one quantity
    for qty in contract.quantities:
        cell_signs: dict[str, set[int]] = {}
        for comp in qty.components:
            if comp.ref is not None:
                continue
            m = next(
                (
                    x
                    for x in mappings
                    if x.quantity == qty.name and x.component == comp.name
                ),
                None,
            )
            if m is None:
                continue
            for cid in m.item_ids:
                cell_signs.setdefault(cid, set()).add(comp.sign)
        for cid, signs in cell_signs.items():
            if len(signs) > 1:
                warnings.append(
                    f"Größe '{qty.name}': Zelle '{cid}' wird innerhalb derselben Größe mit "
                    f"gegensätzlichen Vorzeichen (+1 und -1) verwendet."
                )

    # 2. Sheet vs. covenant test_basis mismatch (e.g., GuV cell in stichtag covenant)
    for cov in contract.covenants:
        if cov.test_basis == "stichtag":
            for q_name in (cov.numerator, cov.denominator):
                qty = qty_map.get(q_name)
                if qty is None:
                    continue
                for comp in qty.components:
                    if comp.ref is not None:
                        continue
                    m = next(
                        (
                            x
                            for x in mappings
                            if x.quantity == qty.name and x.component == comp.name
                        ),
                        None,
                    )
                    if m is None:
                        continue
                    for cid in m.item_ids:
                        if cid in plan.items and plan.items[cid].sheet == "GuV":
                            warnings.append(
                                f"Covenant '{cov.id}' (Stichtag): Komponente '{comp.name}' verwendet "
                                f"GuV-Zelle '{cid}'."
                            )

    return warnings


def map_terms(
    contract: ContractCovenants,
    plan: PlanData,
    llm: JsonLLM,
    cache_dir: Path,
    prompt_path: Path | None = None,
    *,
    use_cache: bool = True,
) -> tuple[list[TermMapping], dict[str, Any]]:
    """Map all leaf components of a contract to PlanItem IDs using the LLM, with validation and caching."""
    leaves = _leaf_components(contract)
    if not contract.has_covenants or not leaves:
        return [], {
            "cache_hit": False,
            "skipped": True,
            "warnings": [],
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "latency_s": 0.0,
            "attempts": 0,
        }

    cache_dir = Path(cache_dir)
    actual_prompt_path = (
        Path(prompt_path) if prompt_path is not None else DEFAULT_MAP_PROMPT_PATH
    )
    prompt_bytes = actual_prompt_path.read_bytes()
    prompt_text = prompt_bytes.decode("utf-8")

    catalog_no_values = plan_item_catalog(plan, include_values=False)
    model_name = getattr(llm, "model_name", "unknown-model")
    cache_key = _compute_mapping_cache_key(
        contract, catalog_no_values, prompt_bytes, model_name
    )
    cache_file = cache_dir / f"mapping_{cache_key[:16]}.json"

    if use_cache and cache_file.exists():
        cached_payload = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_mappings = [
            TermMapping.model_validate(item) for item in cached_payload["mappings"]
        ]
        cached_meta = dict(cached_payload.get("meta", {}))
        cached_meta["cache_hit"] = True
        cached_meta["cache_file"] = str(cache_file)
        return cached_mappings, cached_meta

    base_user_msg = _build_mapping_user_message(contract, leaves, catalog_no_values)

    # Attempt 1
    resp1, llm_meta1 = llm.complete_json(
        system=prompt_text,
        user=base_user_msg,
        schema=MappingResponse,
        purpose=f"map_terms:{contract.contract_file}",
    )
    val_errors = validate_mappings(contract, resp1.mappings, plan)
    total_p_tok = llm_meta1.get("prompt_tokens", 0)
    total_c_tok = llm_meta1.get("completion_tokens", 0)
    total_lat = llm_meta1.get("latency_s", 0.0)
    total_attempts = llm_meta1.get("attempts", 1)
    final_model = llm_meta1.get("model", model_name)
    final_items = resp1.mappings

    # On validation errors -> ONE retry call with error feedback
    if val_errors:
        err_bullet_list = "\n".join(f"- {e}" for e in val_errors)
        retry_user_msg = (
            f"{base_user_msg}\n\n"
            f"Your previous mapping response was:\n{resp1.model_dump_json(indent=2)}\n\n"
            f"Fix these problems:\n{err_bullet_list}\n"
            "Return only valid JSON matching MappingResponse."
        )
        resp2, llm_meta2 = llm.complete_json(
            system=prompt_text,
            user=retry_user_msg,
            schema=MappingResponse,
            purpose=f"map_terms_retry:{contract.contract_file}",
        )
        total_p_tok += llm_meta2.get("prompt_tokens", 0)
        total_c_tok += llm_meta2.get("completion_tokens", 0)
        total_lat = round(total_lat + llm_meta2.get("latency_s", 0.0), 3)
        total_attempts += llm_meta2.get("attempts", 1)
        final_model = llm_meta2.get("model", final_model)
        val_errors = validate_mappings(contract, resp2.mappings, plan)
        if val_errors:
            raise MappingError(
                f"Mapping validation failed for {contract.contract_file}: {'; '.join(val_errors)}",
                errors=val_errors,
            )
        final_items = resp2.mappings

    # Preserve canonical leaf component order and attach contract_file
    by_key = {(m.quantity, m.component): m for m in final_items}
    term_mappings: list[TermMapping] = []
    for qty, comp in leaves:
        item = by_key[(qty.name, comp.name)]
        term_mappings.append(
            TermMapping(
                contract_file=contract.contract_file,
                quantity=item.quantity,
                component=item.component,
                item_ids=list(item.item_ids),
                rationale=item.rationale,
            )
        )

    warnings = _mapping_warnings(contract, term_mappings, plan)
    meta: dict[str, Any] = {
        "purpose": f"map_terms:{contract.contract_file}",
        "model": final_model,
        "prompt_tokens": total_p_tok,
        "completion_tokens": total_c_tok,
        "latency_s": total_lat,
        "attempts": total_attempts,
        "cache_hit": False,
        "cache_key": cache_key,
        "cache_file": str(cache_file),
        "warnings": warnings,
    }

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "mappings": [m.model_dump(mode="json") for m in term_mappings],
        "meta": meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_file.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return term_mappings, meta
