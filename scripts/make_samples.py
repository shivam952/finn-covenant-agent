"""Generate synthetic sample .eml files in data/samples/ for testing Finn."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_DIR = REPO_ROOT / "data" / "samples"
CONTRACTS_DIR = REPO_ROOT / "data" / "contracts"
XLSX_PATH = SAMPLES_DIR / "Wirtschaftsplan_2027.xlsx"


def _build_eml(
    *,
    sender: str,
    to: str,
    subject: str,
    message_id: str,
    date: str,
    body: str,
    attachment_bytes: bytes | None = None,
    attachment_filename: str | None = None,
    maintype: str = "application",
    subtype: str = "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = date
    msg.set_content(body)

    if attachment_bytes is not None and attachment_filename is not None:
        msg.add_attachment(
            attachment_bytes,
            maintype=maintype,
            subtype=subtype,
            filename=attachment_filename,
        )

    return msg.as_bytes()


def main() -> None:
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    xlsx_bytes = XLSX_PATH.read_bytes()
    pdf_bytes = (
        CONTRACTS_DIR / "Vertrag_1_Sparkasse_Investitionsdarlehen.pdf"
    ).read_bytes()

    sender_katrin = "Katrin Möller <k.moeller@stadtwerke-musterstadt.example>"
    recipient_finn = "finn@example.com"

    # 1. order.eml
    (SAMPLES_DIR / "order.eml").write_bytes(
        _build_eml(
            sender=sender_katrin,
            to=recipient_finn,
            subject="Covenant-Prüfung Wirtschaftsplan 2027",
            message_id="<order-2027-001@stadtwerke-musterstadt.example>",
            date="Fri, 06 Nov 2026 09:15:00 +0100",
            body=(
                "Hallo Finn,\n\n"
                "anbei sende ich den aktuellen Entwurf unseres Wirtschaftsplans 2027 "
                "(Wirtschaftsplan_2027.xlsx).\n"
                "Bitte prüfe für alle vier bestehenden Kreditverträge die Financial Covenants "
                "zum Prüfungsstichtag 31.12.2027 und schicke mir eine Übersicht mit den Werten, "
                "dem jeweiligen Headroom sowie den genauen Fundstellen in den Verträgen und im Plan.\n\n"
                "Viele Grüße\n"
                "Katrin Möller\n"
                "Leitung Finanzen – Stadtwerke Musterstadt GmbH"
            ),
            attachment_bytes=xlsx_bytes,
            attachment_filename="Wirtschaftsplan_2027.xlsx",
        )
    )

    # 2. order_informal.eml
    (SAMPLES_DIR / "order_informal.eml").write_bytes(
        _build_eml(
            sender=sender_katrin,
            to=recipient_finn,
            subject="WP 27 – bitte Covenants checken",
            message_id="<order-2027-002@stadtwerke-musterstadt.example>",
            date="Fri, 06 Nov 2026 10:05:00 +0100",
            body=(
                "Hi Finn,\n\n"
                "kannst du bitte kurz die Covenants zum 31/12/27 auf Basis des angehängten Entwurfs "
                "durchrechnen? Besonders wichtig ist mir der Konsortialkredit.\n\n"
                "Danke & Gruß\n"
                "Katrin"
            ),
            attachment_bytes=xlsx_bytes,
            attachment_filename="Wirtschaftsplan_2027_Entwurf_v3.xlsx",
        )
    )

    # 3. order_english.eml
    (SAMPLES_DIR / "order_english.eml").write_bytes(
        _build_eml(
            sender=sender_katrin,
            to=recipient_finn,
            subject="Covenant test FY27",
            message_id="<order-2027-003@stadtwerke-musterstadt.example>",
            date="Fri, 06 Nov 2026 11:20:00 +0100",
            body=(
                "Hi Finn,\n\n"
                "Please run the financial covenant check for FY 2027 (as of 31.12.2027) using the "
                "attached business plan workbook and reply with the summary table and sources.\n\n"
                "Best regards,\n"
                "Katrin Möller"
            ),
            attachment_bytes=xlsx_bytes,
            attachment_filename="Wirtschaftsplan_2027.xlsx",
        )
    )

    # 4. no_attachment.eml
    (SAMPLES_DIR / "no_attachment.eml").write_bytes(
        _build_eml(
            sender=sender_katrin,
            to=recipient_finn,
            subject="Covenant-Prüfung Wirtschaftsplan 2027",
            message_id="<order-2027-004@stadtwerke-musterstadt.example>",
            date="Fri, 06 Nov 2026 12:00:00 +0100",
            body=(
                "Hallo Finn,\n\n"
                "bitte führe die Covenant-Prüfung für den Wirtschaftsplan 2027 durch.\n\n"
                "Viele Grüße\n"
                "Katrin Möller"
            ),
        )
    )

    # 5. wrong_attachment.eml
    (SAMPLES_DIR / "wrong_attachment.eml").write_bytes(
        _build_eml(
            sender=sender_katrin,
            to=recipient_finn,
            subject="Covenant-Prüfung Wirtschaftsplan 2027",
            message_id="<order-2027-005@stadtwerke-musterstadt.example>",
            date="Fri, 06 Nov 2026 12:30:00 +0100",
            body=(
                "Hallo Finn,\n\n"
                "anbei die Unterlagen für die Covenant-Prüfung 2027.\n\n"
                "Viele Grüße\n"
                "Katrin Möller"
            ),
            attachment_bytes=pdf_bytes,
            attachment_filename="Vertrag_1_Sparkasse_Investitionsdarlehen.pdf",
            maintype="application",
            subtype="pdf",
        )
    )

    # 6. unrelated.eml
    (SAMPLES_DIR / "unrelated.eml").write_bytes(
        _build_eml(
            sender="Thomas Becker <t.becker@stadtwerke-musterstadt.example>",
            to=recipient_finn,
            subject="Raumbuchung Donnerstag",
            message_id="<unrelated-2027-006@stadtwerke-musterstadt.example>",
            date="Fri, 06 Nov 2026 13:00:00 +0100",
            body=(
                "Hallo,\n\n"
                "können Sie einen Raum für Donnerstag buchen? Wir sind ca. 8 Personen.\n\n"
                "Beste Grüße\n"
                "Thomas Becker"
            ),
        )
    )


if __name__ == "__main__":
    main()

