"""CLI entry point for Finn (`finn watch` and `finn run --eml <file>`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from finn.audit import write_audit
from finn.config import load_settings
from finn.fmt import fmt_num, fmt_pct
from finn.llm import make_llm
from finn.mail.inbox import parse_eml_file
from finn.models import Result
from finn.pipeline import RunContext, generate_run_id, run_check


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for `finn`."""
    parser = argparse.ArgumentParser(
        prog="finn",
        description="Finn – Email-driven financial covenant check agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "watch",
        help="Poll IMAP inbox for covenant check orders and reply via SMTP",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run a single covenant check offline from a local .eml file",
    )
    run_parser.add_argument(
        "--eml",
        type=Path,
        required=True,
        help="Path to the input .eml file containing the Excel attachment",
    )
    run_parser.add_argument(
        "--compare-expected",
        action="store_true",
        help="Compare every calculated result against tests/expected_2027.json",
    )

    return parser


def _fmt_result_val(val: float, unit: str, decimals: int = 2) -> str:
    if unit == "percent":
        return fmt_pct(val, decimals)
    return fmt_num(val, decimals)


def _print_report_table(results: list[Result]) -> None:
    header = (
        f"{'Vertrag':<38} | {'Covenant':<33} | {'Wert':>10} | "
        f"{'Grenzwert':>11} | {'Headroom':>10} | {'eingehalten':<11} | Stress"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        short_contract = (
            r.contract_label[:35] + "..."
            if len(r.contract_label) > 38
            else r.contract_label
        )
        val_str = _fmt_result_val(r.value_reported, r.unit, 2)
        lim_str = f"{r.operator} {_fmt_result_val(r.limit, r.unit, 2)}"
        hr_str = _fmt_result_val(r.headroom, r.unit, 2)
        met_str = "Ja" if r.met else "Nein"
        print(
            f"{short_contract:<38} | {r.covenant_name:<33} | {val_str:>10} | "
            f"{lim_str:>11} | {hr_str:>10} | {met_str:<11} | {r.stress_text}"
        )


def _compare_with_expected(audit: dict[str, Any]) -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    expected_path = repo_root / "tests" / "expected_2027.json"
    expected_data = json.loads(expected_path.read_text(encoding="utf-8"))
    exp_contracts: dict[str, Any] = expected_data["contracts"]

    extracted_contracts = audit.get("steps", {}).get("extract_covenants", {}).get("contracts", [])
    label_to_file: dict[str, str] = {}
    for entry in extracted_contracts:
        c_dict = entry.get("contract", {})
        label_to_file[c_dict.get("contract_label", "")] = c_dict.get("contract_file", "")

    results_list = [
        Result.model_validate(r)
        for r in audit.get("steps", {}).get("calculate", {}).get("results", [])
    ]

    print("\n=== Vergleich mit tests/expected_2027.json ===")
    all_ok = True
    for r in results_list:
        c_file = label_to_file.get(r.contract_label, "")
        exp_c = exp_contracts.get(c_file, {})
        exp_covs: dict[str, Any] = exp_c.get("covenants", {})
        matched_name = None
        matched_exp = None
        for cov_name, cov_vals in exp_covs.items():
            if (
                cov_vals.get("unit") == r.unit
                and cov_vals.get("operator") == r.operator
                and abs(float(cov_vals.get("limit", 0.0)) - r.limit) < 1e-6
            ):
                matched_name = cov_name
                matched_exp = cov_vals
                break

        if matched_exp is None:
            all_ok = False
            print(f"✗ {c_file} [{r.covenant_name}]: Kein passender Eintrag in expected_2027.json")
            continue

        diff_raw = abs(r.value_raw - float(matched_exp["value_raw"]))
        diff_rep = abs(round(r.value_reported, 4) - float(matched_exp["value_reported"]))
        diff_hr = abs(round(r.headroom, 4) - float(matched_exp["headroom"]))
        met_ok = r.met == bool(matched_exp["met"])

        ok = diff_raw <= 1e-3 and diff_rep <= 1e-3 and diff_hr <= 1e-3 and met_ok
        mark = "✓" if ok else "✗"
        if not ok:
            all_ok = False
        print(
            f"{mark} {c_file} – {matched_name}: "
            f"value_reported={r.value_reported:.4f} (exp={matched_exp['value_reported']}, Δ={diff_rep:.4f}), "
            f"headroom={r.headroom:.4f} (exp={matched_exp['headroom']}, Δ={diff_hr:.4f}), "
            f"met={r.met} (exp={matched_exp['met']})"
        )
    return all_ok


def _run_eml_command(eml_path: Path, compare_expected: bool) -> int:
    eml_path = Path(eml_path)
    if not eml_path.exists():
        print(f"Fehler: Die E-Mail-Datei „{eml_path}“ wurde nicht gefunden.")
        return 2

    parsed_mail = parse_eml_file(eml_path)
    xlsx_att = parsed_mail.first_xlsx_attachment()
    if xlsx_att is None:
        print(
            "Fehler: Die E-Mail enthält keinen Excel-Anhang (.xlsx) mit dem Wirtschaftsplan. "
            "Bitte senden Sie den Wirtschaftsplan als .xlsx-Datei."
        )
        return 2

    settings = load_settings()
    run_id = generate_run_id()
    run_dir = Path(settings.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    saved_xlsx_path = run_dir / Path(xlsx_att.filename).name
    saved_xlsx_path.write_bytes(xlsx_att.payload)

    llm = make_llm(settings)
    ctx = RunContext.create(settings=settings, llm=llm, run_id=run_id)
    outcome = run_check(saved_xlsx_path, ctx)

    outcome.audit["email"] = {
        "eml_file": str(eml_path),
        "from": parsed_mail.sender,
        "subject": parsed_mail.subject,
        "message_id": parsed_mail.message_id,
        "date": parsed_mail.date,
        "body_text": parsed_mail.body_text,
        "attachment_filename": xlsx_att.filename,
        "saved_xlsx_path": str(saved_xlsx_path),
    }
    audit_path = write_audit(outcome.audit, settings.runs_dir)

    _print_report_table(outcome.report.results)

    if outcome.report.contracts_without_covenants:
        print("\nVerträge ohne Financial Covenants:")
        for cc in outcome.report.contracts_without_covenants:
            print(f"  - {cc.contract_label}: {cc.reason_if_none}")

    if outcome.report.warnings:
        print("\nHinweise / Warnungen:")
        for w in outcome.report.warnings:
            print(f"  ! {w}")

    print(f"\nAudit-Log: {audit_path}")

    if compare_expected:
        _compare_with_expected(outcome.audit)

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Finn CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "watch":
        print("not implemented")
        return 0
    if args.command == "run":
        return _run_eml_command(args.eml, bool(args.compare_expected))

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
