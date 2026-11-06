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
    prompt_hash = hashlib.sha256(prompt_bytes).hexdigest()
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


def _semantic_warnings(cc: ContractCovenants) -> list[str]:
    """Run non-mutating semantic checks on extracted ContractCovenants and return warnings."""
    warnings: list[str] = []

    for cov in cc.covenants:
        if cov.unit == "percent" and not (0.0 < cov.limit <= 100.0):
            warnings.append(
                f"Covenant '{cov.id}': Prozent-Grenzwert {cov.limit} liegt außerhalb von (0, 100]."
            )

    for qty in cc.quantities:
        for comp in qty.components:
            if not (0.0 < comp.weight <= 1.0):
                warnings.append(
                    f"Größe '{qty.name}', Komponente '{comp.name}': Gewicht {comp.weight} liegt außerhalb von (0, 1]."
                )

    for src in _collect_all_sources(cc):
        if src.verified is False:
            warnings.append(
                f"Nicht verifiziertes Zitat ({src.section}, S. {src.page}): {src.verify_note}"
            )
        elif src.verify_note and src.verify_note.startswith("Seitenangabe korrigiert"):
            warnings.append(f"Hinweis zum Zitat ({src.section}): {src.verify_note}")

    return warnings


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

    forced = extracted.model_copy(update={"contract_file": pdf_path.name})
    verified = verify_contract(forced, pages)
    warnings = _semantic_warnings(verified)

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


def extract_all(
    contracts_dir: Path,
    llm: JsonLLM,
    cache_dir: Path,
    prompt_version: str = "v1",
    *,
    prompt_path: Path | None = None,
    use_cache: bool = True,
) -> list[tuple[ContractCovenants, dict[str, Any]]]:
    """Extract covenants for all PDF files in contracts_dir, sorted by file name."""
    contracts_dir = Path(contracts_dir)
    pdf_files = sorted(contracts_dir.glob("*.pdf"), key=lambda p: p.name)
    results: list[tuple[ContractCovenants, dict[str, Any]]] = []
    for pdf_path in pdf_files:
        res = extract_covenants(
            pdf_path,
            llm,
            cache_dir,
            prompt_version=prompt_version,
            prompt_path=prompt_path,
            use_cache=use_cache,
        )
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
            if ext_num_sig != gold_num_sig:
                differences.append(
                    f"{cc.contract_file} [{g_cov.name}]: numerator signature {ext_num_sig} != {gold_num_sig}"
                )
            if ext_den_sig != gold_den_sig:
                differences.append(
                    f"{cc.contract_file} [{g_cov.name}]: denominator signature {ext_den_sig} != {gold_den_sig}"
                )

    print()
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
