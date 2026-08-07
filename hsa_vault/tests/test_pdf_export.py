"""Audit packet tests, including the markup-injection regression.

Provider/category/description come from a vision model reading an image someone
else printed. reportlab's Paragraph parses a mini-XML markup, so that text is an
injection sink unless it is escaped.
"""

from datetime import date
from decimal import Decimal

import pytest

from core import pdf_export
from core.models import Receipt

HOSTILE = [
    pytest.param('<img src="/etc/passwd"/>', id="reportlab-img-local-file-read"),
    pytest.param('</para><img src="/etc/hosts"/>', id="para-breakout"),
    pytest.param("unclosed <b tag", id="malformed-markup-crashes-export"),
    pytest.param("<script>alert(1)</script>", id="angle-brackets"),
    pytest.param("A & B Medical", id="bare-ampersand"),
    pytest.param("<onDraw name='x'/>", id="ondraw-callback"),
]


def receipt(**kwargs) -> Receipt:
    defaults = dict(
        file_hash="a" * 64,
        service_date=date(2026, 3, 14),
        provider="Test Clinic",
        amount=Decimal("42.18"),
        category="Physician",
    )
    defaults.update(kwargs)
    return Receipt(**defaults)


# --- injection regression --------------------------------------------------


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_provider_text_cannot_break_or_exfiltrate(payload):
    """The export must still build, and must never embed a local file."""
    pdf = pdf_export.build_audit_packet([receipt(provider=payload)], 2026, "Owner")
    assert pdf.startswith(b"%PDF-")
    assert b"root:x:0:0" not in pdf  # /etc/passwd contents


@pytest.mark.parametrize("payload", HOSTILE)
def test_hostile_description_and_notes_are_safe(payload):
    pdf = pdf_export.build_audit_packet(
        [receipt(description=payload, notes=payload, patient=payload)], 2026, "Owner"
    )
    assert pdf.startswith(b"%PDF-")


def test_hostile_owner_name_is_escaped():
    pdf = pdf_export.build_audit_packet([receipt()], 2026, '<img src="/etc/passwd"/>')
    assert pdf.startswith(b"%PDF-")
    assert b"root:x:0:0" not in pdf


def test_safe_escapes_markup_characters():
    assert pdf_export.safe("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"
    assert pdf_export.safe(None) == ""


# --- normal behaviour ------------------------------------------------------


def test_packet_contains_only_the_requested_tax_year():
    receipts = [
        receipt(service_date=date(2026, 1, 1), provider="In scope"),
        receipt(service_date=date(2025, 1, 1), provider="Out of scope"),
    ]
    pdf = pdf_export.build_audit_packet(receipts, 2026, "Owner")
    assert pdf.startswith(b"%PDF-")


def test_archived_receipts_are_excluded_from_the_packet():
    csv_bytes = pdf_export.build_csv(
        [receipt(provider="Kept"), receipt(provider="Archived", deleted=True)], 2026
    )
    assert b"Kept" in csv_bytes
    assert b"Archived" not in csv_bytes


def test_csv_round_trips_the_amount_as_a_plain_string():
    csv_bytes = pdf_export.build_csv([receipt(amount=Decimal("1042.18"))], 2026)
    assert b"1042.18" in csv_bytes


def test_zip_contains_the_csv_index_even_with_no_images():
    import io
    import zipfile

    data = pdf_export.build_zip([receipt()], 2026, lambda _: (_ for _ in ()).throw(OSError()))
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert "HSA_2026_index.csv" in archive.namelist()


def test_a_receipt_with_no_image_still_gets_a_page():
    pdf = pdf_export.build_audit_packet([receipt(drive_file_id="")], 2026, "Owner")
    assert pdf.startswith(b"%PDF-")
