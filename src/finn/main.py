"""CLI entry point for Finn (`finn watch` and `finn run --eml <file>`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from finn.config import load_settings
from finn.fmt import fmt_num, fmt_pct
from finn.llm import make_llm
from finn.mail.inbox import parse_eml_file
from finn.models import Result
from finn.handler import handle_email
from finn.mail.inbox import ImapMailbox
from finn.mail.outbox import send_message
from finn.watcher import run_watch
from finn.render import load_texts, render_stress


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI argument parser for `finn`."""
    parser = argparse.ArgumentParser(
        prog="finn",
        description="Finn – Email-driven financial covenant check agent",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    watch_parser = subparsers.add_parser(
        "watch",
        help="Poll IMAP inbox for covenant check orders and reply via SMTP",
    )
    watch_parser.add_argument("--once", action="store_true", help="Poll a single time and exit")

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
        help="Compare every calculated result against the expected answer key",
    )
    run_parser.add_argument(
        "--lang",
        choices=["de", "en"],
        default=None,
        help="Reply language (default: REPLY_LANGUAGE from .env)",
    )
    run_parser.add_argument(
        "--preview",
        action="store_true",
        help="Open the generated reply.html in the default browser",
    )
    run_parser.add_argument(
        "--send",
        action="store_true",
        help="Also send the reply to the sender via SMTP (needs mail settings in .env)",
    )

    return parser


def _fmt_result_val(val: float, unit: str, decimals: int = 2, lang: str = "de") -> str:
    if unit == "percent":
        return fmt_pct(val, decimals, lang=lang)
    return fmt_num(val, decimals, lang=lang)


def _print_report_table(
    results: list[Result],
    lang: str = "de",
    amount_unit: str = "",
) -> None:
    rt = load_texts(lang)["report"]
    ct = load_texts(lang)["cli"]
    header = (
        f"{rt['col_contract']:<38} | {rt['col_covenant']:<33} | {rt['col_value']:>10} | "
        f"{rt['col_limit']:>11} | {rt['col_headroom']:>10} | {rt['col_status']:<11} | {rt['label_stress']}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        short_contract = (
            r.contract_label[:35] + "..."
            if len(r.contract_label) > 38
            else r.contract_label
        )
        val_str = _fmt_result_val(r.value_reported, r.unit, 2, lang=lang)
        lim_str = f"{r.operator} {_fmt_result_val(r.limit, r.unit, 2, lang=lang)}"
        hr_str = _fmt_result_val(r.headroom, r.unit, 2, lang=lang)
        met_str = ct["met_yes"] if r.met else ct["met_no"]
        stress_str = render_stress(r.stress, lang=lang, unit=amount_unit)
        print(
            f"{short_contract:<38} | {r.covenant_name:<33} | {val_str:>10} | "
            f"{lim_str:>11} | {hr_str:>10} | {met_str:<11} | {stress_str}"
        )


def _compare_with_expected(audit: dict[str, Any]) -> bool:
    repo_root = Path(__file__).resolve().parents[2]
    expected_path = sorted((repo_root / "tests").glob("expected_*.json"))[0]
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

    print(f"\n=== Vergleich mit {expected_path.name} ===")
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
            print(f"✗ {c_file} [{r.covenant_name}]: Kein passender Eintrag in {expected_path.name}")
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


def _smtp_sender(settings: Any) -> Any:
    def _send(message: Any) -> None:
        send_message(
            message,
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.mail_user,
            password=settings.mail_app_password,
        )

    return _send


def _run_eml_command(
    eml_path: Path,
    compare_expected: bool,
    lang_override: str | None = None,
    preview: bool = False,
    send: bool = False,
) -> int:
    eml_path = Path(eml_path)
    settings = load_settings()
    lang = lang_override or settings.reply_language
    cli_texts = load_texts(lang)["cli"]
    if not eml_path.exists():
        print(cli_texts["error_eml_not_found"].format(path=eml_path))
        return 2

    parsed_mail = parse_eml_file(eml_path)
    try:
        llm = make_llm(settings)
    except ValueError as exc:  # missing key or model: say what to set instead of a traceback
        print(cli_texts["error_no_llm"].format(detail=exc))
        return 2
    h = handle_email(parsed_mail, settings, llm, lang_override=lang_override)
    if h.ignored_reason is not None:
        print(cli_texts["ignored"].format(reason=h.ignored_reason))
        return 0

    lang = h.reply_lang or lang
    cli_texts = load_texts(lang)["cli"]
    print(cli_texts["decision"].format(kind=h.decision.kind if h.decision else "error"))
    ok = True
    if h.outcome is not None:
        report = h.outcome.report
        amount_unit = str(report.plan_meta.get("amount_unit", ""))
        _print_report_table(report.results, lang=lang, amount_unit=amount_unit)
        if report.contracts_without_covenants:
            print("\n" + cli_texts["no_covenants_heading"])
            for cc in report.contracts_without_covenants:
                print(f"  - {cc.contract_label}: {cc.reason_if_none}")
        if report.warnings:
            print("\n" + cli_texts["warnings_heading"])
            for w in report.warnings:
                print(f"  ! {w}")
        if compare_expected:
            ok = _compare_with_expected(h.outcome.audit)
    elif h.reply is not None:
        print(h.reply.text)

    print("\n" + cli_texts["audit_written"].format(path=h.audit_path))
    print(cli_texts["reply_written"].format(path=h.files.get("reply.html")))

    if send:
        settings.require_mail()
        _smtp_sender(settings)(h.message)
        print(cli_texts["reply_sent"].format(to=h.message["To"]))

    if preview and h.files.get("reply.html"):
        import webbrowser

        webbrowser.open(h.files["reply.html"].resolve().as_uri())

    if h.error:
        return 1
    if h.outcome is None:
        return 2  # no check was run (missing/wrong attachment, unrelated, clarification); a reply was prepared
    return 0 if ok else 1


def _watch_command(once: bool) -> int:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = load_settings()
    cli_texts = load_texts(settings.reply_language)["cli"]
    try:
        settings.require_mail()
        llm = make_llm(settings)
    except ValueError as exc:
        print(cli_texts["error_no_llm"].format(detail=exc))
        return 2

    def _make_mailbox() -> ImapMailbox:
        box = ImapMailbox(settings.imap_host, settings.imap_port, settings.mail_user, settings.mail_app_password)
        box.connect()
        return box

    print(load_texts(settings.reply_language)["cli"]["watching"].format(
        address=settings.finn_mail_address, seconds=settings.poll_seconds))
    return run_watch(settings, _make_mailbox, _smtp_sender(settings), llm, once=once)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Finn CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "watch":
        return _watch_command(bool(args.once))
    if args.command == "run":
        return _run_eml_command(
            args.eml,
            bool(args.compare_expected),
            lang_override=args.lang,
            preview=bool(args.preview),
            send=bool(args.send),
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
