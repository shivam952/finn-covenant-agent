"""The summary check: every sentence is attributed to covenants, uses only their numbers, claims no false status."""

from __future__ import annotations

from typing import Any

from finn.numbers import extract_number_tokens
from finn.render import load_texts
from finn.summary import Summary, SummarySentence, find_unsupported_numbers, write_summary

FACTS: dict[str, Any] = {
    "test_date": "31.12.2027",
    "covenants_total": "2",
    "covenants": [
        {"id": "V4-EKQ", "status_key": "tight", "value": "30,7 %", "limit": "≥ 30,0 %",
         "headroom": "0,7 Prozentpunkte",
         "stress": "Ein zusätzlicher Jahresverlust von 3.057 T€ würde die Quote unter 30,0 % drücken."},
        {"id": "V4-DYNVG", "status_key": "tight", "value": "3,37", "limit": "≤ 3,50", "headroom": "0,13",
         "stress": "Das EBITDA dürfte um 3,6 % (1.457 T€) sinken, bevor der Grenzwert von 3,50 überschritten wird."},
    ],
}
TEMPLATE = ["Template-Satz."]
CLAIMS = dict(load_texts("de")["summary_claims"])


class _ScriptedLLM:
    model_name = "scripted"

    def __init__(self, answers: list[list[tuple[str, list[str]]]]) -> None:
        self.answers = list(answers)
        self.users: list[str] = []

    def complete_json(self, system: str, user: str, schema: type, purpose: str) -> tuple[Any, dict[str, Any]]:
        self.users.append(user)
        return Summary(sentences=[SummarySentence(text=t, covenant_ids=c) for t, c in self.answers.pop(0)]), {}


def _run(llm: Any) -> Any:
    return write_summary(FACTS, "de", llm, TEMPLATE, max_sentences=3, max_sentence_chars=280,
                         small_counts_max=10, status_claims=CLAIMS)


def test_tokenizer_handles_locales_dates_and_unicode_minus() -> None:
    assert extract_number_tokens("30,7 % und 1.234,5 T€, −3,6 % zum 31.12.2027", "de") == [
        "date:31.12.2027", "30.7", "1234.5", "3.6"]
    assert extract_number_tokens("30.7 % and 1,234.5 as of 31.12.2027", "en") == ["date:31.12.2027", "30.7", "1234.5"]
    assert extract_number_tokens("6.216 T€", "de") == ["6216"]


def test_attributed_sentences_with_their_own_numbers_are_accepted() -> None:
    llm = _ScriptedLLM([[
        ("Die Eigenkapitalquote liegt mit 30,7 % knapp über 30,0 %.", ["V4-EKQ"]),
        ("Ein EBITDA-Rückgang um 3,6 % würde den Verschuldungsgrad über 3,50 heben.", ["V4-DYNVG"]),
        ("Die Prüfung beruht auf Planwerten.", []),
    ]])
    assert _run(llm).source == "llm"


def test_number_borrowed_from_another_covenant_is_rejected() -> None:
    llm = _ScriptedLLM([[("Die Eigenkapitalquote hat einen Puffer von 0,13.", ["V4-EKQ"])],
                        [("Die Eigenkapitalquote hat einen Puffer von 0,13.", ["V4-EKQ"])]])
    out = _run(llm)
    assert out.source == "template"
    assert "0.13" in llm.users[1]


def test_false_breach_claim_is_rejected_even_with_valid_numbers() -> None:
    """The reviewer's example: right numbers, wrong claim, directly under a verdict saying 'all met'."""
    bad = [("Der Dynamische Verschuldungsgrad ist mit 3,37 bereits verletzt.", ["V4-DYNVG"])]
    llm = _ScriptedLLM([bad, bad])
    out = _run(llm)
    assert out.source == "template"
    assert any("claims a breach" in p for p in out.meta["attempts"][0]["problems"])


def test_unknown_covenant_id_is_rejected() -> None:
    llm = _ScriptedLLM([[("Alles gut.", ["V9-XYZ"])], [("Alles gut.", ["V9-XYZ"])]])
    assert _run(llm).source == "template"


def test_invented_number_then_corrected_is_accepted() -> None:
    llm = _ScriptedLLM([[("Die Quote liegt bei 31,5 %.", ["V4-EKQ"])], [("Die Quote liegt bei 30,7 %.", ["V4-EKQ"])]])
    assert _run(llm).source == "llm"


def test_llm_exception_never_blocks_the_reply() -> None:
    class _Broken:
        model_name = "broken"

        def complete_json(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("network down")

    assert _run(_Broken()).source == "template"


def test_no_llm_gives_template() -> None:
    assert _run(None).source == "template"


def test_find_unsupported_numbers_uses_abs_values() -> None:
    assert find_unsupported_numbers(["um −3,6 % (1.457 T€)"], {"3.6", "1457"}, "de") == []
