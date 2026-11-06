"""End-to-end orchestration pipeline for the covenant verification run."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from finn.audit import redact_settings
from finn.calc import evaluate_contract
from finn.config import Settings
from finn.contracts.extract import extract_all
from finn.llm import JsonLLM, make_llm
from finn.mapping import map_terms
from finn.models import ContractCovenants, Result, RunReport, TermMapping
from finn.plan.reader import plan_item_catalog, read_plan


def generate_run_id() -> str:
    """Generate a sortable timestamp + short UUID run identifier."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{ts}_{short_uuid}"


@dataclass
class RunContext:
    """Execution context for a single covenant check run."""

    run_id: str
    started_at: str
    settings: Settings
    llm: JsonLLM
    paths: dict[str, Path] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        settings: Settings,
        llm: JsonLLM | None = None,
        run_id: str | None = None,
    ) -> RunContext:
        actual_run_id = run_id or generate_run_id()
        started_at = datetime.now(timezone.utc).isoformat()
        actual_llm = llm if llm is not None else make_llm(settings)
        paths = {
            "contracts_dir": Path(settings.contracts_dir),
            "cache_dir": Path(settings.cache_dir),
            "runs_dir": Path(settings.runs_dir),
        }
        return cls(
            run_id=actual_run_id,
            started_at=started_at,
            settings=settings,
            llm=actual_llm,
            paths=paths,
        )


@dataclass
class RunOutcome:
    """Final report and full audit dictionary produced by run_check."""

    report: RunReport
    audit: dict[str, Any]


def run_check(xlsx_path: Path, ctx: RunContext) -> RunOutcome:
    """Execute the fixed pipeline: read_plan -> extract_all -> map_terms -> evaluate_contract -> RunReport."""
    xlsx_path = Path(xlsx_path)
    contracts_dir = ctx.paths.get("contracts_dir", Path(ctx.settings.contracts_dir))
    cache_dir = ctx.paths.get("cache_dir", Path(ctx.settings.cache_dir))

    t_pipeline_start = time.monotonic()

    # Step 1: Read the Excel business plan (PlanFormatError propagates to caller)
    t0 = time.monotonic()
    plan = read_plan(xlsx_path)
    plan_duration_s = round(time.monotonic() - t0, 3)
    catalog_with_values = plan_item_catalog(plan, include_values=True)

    # Step 2: Extract covenants from all contracts (cached)
    t0 = time.monotonic()
    extracted_pairs = extract_all(contracts_dir, ctx.llm, cache_dir)
    extract_duration_s = round(time.monotonic() - t0, 3)

    # Step 3 & 4: Map terms (cached) and evaluate covenants in code per contract
    t_map_start = time.monotonic()
    all_mappings: list[TermMapping] = []
    mapping_step_records: list[dict[str, Any]] = []
    contracts_without_covenants: list[ContractCovenants] = []
    prefixed_warnings: list[str] = list(plan.checks)

    for contract, ext_meta in extracted_pairs:
        for w in ext_meta.get("warnings", []):
            prefixed_warnings.append(f"[{contract.contract_label}] {w}")

        if not contract.has_covenants:
            contracts_without_covenants.append(contract)
            continue

        c_mappings, map_meta = map_terms(contract, plan, ctx.llm, cache_dir)
        all_mappings.extend(c_mappings)
        for w in map_meta.get("warnings", []):
            prefixed_warnings.append(f"[{contract.contract_label}] {w}")

        mapping_step_records.append(
            {
                "contract_file": contract.contract_file,
                "contract_label": contract.contract_label,
                "meta": map_meta,
                "mappings": [m.model_dump(mode="json") for m in c_mappings],
            }
        )
    map_duration_s = round(time.monotonic() - t_map_start, 3)

    t_calc_start = time.monotonic()
    all_results: list[Result] = []
    for contract, _ext_meta in extracted_pairs:
        if not contract.has_covenants:
            continue
        c_results = evaluate_contract(contract, all_mappings, plan)
        all_results.extend(c_results)
    calc_duration_s = round(time.monotonic() - t_calc_start, 3)

    report = RunReport(
        results=all_results,
        contracts_without_covenants=contracts_without_covenants,
        warnings=prefixed_warnings,
    )

    total_duration_s = round(time.monotonic() - t_pipeline_start, 3)

    audit: dict[str, Any] = {
        "run_id": ctx.run_id,
        "started_at": ctx.started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": total_duration_s,
        "xlsx_path": str(xlsx_path),
        "settings": redact_settings(ctx.settings),
        "steps": {
            "read_plan": {
                "duration_s": plan_duration_s,
                "meta": plan.meta,
                "checks": plan.checks,
                "catalog_with_values": catalog_with_values,
            },
            "extract_covenants": {
                "duration_s": extract_duration_s,
                "contracts": [
                    {
                        "contract": cc.model_dump(mode="json"),
                        "meta": meta,
                    }
                    for cc, meta in extracted_pairs
                ],
            },
            "map_terms": {
                "duration_s": map_duration_s,
                "contracts": mapping_step_records,
            },
            "calculate": {
                "duration_s": calc_duration_s,
                "results": [r.model_dump(mode="json") for r in all_results],
            },
        },
        "report": report.model_dump(mode="json"),
    }

    return RunOutcome(report=report, audit=audit)
