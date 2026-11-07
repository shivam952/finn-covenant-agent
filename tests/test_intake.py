"""Intake + decision + handler: the LLM reads the mail into a schema, code decides, every mail gets a reply."""

from __future__ import annotations

from pathlib import Path

import pytest

import finn.handler
from finn.config import Settings
from finn.handler import handle_email
from finn.intake import OrderIntent, decide, is_automatic_or_own, read_intent
from finn.mail.inbox import parse_eml_file
from golden_llm import GoldenFakeLLM

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"


def _settings(tmp_path: Path) -> Settings:
    return Settings(llm_api_key="x", llm_model="fake", contracts_dir=CONTRACTS_DIR,
                    cache_dir=tmp_path / "cache", runs_dir=tmp_path / "runs",
                    finn_mail_address="finn@example.com")


CHECK = OrderIntent(intent="covenant_check", plan_year=2027, test_date="2027-12-31", salutation="Katrin Möller")


@pytest.mark.parametrize("sample,intent,expected", [
    ("order.eml", CHECK, "run"),
    ("order_informal.eml", CHECK.model_copy(update={"focus_contracts": ["Konsortialkredit"]}), "run"),
    ("order_english.eml", CHECK.model_copy(update={"reply_language": "en"}), "run"),
    ("no_attachment.eml", CHECK, "missing_attachment"),
    ("wrong_attachment.eml", CHECK, "wrong_attachment"),
    ("unrelated.eml", OrderIntent(intent="other"), "unrelated"),
])
def test_decision_per_sample(sample: str, intent: OrderIntent, expected: str) -> None:
    assert decide(parse_eml_file(SAMPLES / sample), intent).kind == expected


def test_unclear_without_plan_asks_back() -> None:
    parsed = parse_eml_file(SAMPLES / "no_attachment.eml")
    d = decide(parsed, OrderIntent(intent="unclear", missing_info=["Welches Planjahr?"]))
    assert d.kind == "needs_clarification" and d.questions == ["Welches Planjahr?"]


def test_intake_fallback_is_structural_when_llm_fails() -> None:
    llm = GoldenFakeLLM(intent=None)  # intake call raises
    with_plan = read_intent(parse_eml_file(SAMPLES / "order.eml"), llm, "de")
    without = read_intent(parse_eml_file(SAMPLES / "unrelated.eml"), llm, "de")
    assert with_plan.source == "fallback" and with_plan.intent.intent == "covenant_check"
    assert without.intent.intent == "unclear"


def test_intake_sends_body_and_filenames_not_attachment_bytes() -> None:
    seen: list[str] = []

    class _Spy(GoldenFakeLLM):
        def complete_json(self, system, user, schema, purpose):  # type: ignore[override]
            seen.append(user)
            return CHECK, {}

    read_intent(parse_eml_file(SAMPLES / "order.eml"), _Spy(), "de")
    assert "Wirtschaftsplan_2027.xlsx" in seen[0]
    assert "UEsDB" not in seen[0]  # base64 start of an .xlsx (zip) file: attachment bytes never reach the LLM
    assert len(seen[0]) < 10000


def test_loop_protection(tmp_path: Path) -> None:
    parsed = parse_eml_file(SAMPLES / "order.eml")
    assert is_automatic_or_own(parsed, "finn@example.com") is None
    parsed.headers["x-finn-reply"] = "1"
    assert is_automatic_or_own(parsed, "finn@example.com") is not None
    parsed = parse_eml_file(SAMPLES / "order.eml")
    parsed.headers["auto-submitted"] = "auto-replied"
    assert is_automatic_or_own(parsed, "finn@example.com") is not None
    parsed.headers["auto-submitted"] = "no"
    assert is_automatic_or_own(parsed, "finn@example.com") is None
    parsed.sender = "Finn <FINN@example.com>"
    assert is_automatic_or_own(parsed, "finn@example.com") == "own address"


def test_full_run_reply_is_threaded_and_uses_intent(tmp_path: Path) -> None:
    intent = CHECK.model_copy(update={"plan_year": 2028, "unsupported_requests": ["Vergleich mit dem Vorjahr"],
                                      "focus_contracts": ["Förderbank"]})
    h = handle_email(parse_eml_file(SAMPLES / "order.eml"), _settings(tmp_path), GoldenFakeLLM(intent=intent))
    assert h.decision.kind == "run" and h.outcome is not None
    text = h.reply.text
    assert text.startswith("Guten Tag Katrin Möller,")
    assert "Planjahr 2028" in text and "geprüft wurde 2027" in text.lower()  # the file wins, the mail is noted
    assert "Nicht Teil dieser Prüfung: Vergleich mit dem Vorjahr" in text
    overview = text.split("ÜBERSICHT", 1)[1]
    assert overview.strip().splitlines()[0].startswith("- Vertrag 2")  # focus contract first
    msg = h.message
    assert msg["To"].endswith("k.moeller@stadtwerke-musterstadt.example>")
    assert msg["In-Reply-To"] and msg["Subject"] == "Re: Covenant-Prüfung Wirtschaftsplan 2027"
    assert h.files["reply.eml"].exists() and h.audit_path.exists()


def test_english_mail_gets_english_reply(tmp_path: Path) -> None:
    h = handle_email(parse_eml_file(SAMPLES / "order_english.eml"), _settings(tmp_path),
                     GoldenFakeLLM(intent=CHECK.model_copy(update={"reply_language": "en"})))
    assert "All 4 covenants are met" in h.reply.text


@pytest.mark.parametrize("sample,needle", [
    ("no_attachment.eml", ".xlsx"),
    ("wrong_attachment.eml", "Vertrag_1_Sparkasse_Investitionsdarlehen.pdf"),
])
def test_bad_input_gets_polite_reply_not_a_crash(tmp_path: Path, sample: str, needle: str) -> None:
    h = handle_email(parse_eml_file(SAMPLES / sample), _settings(tmp_path), GoldenFakeLLM(intent=CHECK))
    assert h.outcome is None and h.error is None and needle in h.reply.text


def test_unrelated_mail(tmp_path: Path) -> None:
    h = handle_email(parse_eml_file(SAMPLES / "unrelated.eml"), _settings(tmp_path),
                     GoldenFakeLLM(intent=OrderIntent(intent="other", salutation="Thomas Becker")))
    assert h.decision.kind == "unrelated" and "Covenant-Prüfungen" in h.reply.text


def test_broken_excel_gets_plan_format_reply(tmp_path: Path) -> None:
    parsed = parse_eml_file(SAMPLES / "order.eml")
    parsed.attachments[0].payload = b"this is not an excel file"
    h = handle_email(parsed, _settings(tmp_path), GoldenFakeLLM(intent=CHECK))
    assert h.decision.kind == "run" and h.outcome is None and h.error is None
    assert "nicht auswerten" in h.reply.text


def test_internal_error_becomes_apology_with_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(finn.handler, "run_check", boom)
    h = handle_email(parse_eml_file(SAMPLES / "order.eml"), _settings(tmp_path), GoldenFakeLLM(intent=CHECK))
    assert h.error and "disk on fire" in h.error
    assert h.run_id in h.reply.text
    assert "disk on fire" not in h.reply.text  # internals stay in the audit, not in the mail
    assert "disk on fire" in h.audit_path.read_text(encoding="utf-8")


def test_one_failing_contract_does_not_hide_the_others(tmp_path: Path) -> None:
    class _FailsOnV3(GoldenFakeLLM):
        def complete_json(self, system, user, schema, purpose):  # type: ignore[override]
            if purpose == "map_terms:Vertrag_3_Landesbank_Investitionsdarlehen.pdf":
                raise RuntimeError("model timeout")
            return super().complete_json(system, user, schema, purpose)

    h = handle_email(parse_eml_file(SAMPLES / "order.eml"), _settings(tmp_path), _FailsOnV3(intent=CHECK))
    assert h.outcome is not None and h.error is None
    ids = {r.covenant_id for r in h.outcome.report.results}
    assert ids == {"V2-WEKQ", "V4-EKQ", "V4-DYNVG"}
    assert h.reply.text.split("\n")[2].startswith("Achtung: 1 Vertrag/Verträge konnten nicht ausgewertet werden")
    assert "Vertrag 3" in h.reply.text and "bitte manuell prüfen" in h.reply.text


def test_reply_all_keeps_colleagues_and_drops_finn(tmp_path: Path) -> None:
    parsed = parse_eml_file(SAMPLES / "order.eml")
    parsed.headers["to"] = "Finn <finn@example.com>, Jan Kühn <j.kuehn@stadtwerke-musterstadt.example>"
    parsed.headers["cc"] = "Controlling <controlling@stadtwerke-musterstadt.example>"
    h = handle_email(parsed, _settings(tmp_path), GoldenFakeLLM(intent=CHECK))
    cc = h.message["Cc"]
    assert "j.kuehn@" in cc and "controlling@" in cc and "finn@example.com" not in cc


def test_noreply_anywhere_in_the_address_is_ignored() -> None:
    parsed = parse_eml_file(SAMPLES / "order.eml")
    parsed.sender = "Google <googlecommunityteam-noreply@google.com>"
    assert is_automatic_or_own(parsed, "finn@example.com") is not None


def test_existing_german_reply_prefix_is_kept() -> None:
    from finn.report import reply_subject

    assert reply_subject("AW: Covenant-Prüfung", "de") == "AW: Covenant-Prüfung"


def test_plan_error_is_explained_in_the_reply_language(tmp_path: Path) -> None:
    parsed = parse_eml_file(SAMPLES / "order_english.eml")
    parsed.attachments[0].payload = b"not an excel file"
    h = handle_email(parsed, _settings(tmp_path), GoldenFakeLLM(intent=CHECK.model_copy(update={"reply_language": "en"})))
    assert "is not a readable Excel workbook" in h.reply.text
    assert "Arbeitsmappe" not in h.reply.text


def test_email_year_is_used_when_the_file_has_it(tmp_path: Path) -> None:
    """Plan year named in the mail and present in the file -> that column; absent -> the file decides + note."""
    h = handle_email(parse_eml_file(SAMPLES / "order.eml"), _settings(tmp_path),
                     GoldenFakeLLM(intent=CHECK.model_copy(update={"plan_year": 2027})))
    assert h.outcome.report.plan_meta["plan_year"] == 2027
    assert not any("Planjahr" in w for w in h.outcome.report.warnings)
