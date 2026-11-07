"""LLM-assisted mapping of contract quantity components to Excel PlanItem cells with validation and cache."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from finn.llm import JsonLLM
from finn.notices import notice
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


def _leaf_components(contract: ContractCovenants) -> list[tuple[Quantity, Component]]:
    leaves: list[tuple[Quantity, Component]] = []
    for qty in contract.quantities:
        for comp in qty.components:
            if comp.ref is None:
                leaves.append((qty, comp))
    return leaves


def _contract_canonical_json(contract: ContractCovenants) -> str:
    """Serialize contract covenant/quantity structure (excluding verification flags and kind) for cache key stability."""
    data = contract.model_dump(mode="json")

    def _strip_verify(obj: Any) -> None:
        if isinstance(obj, dict):
            obj.pop("verified", None)
            obj.pop("verify_note", None)
            obj.pop("kind", None)
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
    # Line endings are normalised so a Windows checkout (CRLF) still hits the committed cache.
    p_hash = hashlib.sha256(prompt_bytes.replace(b"\r\n", b"\n")).hexdigest()
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
            f"sign={comp.sign:+d} | weight={comp.weight}"
            f"{' | carve_out=true' if comp.carve_out else ''}\n"
            f"   Komponenten-Definition: {comp.definition}\n"
            f"   Größen-Zitat ({qty.source.section}): {qty.source.quote}"
        )

    lines.append("")
    lines.append("Wirtschaftsplan-Katalog (ohne Zahlenwerte):")
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

        # e) An empty mapping needs a reason (the reason itself is shown to the reader, not parsed).
        if len(m.item_ids) == 0 and not m.rationale.strip():
            errors.append(f"{prefix}: Leere Zuordnung ohne Begründung.")

    # f) Structural rule: a quantity whose leaf components are ALL unmapped would silently become 0
    #    (a zero denominator or a missing numerator). That is never a valid reading of a plan.
    by_qty: dict[str, list[MappingItem]] = {}
    for m in items:
        by_qty.setdefault(m.quantity, []).append(m)
    leaf_qtys = {q.name for q, _ in _leaf_components(contract)}
    for qty_name in sorted(leaf_qtys):
        ms = by_qty.get(qty_name, [])
        if ms and all(len(m.item_ids) == 0 for m in ms):
            errors.append(
                f"{qty_name}: Keine einzige Planzelle zugeordnet – die Größe wäre 0."
            )

    return errors


def _mapping_warnings(
    contract: ContractCovenants,
    mappings: list[TermMapping],
    plan: PlanData,
    skip: set[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Non-fatal remarks on a mapping, as language-neutral notices."""
    warnings: list[dict[str, Any]] = []
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
                warnings.append(notice("opposite_signs", cell=cid, quantity=qty.name))

    # 2. Sheet role vs. covenant test_basis mismatch (income_statement cell in stichtag covenant)
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
                    flow_roles = set(plan.meta.get("flow_roles", []))
                    for cid in m.item_ids:
                        if cid in plan.items and plan.items[cid].role in flow_roles:
                            warnings.append(notice("flow_cell_in_stichtag_covenant", covenant=cov.name,
                                                   component=comp.name, cell=cid))

    # 3. Every component set to "no plan line" is shown to the reader: one with its reason, several
    #    as one list (their reasons stay in the audit file). Carve-outs emptied by code already
    #    have their own notice.
    empty = [m for m in mappings if not m.item_ids and (m.quantity, m.component) not in (skip or set())]
    if len(empty) == 1:
        m = empty[0]
        warnings.append(notice("empty_plan_line", component=m.component, quantity=m.quantity,
                               rationale=m.rationale.strip()))
    elif empty:
        warnings.append(notice("empty_plan_lines",
                               items=", ".join(f"„{m.component}“ ({m.quantity})" for m in empty)))

    return warnings


def _contained_cells(cell_ids: set[str], plan: PlanData) -> set[str]:
    """Cells whose amount is part of the sum of `cell_ids`: the cells, the inputs of chosen totals,
    and "davon" sub-lines of any of those."""
    out = set(cell_ids)
    for cid in cell_ids:
        item = plan.items.get(cid)
        if item is not None:
            out.update(item.depends_on)
    changed = True
    while changed:
        changed = False
        for iid, item in plan.items.items():
            if iid not in out and item.part_of is not None and item.part_of in out:
                out.add(iid)
                changed = True
    return out


def _cells_with_sign(
    qty_name: str,
    want_sign: int,
    contract: ContractCovenants,
    by_key: dict[tuple[str, str], TermMapping],
    sign_mult: int = 1,
    visiting: frozenset[str] = frozenset(),
) -> set[str]:
    """All mapped cells that enter `qty_name` with the effective sign `want_sign` (refs followed)."""
    qty = next((q for q in contract.quantities if q.name == qty_name), None)
    if qty is None or qty_name in visiting:
        return set()
    cells: set[str] = set()
    for comp in qty.components:
        eff = sign_mult * comp.sign
        if comp.ref is not None:
            cells |= _cells_with_sign(comp.ref, want_sign, contract, by_key, eff, visiting | {qty_name})
        elif eff == want_sign and not comp.carve_out:
            m = by_key.get((qty.name, comp.name))
            if m is not None:
                cells.update(m.item_ids)
    return cells


def _carved(carve_notes: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(n["params"]["quantity"], n["params"]["component"]) for n in carve_notes}


def apply_carve_outs(
    contract: ContractCovenants, mappings: list[TermMapping], plan: PlanData
) -> tuple[list[TermMapping], list[dict[str, Any]]]:
    """A carve-out ("X, excluding Y") may only remove cells that the rest of its quantity contains.

    If the cells chosen for X already leave Y out, subtracting Y again would count the exclusion
    twice; those cells are dropped from the carve-out and the reader is told.
    """
    by_key = {(m.quantity, m.component): m for m in mappings}
    notes: list[dict[str, Any]] = []
    out: list[TermMapping] = []
    for m in mappings:
        qty = next((q for q in contract.quantities if q.name == m.quantity), None)
        comp = next((c for c in qty.components if c.name == m.component), None) if qty else None
        if comp is None or not comp.carve_out or not m.item_ids:
            out.append(m)
            continue
        base = _contained_cells(_cells_with_sign(qty.name, -comp.sign, contract, by_key), plan)
        keep = [i for i in m.item_ids if i in base]
        dropped = [i for i in m.item_ids if i not in base]
        if dropped:
            notes.append(notice("carve_out_not_contained", component=m.component, quantity=m.quantity,
                                cells=", ".join(dropped)))
            m = m.model_copy(update={"item_ids": keep})
        out.append(m)
    return out, notes


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
        adjusted, carve_notes = apply_carve_outs(contract, cached_mappings, plan)
        cached_meta["warnings"] = carve_notes + _mapping_warnings(contract, adjusted, plan, _carved(carve_notes))
        return adjusted, cached_meta

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

    raw_mappings = term_mappings
    term_mappings, carve_notes = apply_carve_outs(contract, raw_mappings, plan)
    warnings = carve_notes + _mapping_warnings(contract, term_mappings, plan, _carved(carve_notes))
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
        # The raw LLM choice is cached; the deterministic carve-out rule is re-applied on every load.
        "mappings": [m.model_dump(mode="json") for m in raw_mappings],
        "meta": meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_file.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return term_mappings, meta
