"""Tests for the reply email: content, traceability, email-safety, languages, MIME threading."""

from __future__ import annotations

import email
import email.policy
import re
from pathlib import Path
from typing import Any

import pytest

from finn.config import Settings
from finn.mail.outbox import build_reply_message
from finn.models import Source
from finn.pipeline import RunContext, run_check
from finn.report import ReplyContext, build_reply, build_simple_reply, reply_subject
from golden_llm import GoldenFakeLLM

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_XLSX = REPO_ROOT / "data" / "samples" / "Wirtschaftsplan_2027.xlsx"
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"


@pytest.fixture(scope="module")
def report(tmp_path_factory: pytest.TempPathFactory) -> Any:
    tmp = tmp_path_factory.mktemp("report")
    settings = Settings(llm_api_key="x", llm_model="fake", contracts_dir=CONTRACTS_DIR,
                        cache_dir=tmp / "cache", runs_dir=tmp / "runs")
    ctx = RunContext.create(settings=settings, llm=GoldenFakeLLM(), run_id="test_run")
    return run_check(SAMPLE_XLSX, ctx).report


def _ctx(lang: str = "de") -> ReplyContext:
    return ReplyContext(lang=lang, salutation="Frau Möller",
                        original_subject="Covenant-Prüfung Wirtschaftsplan 2027", run_id="test_run")


def test_reply_contains_every_number_cell_and_source(report: Any) -> None:
    reply = build_reply(report, _ctx(), None)
    for body in (reply.html, reply.text):
        for v in ("34,32 %", "1,56", "30,7 %", "3,37", "≥ 27,5 %", "≥ 1,30", "≥ 30,0 %", "≤ 3,50",
                  "6,82 Prozentpunkte", "0,26", "0,7 Prozentpunkte", "0,13"):
            assert v in body, v
        for r in report.results:
            for cell in r.plan_cells:
                assert cell in body
        assert "Sparkasse" in body  # contract without covenants with its reason
    assert "Alle 4 Covenants werden zum 31.12.2027 auf Planbasis eingehalten." in reply.text
    assert "deterministisch im Code berechnet" in reply.text


def test_status_badges(report: Any) -> None:
    html_body = build_reply(report, _ctx(), None).html
    overview = html_body.split("Übersicht", 1)[1].split("Details je Vertrag", 1)[0]
    names = re.findall(r'width="48%"><strong>(.*?)</strong>', overview)
    badges = re.findall(r'font-weight:600;white-space:nowrap;">([^<]+)</span>', overview)
    status = dict(zip(names, badges))
    assert status == {
        "Eigenkapitalquote": "knapp",
        "Dynamischer Verschuldungsgrad": "knapp",
        "Schuldendienstdeckungsgrad": "eingehalten",
        "Wirtschaftliche Eigenkapitalquote": "eingehalten",
    }


def test_tightest_covenant_comes_first(report: Any) -> None:
    text = build_reply(report, _ctx(), None).text
    overview = text.split("ÜBERSICHT", 1)[1].split("DETAILS", 1)[0]
    assert overview.strip().splitlines()[0].startswith("- Vertrag 4")


def test_html_is_email_safe(report: Any) -> None:
    html_body = build_reply(report, _ctx(), None).html.lower()
    for bad in ("<script", "display:flex", "display:grid", "<details", "var(--", "src=\"http", "<link"):
        assert bad not in html_body


def test_unverified_quote_is_marked(report: Any) -> None:
    r0 = report.results[0]
    bad = Source(page=1, section="§ 99", quote="erfundenes Zitat das nicht existiert", verified=False)
    patched = report.model_copy(deep=True)
    patched.results[0] = r0.model_copy(update={"contract_sources": [bad, *r0.contract_sources]})
    reply = build_reply(patched, _ctx(), None)
    assert "⚠ nicht verifiziert" in reply.html and "⚠ nicht verifiziert" in reply.text


def test_english_reply_uses_english_words_and_numbers(report: Any) -> None:
    reply = build_reply(report, _ctx("en"), None)
    assert "All 4 covenants are met" in reply.text
    assert "30.7 %" in reply.text and "30,7 %" not in reply.text
    assert "Overview" in reply.html


def test_focus_contract_comes_first(report: Any) -> None:
    ctx = _ctx()
    ctx.focus_contracts = ["Förderbank"]
    text = build_reply(report, ctx, None).text
    overview = text.split("ÜBERSICHT", 1)[1].split("DETAILS", 1)[0]
    assert overview.strip().splitlines()[0].startswith("- Vertrag 2")


def test_subject_prefix_not_doubled() -> None:
    assert reply_subject("Covenant-Prüfung Wirtschaftsplan 2027", "de") == "Re: Covenant-Prüfung Wirtschaftsplan 2027"
    assert reply_subject("Re: X", "de") == "Re: X"
    assert reply_subject("RE: X", "de") == "RE: X"


def test_mime_message_threads_and_attaches(report: Any) -> None:
    reply = build_reply(report, _ctx(), None)
    msg = build_reply_message(reply, from_addr="finn@example.com", from_name="Finn",
                              to_addr="k.moeller@example.com", in_reply_to="<abc@x>",
                              references="<abc@x>", attachments=[("finn_audit.json", b"{}", "application/json")])
    parsed = email.message_from_bytes(bytes(msg), policy=email.policy.default)
    assert parsed["In-Reply-To"] == "<abc@x>"
    assert "<abc@x>" in parsed["References"]
    assert parsed["X-Finn-Reply"] == "1"
    assert parsed.get_body(("html",)) is not None and parsed.get_body(("plain",)) is not None
    assert [a.get_filename() for a in parsed.iter_attachments()] == ["finn_audit.json"]


@pytest.mark.parametrize("kind,params", [
    ("missing_attachment", {}), ("wrong_attachment", {"filename": "plan.pdf"}),
    ("plan_format_error", {"message": "Blatt fehlt."}), ("unrelated", {}),
    ("internal_error", {"run_id": "r1"}),
])
def test_simple_replies_render_in_both_languages(kind: str, params: dict[str, Any]) -> None:
    for lang in ("de", "en"):
        reply = build_simple_reply(kind, ReplyContext(lang=lang, original_subject="Hallo"), params)
        assert reply.subject == "Re: Hallo"
        assert "Finn" in reply.text and "Finn" in reply.html
        for v in params.values():
            assert v in reply.text
