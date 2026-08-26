"""§4.5.3 / §15.5 contact redaction — the mechanism behind the no-PII claim.

(§9.1 calls this file ``test_redact.py``; it is named for its module here so every
normalizer test sits under one ``tests/test_normalize_*.py`` glob.)
"""

from __future__ import annotations

import re

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


def test_obfuscated_separators_still_redact():
    for form in ("jane (at) acme.com", "jane[at]acme.com", "jane (at)acme.com"):
        text, hit = redact_text(f"Write to {form} please")
        assert hit and "acme.com" not in text, form


def test_a_bare_domain_is_not_an_address_and_the_word_before_it_survives():
    """Cohere's live anti-fraud line, on all 143 jobs of the board (§10.2 live run)."""
    body = "Communications come from an @cohere.com or @cw.cohere email alias."
    text, hit = redact_text(body)
    assert (text, hit) == (body, False)


def test_a_thousands_group_is_not_an_international_dialling_prefix():
    """Crusoe's live compensation line: the `00` of "215,000" opened an international
    phone match that ran to "260.000", eating the pay range redaction runs ahead of."""
    body = "Compensation will be paid in the range of up to $215,000 - 260.000 + Bonus."
    assert redact_text(body) == (body, False)


def test_a_real_number_written_with_the_00_prefix_still_redacts():
    text, hit = redact_text("Call 0049 30 1234567 for details")
    assert hit and "0049" not in text


# --- V1 H1: national-format European phone numbers ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Rufen Sie uns an: 030 / 12 34 56 78",
        "Contact: 0721 9876543",
        "Bei Fragen: 0511-123456",
        "Tel: 040 123 456 78",
        "Bel ons op 020 123 4567",
    ],
)
def test_a_national_trunk_number_is_redacted(text: str):
    """V1 H1: `_PHONE` had only the `+`/`00` and NANP forms, so it missed the exact
    population §5.6/§5.8 name as the reason `redactContacts` defaults to on — German and
    Dutch SME ads write the trunk form, never `+49`."""
    redacted, hit = redact_text(text)
    assert hit and "[redacted]" in redacted
    assert not re.search(r"\d{5}", redacted), redacted


@pytest.mark.parametrize(
    "text",
    [
        "paid in the range of up to $215,000 - 260.000 + Bonus.",
        "EUR 90.000 - 110.000 brutto pro Jahr",
        "Salary 180000 - 220000 EUR",
        "Between 2019 and 2024 we grew",
        "a team of 0 - 5 engineers",
        "a ratio of 0.5 - 1.5 million",
    ],
)
def test_the_trunk_pattern_does_not_eat_money_or_years(text: str):
    """The leading `0` and the currency/comma lookbehind are what keep V1 H1's new branch
    off the salary ranges V2 BUG-4 already showed this module can destroy."""
    assert redact_text(text) == (text, False)
