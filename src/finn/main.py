"""CLI entry point for Finn (`finn watch` and `finn run --eml <file>`)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence


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

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Finn CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "watch":
        print("not implemented")
        return 0
    if args.command == "run":
        print("not implemented")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

