"""§4.5.3 / §15.5 contact redaction — the mechanism behind the no-PII claim.

(§9.1 calls this file ``test_redact.py``; it is named for its module here so every
normalizer test sits under one ``tests/test_normalize_*.py`` glob.)
"""

from __future__ import annotations

import pytest

from core.models import Ref
from core.normalize.record import build_job_record
from core.normalize.redact import PLACEHOLDER, redact_description, redact_text
from core.normalize.salary import parse_salary

EMAILS = [
    "jane.doe@acme.com",
    "m.mueller@firma.de",
    "recruiting+jobs@sub.example.co.uk",
    "HR@Example.COM",
    "a_b-c%d@example.io",
]

PHONES = [
    "+49 30 1234567",
    "+1 (555) 123-4567",
    "+44 20 7946 0958",
    "+33 1 23 45 67 89",
    "0049 30 12345678",
    "(555) 123-4567",
    "555-123-4567",
    "555.123.4567",
]


@pytest.mark.parametrize("email", EMAILS)
def test_emails_are_removed(email):
    text, hit = redact_text(f"Questions? Reach out to {email}.")
    assert hit and email not in text and PLACEHOLDER in text


@pytest.mark.parametrize("phone", PHONES)
def test_international_phone_formats_are_removed(phone):
    text, hit = redact_text(f"Call us on {phone} for details.")
    assert hit and phone not in text and PLACEHOLDER in text


def test_the_german_sme_closing_line():
    body = "Ihre Ansprechpartnerin: Frau Müller, m.mueller@firma.de, Tel. +49 89 123456789"
    text, hit = redact_text(body)
    assert hit
    assert "@" not in text and "123456789" not in text
    assert "Frau Müller" in text  # a name is not a contact detail we can detect


def test_clean_bodies_are_untouched():
    body = "We pay $180,000 - $240,000 per year and match 401(k) up to 4%."
    assert redact_text(body) == (body, False)


def test_html_structure_survives():
    html = '<p>Mail <a href="mailto:a@b.io">a@b.io</a> or call +1 555 123 4567.</p>'
    text, hit = redact_text(html)
    assert hit
    expected = (
        f'<p>Mail <a href="mailto:{PLACEHOLDER}">{PLACEHOLDER}</a> or call {PLACEHOLDER}.</p>'
    )
    assert text == expected


def test_flag_is_only_true_when_something_was_removed():
    assert redact_description("<p>hi</p>", "hi", True)[2] is False
    assert redact_description("<p>a@b.io</p>", "a@b.io", True)[2] is True


def test_disabled_passes_the_body_through_untouched():
    html, text, flag = redact_description("<p>a@b.io</p>", "call +49 30 1234567", False)
    assert html == "<p>a@b.io</p>"
    assert text == "call +49 30 1234567"
    assert flag is None


def test_short_digit_runs_are_not_phone_numbers():
    for body in ("Team of 8 - 12 people", "+1 more", "Suite 200-300", "ISO 27001"):
        assert redact_text(body) == (body, False), body


def test_a_redacted_phone_number_cannot_be_parsed_as_a_salary():
    body = "Questions? Call the team on +1 555-123-4567 or write to jobs@acme.com."
    redacted, hit = redact_text(body)
    assert hit
    assert parse_salary({}, redacted).source is None


def test_record_redacts_before_the_salary_regex_runs():
    extracted = {
        "sourceId": "1",
        "title": "Engineer",
        "descriptionHtml": "<p>Call 555-123-4567. Pays $180,000 - $240,000 per year.</p>",
    }
    row = build_job_record(
        Ref("greenhouse", "acme"),
        extracted,
        {"includeDescription": True, "descriptionFormat": "both", "redactContacts": True},
    )
    assert row.descriptionRedacted is True
    assert "555-123-4567" not in (row.descriptionText or "")
    assert "555-123-4567" not in (row.descriptionHtml or "")
    assert (row.salaryMin, row.salaryMax, row.salarySource) == (180000, 240000, "parsed")


def test_record_leaves_the_body_alone_when_redaction_is_off():
    extracted = {"sourceId": "1", "title": "Engineer", "descriptionHtml": "<p>hr@acme.com</p>"}
    row = build_job_record(
        Ref("greenhouse", "acme"),
        extracted,
        {"includeDescription": True, "descriptionFormat": "both", "redactContacts": False},
    )
    assert "hr@acme.com" in row.descriptionHtml
    assert row.descriptionRedacted is None


def test_empty_input():
    assert redact_text(None) == (None, False)
    assert redact_text("") == ("", False)
