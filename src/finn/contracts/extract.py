"""LLM-based extraction of financial covenants and quantity definitions with disk caching."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finn.config import load_settings
from finn.contracts.pdf_text import extract_pages, file_sha256, render_for_llm
from finn.contracts.verify import verify_contract
from finn.notices import notice
from finn.numbers import canonical_value, extract_number_tokens
from finn.llm import JsonLLM, LLMClient
from finn.models import ContractCovenants, Quantity, Source

DEFAULT_PROMPT_PATH = (
    Path(__file__).resolve().parents[3] / "prompts" / "extract_covenants.txt"
)


def _compute_cache_key(
    pdf_path: Path,
    prompt_bytes: bytes,
    model_name: str,
    prompt_version: str,
) -> str:
    pdf_hash = file_sha256(pdf_path)
    # Line endings are normalised so a Windows checkout (CRLF) still hits the committed cache.
    prompt_hash = hashlib.sha256(prompt_bytes.replace(b"\r\n", b"\n")).hexdigest()
    combined = f"{pdf_hash}:{prompt_hash}:{model_name}:{prompt_version}"
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _collect_all_sources(cc: ContractCovenants) -> list[Source]:
    sources: list[Source] = []
    if cc.reason_source is not None:
        sources.append(cc.reason_source)
    for q in cc.quantities:
        sources.append(q.source)
    for c in cc.covenants:
        sources.append(c.source)
        sources.extend(c.definition_sources)
    return sources


def limit_not_in_quote(cc: ContractCovenants, lang: str = "de") -> list[str]:
    """Covenant ids whose limit does not occur as a number in the cited obligation quote.

    The quote check proves the text exists; this proves the limit was read from that text
    (and not, say, from a pricing grid elsewhere in the contract).
    """
    bad: list[str] = []
    for cov in cc.covenants:
        tokens = set(extract_number_tokens(cov.source.quote, lang))
        if canonical_value(cov.limit) not in tokens:
            bad.append(cov.id)
    return bad


def _reachable_quantities(cc: ContractCovenants, start: str) -> set[str]:
    """Names of all quantities reachable from `start` through component refs (including start)."""
    by_name = {q.name: q for q in cc.quantities}
    seen: set[str] = set()
    stack = [start]
    while stack:
        name = stack.pop()
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        stack.extend(c.ref for c in by_name[name].components if c.ref is not None)
    return seen


def structural_problems(cc: ContractCovenants) -> list[str]:
    """Covenant definitions that cannot be a valid ratio, as plain-language findings for the model.

    A numerator that contains the whole denominator quantity (or the other way round) mixes the
    two sides of the ratio. Refs must point to defined quantities.
    """
    problems: list[str] = []
    names = {q.name for q in cc.quantities}
    for qty in cc.quantities:
        for comp in qty.components:
            if comp.ref is not None and comp.ref not in names:
                problems.append(f"component '{comp.name}' in '{qty.name}' refers to undefined quantity '{comp.ref}'")
    for cov in cc.covenants:
        if cov.numerator == cov.denominator:
            problems.append(f"{cov.id}: numerator and denominator are the same quantity")
            continue
        if cov.denominator in _reachable_quantities(cc, cov.numerator):
            problems.append(f"{cov.id}: the numerator '{cov.numerator}' contains the denominator "
                            f"'{cov.denominator}'; the numerator must only contain what the contract "
                            "puts above the fraction line")
        if cov.numerator in _reachable_quantities(cc, cov.denominator):
            problems.append(f"{cov.id}: the denominator '{cov.denominator}' contains the numerator "
                            f"'{cov.numerator}'")
    return problems


def normalize_contract(cc: ContractCovenants) -> tuple[ContractCovenants, list[str]]:
    """Deterministic clean-up of an extraction, recorded for the audit (not shown as warnings).

    - Components with weight 0 contribute nothing; they are dropped (the model sometimes lists
      explicitly excluded items this way).
    - Quantities no covenant needs are dropped, so the mapping step only sees what is calculated.
    """
    changes: list[str] = []
    quantities = []
    for qty in cc.quantities:
        kept = [c for c in qty.components if c.weight != 0.0]
        for c in qty.components:
            if c.weight == 0.0:
                changes.append(f"dropped weight-0 component '{c.name}' from '{qty.name}'")
        quantities.append(qty.model_copy(update={"components": kept}) if len(kept) != len(qty.components) else qty)
    cc = cc.model_copy(update={"quantities": quantities})
    if cc.has_covenants:
        needed: set[str] = set()
        for cov in cc.covenants:
            needed |= _reachable_quantities(cc, cov.numerator) | _reachable_quantities(cc, cov.denominator)
        for q in cc.quantities:
            if q.name not in needed:
                changes.append(f"dropped quantity '{q.name}' (not used by any covenant)")
        cc = cc.model_copy(update={"quantities": [q for q in cc.quantities if q.name in needed]})
    return cc, changes


def _dedupe(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in notes:
        key = json.dumps(n, sort_keys=True, ensure_ascii=False, default=str)
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def _semantic_warnings(cc: ContractCovenants) -> list[dict[str, Any]]:
    """Non-mutating plausibility checks on an extraction, as language-neutral notices."""
    notes: list[dict[str, Any]] = []
    for cov in cc.covenants:
        if cov.unit == "percent" and not (0.0 < cov.limit <= 100.0):
            notes.append(notice("percent_limit_out_of_range", covenant=cov.name, limit=cov.limit))
    for qty in cc.quantities:
        for comp in qty.components:
            if not (0.0 < comp.weight <= 1.0):
                notes.append(notice("weight_out_of_range", weight=comp.weight, component=comp.name, quantity=qty.name))
    for src in _collect_all_sources(cc):
        if src.verified is False:
            notes.append(notice(src.verify_note or "quote_not_found", section=src.section, page=src.page))
        elif src.verify_note == "quote_page_corrected":
            notes.append(notice("quote_page_corrected", section=src.section, page=src.page,
                                original_page=src.original_page))
    return notes


def extract_covenants(
    pdf_path: Path,
    llm: JsonLLM,
    cache_dir: Path,
    prompt_version: str = "v1",
    *,
    prompt_path: Path | None = None,
    use_cache: bool = True,
) -> tuple[ContractCovenants, dict[str, Any]]:
    """Extract and verify ContractCovenants from a PDF contract with SHA-256 disk caching."""
    pdf_path = Path(pdf_path)
    cache_dir = Path(cache_dir)
    actual_prompt_path = Path(prompt_path) if prompt_path is not None else DEFAULT_PROMPT_PATH
    prompt_bytes = actual_prompt_path.read_bytes()
    prompt_text = prompt_bytes.decode("utf-8")

    model_name = getattr(llm, "model_name", "unknown-model")
    cache_key = _compute_cache_key(pdf_path, prompt_bytes, model_name, prompt_version)
    cache_file = cache_dir / f"covenants_{cache_key[:16]}.json"

    if use_cache and cache_file.exists():
        cached_payload = json.loads(cache_file.read_text(encoding="utf-8"))
        cached_contract = ContractCovenants.model_validate(cached_payload["contract"])
        cached_meta = dict(cached_payload.get("meta", {}))
        cached_meta["cache_hit"] = True
        cached_meta["cache_file"] = str(cache_file)
        return cached_contract, cached_meta

    pages = extract_pages(pdf_path)
    rendered_text = render_for_llm(pages)
    user_message = f"Contract file: {pdf_path.name}\n\n{rendered_text}"

    extracted, llm_meta = llm.complete_json(
        system=prompt_text,
        user=user_message,
        schema=ContractCovenants,
        purpose=f"extract_covenants:{pdf_path.name}",
    )

    def _check(candidate: ContractCovenants) -> tuple[ContractCovenants, list[str], list[str], list[str]]:
        checked = verify_contract(candidate.model_copy(update={"contract_file": pdf_path.name}), pages)
        checked, changes = normalize_contract(checked)
        return checked, limit_not_in_quote(checked), structural_problems(checked), changes

    verified, missing_limits, struct, normalized = _check(extracted)
    if missing_limits or struct:
        # One corrective round: name the problems, let the model re-read. Code never edits a value.
        findings: list[str] = []
        if missing_limits:
            findings.append(
                "for these covenants the limit does not appear in the quoted obligation text: "
                f"{', '.join(missing_limits)}. Re-read the clause that states the binding limit and "
                "quote the sentence that contains it"
            )
        findings += struct
        feedback = "\n\nYour previous answer was checked by code and has these problems:\n- " + "\n- ".join(findings)
        retry, retry_meta = llm.complete_json(
            system=prompt_text,
            user=user_message + feedback,
            schema=ContractCovenants,
            purpose=f"extract_covenants:{pdf_path.name}",
        )
        r_verified, r_missing, r_struct, r_normalized = _check(retry)
        llm_meta = {**llm_meta, "correction_retry": {
            "missing_limits_before": missing_limits, "missing_limits_after": r_missing,
            "structural_before": struct, "structural_after": r_struct, "meta": retry_meta}}
        if (len(r_struct), len(r_missing)) < (len(struct), len(missing_limits)):
            verified, missing_limits, struct, normalized = r_verified, r_missing, r_struct, r_normalized

    if struct:
        # A ratio whose two sides overlap would give a confident wrong number: refuse this contract.
        raise ExtractionStructureError(
            f"{pdf_path.name}: covenant definition is not a valid ratio: {'; '.join(struct)}"
        )

    warnings = _semantic_warnings(verified)
    names = {c.id: c.name for c in verified.covenants}
    for cov_id in missing_limits:
        warnings.append(notice("limit_not_in_quote", covenant=names.get(cov_id, cov_id)))

    all_sources = _collect_all_sources(verified)
    verified_count = sum(1 for s in all_sources if s.verified is True)

    meta: dict[str, Any] = {
        **llm_meta,
        "cache_hit": False,
        "cache_key": cache_key,
        "cache_file": str(cache_file),
        "prompt_version": prompt_version,
        "quotes_total": len(all_sources),
        "quotes_verified": verified_count,
        "warnings": warnings,
        "normalized": normalized,
    }

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_payload = {
        "contract": verified.model_dump(mode="json"),
        "meta": meta,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    cache_file.write_text(
        json.dumps(cache_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return verified, meta


class ExtractionStructureError(RuntimeError):
    """The extracted covenant definition is structurally impossible, even after one correction."""


class NoContractsError(RuntimeError):
    """The contracts folder is empty or missing."""


def extract_all(
    contracts_dir: Path,
    llm: JsonLLM,
    cache_dir: Path,
    prompt_version: str = "v1",
    *,
    prompt_path: Path | None = None,
    use_cache: bool = True,
    errors: list[tuple[str, str]] | None = None,
) -> list[tuple[ContractCovenants, dict[str, Any]]]:
    """Extract covenants for all PDF files in contracts_dir, sorted by file name.

    If `errors` is given, a contract that fails is recorded there as (file name, reason) and the
    others are still processed; otherwise the first failure is raised.
    """
    contracts_dir = Path(contracts_dir)
    pdf_files = sorted(contracts_dir.glob("*.pdf"), key=lambda p: p.name)
    if not pdf_files:
        # Never answer "all fine" with an empty table: no contracts is a configuration error.
        raise NoContractsError(f"No contract PDFs found in {contracts_dir.resolve()}")
    results: list[tuple[ContractCovenants, dict[str, Any]]] = []
    for pdf_path in pdf_files:
        try:
            res = extract_covenants(
                pdf_path,
                llm,
                cache_dir,
                prompt_version=prompt_version,
                prompt_path=prompt_path,
                use_cache=use_cache,
            )
        except Exception as exc:
            if errors is None:
                raise
            errors.append((pdf_path.name, f"{type(exc).__name__}: {exc}"))
            continue
        results.append(res)
    return results


def _resolve_signature(
    qty_name: str,
    quantities_by_name: dict[str, Quantity],
    sign_mult: int = 1,
    weight_mult: float = 1.0,
    visiting: frozenset[str] = frozenset(),
) -> list[tuple[int, float]]:
    """Resolve a Quantity into a sorted list of leaf (sign, weight) pairs."""
    if qty_name in visiting or qty_name not in quantities_by_name:
        return []
    qty = quantities_by_name[qty_name]
    out: list[tuple[int, float]] = []
    for comp in qty.components:
        eff_sign = sign_mult * comp.sign
        eff_weight = round(weight_mult * comp.weight, 4)
        if comp.ref is not None:
            out.extend(
                _resolve_signature(
                    comp.ref,
                    quantities_by_name,
                    eff_sign,
                    eff_weight,
                    visiting | {qty_name},
                )
            )
        else:
            out.append((eff_sign, eff_weight))
    return sorted(out)


def _compare_with_golden(*, use_cache: bool = True) -> int:
    """Run live extraction on data/contracts and compare with tests/fixtures/golden_covenants.json."""
    settings = load_settings()
    llm = LLMClient(settings)
    repo_root = Path(__file__).resolve().parents[3]
    golden_path = repo_root / "tests" / "fixtures" / "golden_covenants.json"
    golden_list = [
        ContractCovenants.model_validate(item)
        for item in json.loads(golden_path.read_text(encoding="utf-8"))
    ]
    golden_by_file = {g.contract_file: g for g in golden_list}

    extracted_pairs = extract_all(
        settings.contracts_dir,
        llm,
        settings.cache_dir,
        use_cache=use_cache,
    )
    differences: list[str] = []
    infos: list[str] = []

    print(
        f"=== Live Extraction Comparison (configured: {llm.model_name}, "
        f"fallback: {llm.fallback_model_name or 'none'}, cache={'on' if use_cache else 'off'}) ===\n"
    )
    for cc, meta in extracted_pairs:
        gold = golden_by_file.get(cc.contract_file)
        if gold is None:
            differences.append(f"{cc.contract_file}: not in golden fixtures")
            continue

        q_tot = meta.get("quotes_total", 0)
        q_ver = meta.get("quotes_verified", 0)
        ver_pct = (100.0 * q_ver / q_tot) if q_tot else 100.0
        p_tok = meta.get("prompt_tokens", 0)
        c_tok = meta.get("completion_tokens", 0)
        lat = meta.get("latency_s", 0.0)
        actual_model = meta.get("model", llm.model_name)
        attempts = meta.get("attempts", 1)
        hit = "CACHE" if meta.get("cache_hit") else "LIVE"

        print(
            f"[{cc.contract_file}] ({hit}) model={actual_model} attempts={attempts} | "
            f"has_covenants={cc.has_covenants} (gold={gold.has_covenants}) | "
            f"covenants={len(cc.covenants)} (gold={len(gold.covenants)}) | "
            f"verified={q_ver}/{q_tot} ({ver_pct:.0f}%) | "
            f"tokens={p_tok}+{c_tok} | latency={lat:.2f}s"
        )

        if q_ver < q_tot:
            differences.append(
                f"{cc.contract_file}: {q_tot - q_ver} von {q_tot} Zitaten nicht verifiziert"
            )

        if cc.has_covenants != gold.has_covenants:
            differences.append(
                f"{cc.contract_file}: has_covenants={cc.has_covenants} != golden {gold.has_covenants}"
            )
            continue

        if len(cc.covenants) != len(gold.covenants):
            differences.append(
                f"{cc.contract_file}: covenant count {len(cc.covenants)} != golden {len(gold.covenants)}"
            )

        ext_q_map = {q.name: q for q in cc.quantities}
        gold_q_map = {q.name: q for q in gold.quantities}

        for g_cov in gold.covenants:
            matched = next(
                (
                    c
                    for c in cc.covenants
                    if c.unit == g_cov.unit
                    and c.operator == g_cov.operator
                    and abs(c.limit - g_cov.limit) < 1e-6
                ),
                None,
            )
            if matched is None:
                differences.append(
                    f"{cc.contract_file}: missing covenant matching ({g_cov.unit}, {g_cov.operator}, {g_cov.limit})"
                )
                print(f"  - MISSING covenant {g_cov.id} ({g_cov.name})")
                continue

            ext_num_sig = _resolve_signature(matched.numerator, ext_q_map)
            gold_num_sig = _resolve_signature(g_cov.numerator, gold_q_map)
            ext_den_sig = _resolve_signature(matched.denominator, ext_q_map)
            gold_den_sig = _resolve_signature(g_cov.denominator, gold_q_map)

            print(
                f"  - {g_cov.name}: rounding={matched.rounding_decimals} (gold={g_cov.rounding_decimals}), "
                f"test_basis={matched.test_basis} (gold={g_cov.test_basis})"
            )
            print(f"    num_sig={ext_num_sig} vs gold={gold_num_sig}")
            print(f"    den_sig={ext_den_sig} vs gold={gold_den_sig}")

            if matched.rounding_decimals != g_cov.rounding_decimals:
                differences.append(
                    f"{cc.contract_file} [{g_cov.name}]: rounding_decimals {matched.rounding_decimals} != {g_cov.rounding_decimals}"
                )
            if matched.test_basis != g_cov.test_basis:
                differences.append(
                    f"{cc.contract_file} [{g_cov.name}]: test_basis {matched.test_basis} != {g_cov.test_basis}"
                )
            # Structure differences are informational only: a model may legitimately split a
            # defined term into its listed sub-items or add a deduction line the contract
            # really contains. Correctness is proven end-to-end by the value check.
            if ext_num_sig != gold_num_sig:
                infos.append(
                    f"{cc.contract_file} [{g_cov.name}]: INFO (Struktur abweichend) Zähler {ext_num_sig} vs. {gold_num_sig}"
                )
            if ext_den_sig != gold_den_sig:
                infos.append(
                    f"{cc.contract_file} [{g_cov.name}]: INFO (Struktur abweichend) Nenner {ext_den_sig} vs. {gold_den_sig}"
                )

    print()
    for info in infos:
        print(f"  ~ {info}")
    if not differences:
        print("MATCH")
        return 0

    print("DIFFERENCES:")
    for diff in differences:
        print(f"  * {diff}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract and verify contract covenants.")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Run live extraction on data/contracts and compare with golden_covenants.json",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass the disk cache and force live LLM calls",
    )
    args = parser.parse_args(argv)
    if args.compare:
        return _compare_with_golden(use_cache=not args.no_cache)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
