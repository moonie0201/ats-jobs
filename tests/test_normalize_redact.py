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
    assert "Frau Müller" not in text
    assert text.startswith("Ihre Ansprechpartnerin: [redacted]")  # the label survives


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


# --- includeDescription flip: label-anchored names and non-email contact channels -------
#
# The flip of `includeDescription` to default `true` is conditional on these two classes
# being caught. Every string below is one of L3_privacy.md §P1's observed EU-board lines,
# which that report measured as surviving the email/phone-only redactor verbatim.


@pytest.mark.parametrize(
    ("body", "gone"),
    [
        ("Ihre Ansprechpartnerin: Frau Sabine Müller, Personalleitung.", "Sabine Müller"),
        ("Ansprechpartner: Thomas Becker", "Thomas Becker"),
        ("Ihr Ansprechpartner: Dr. Peter Wagner, Inhaber", "Peter Wagner"),
        ("Kontakt: Anna Schmidt.", "Anna Schmidt"),
        ("Kontaktperson: Herr Thomas Becker\nWir freuen uns.", "Thomas Becker"),
        ("Contactpersoon: Mevr. Anke de Vries.", "Anke de Vries"),
        ("Neem contact op met Peter van der Berg.", "Peter van der Berg"),
        ("Contact : Marie Dubois, Responsable RH", "Marie Dubois"),
        ("Contact: Anna Schmidt", "Anna Schmidt"),
        ("Hiring manager: Anna Schmidt.", "Anna Schmidt"),
        ("Recruiter: Jane Doe\nApply today.", "Jane Doe"),
        ("Questions? Contact Anna Schmidt.", "Anna Schmidt"),
        ("Bei Fragen wenden Sie sich an Herrn Thomas Becker.", "Thomas Becker"),
        ("Ihre Ansprechpartnerin: <strong>Frau Müller</strong>", "Frau Müller"),
    ],
)
def test_a_labelled_contact_name_is_redacted(body: str, gone: str):
    text, hit = redact_text(body)
    assert hit and gone not in text, text
    assert PLACEHOLDER in text


def test_the_label_survives_so_the_buyer_can_see_what_was_taken():
    text, _ = redact_text("Ihre Ansprechpartnerin: Frau Sabine Müller, Personalleitung.")
    assert text == "Ihre Ansprechpartnerin: [redacted], Personalleitung."


@pytest.mark.parametrize(
    "body",
    [
        "Message our recruiter Jane Doe on linkedin.com/in/jane-doe-84b21a/",
        "Bei Fragen: https://www.linkedin.com/in/thomas-becker-1a2b3c",
        "Wenden Sie sich an xing.com/profile/Thomas_Becker12",
        "Book a chat with me: calendly.com/tom-thomassen/30min",
        "Bewirb dich per WhatsApp unter wa.me/4915112345678",
        "Ping me on https://t.me/sabine_mueller",
        "Telegram @sabine_mueller",
        "WhatsApp: @recruiting.anna",
        "Add me on LinkedIn @jane.doe",
    ],
)
def test_a_non_email_contact_channel_is_redacted(body: str):
    text, hit = redact_text(body)
    assert hit and PLACEHOLDER in text
    for token in ("linkedin.com/in", "xing.com/profile", "calendly.com/", "wa.me/", "t.me/"):
        assert token not in text, text
    assert "@sabine_mueller" not in text and "@jane.doe" not in text


@pytest.mark.parametrize(
    "body",
    [
        # A name in running prose: no label, no redaction. The honest gap, held by a test
        # so it cannot be closed by accident with a name recogniser that shreds ad bodies.
        "You will report directly to Anna Schmidt, our VP of Engineering.",
        "This role works closely with Product and with Marie Dubois in Paris.",
        # Job titles that merely contain a label word.
        "We are hiring a Contact Center Manager for our Berlin office.",
        "Our Contact-Center Manager leads a team of twelve.",
        "Reporting line: Senior Recruiter Operations, Talent Acquisition",
        # German capitalised common nouns after a real label.
        "Kontakt: Unser Recruiting Team freut sich auf Ihre Bewerbung",
        "Contact: die Personalabteilung der Firma",
        # Employer social links are not personal contact channels.
        "Follow us at linkedin.com/company/acme for updates.",
        "See https://www.xing.com/pages/acme-gmbh for our company page.",
        # Ordinary prose with capitalised words mid-sentence.
        "You will join the Berlin Platform Team and own delivery end to end.",
        "We use Python, Kubernetes and Google Cloud Platform every day.",
    ],
)
def test_ordinary_prose_and_job_titles_survive(body: str):
    assert redact_text(body) == (body, False), body


def test_the_flag_fires_for_a_name_only_body():
    """`descriptionRedacted` has to be true for the new rules too, not just for emails."""
    html, text, flag = redact_description(
        "<p>Ansprechpartner: Thomas Becker</p>", "Ansprechpartner: Thomas Becker", True
    )
    assert flag is True
    assert "Thomas Becker" not in html and "Thomas Becker" not in text


def test_role_mailboxes_are_removed_like_any_other_address():
    """Not because they are personal data — they are not — but because `_EMAIL` cannot
    tell them apart, `test_adapter_lever.py` already asserts `accommodations@` goes, and
    the buyer has `applyUrl` for applying. Documented rather than exempted."""
    text, hit = redact_text("Send your CV to jobs@acme.com or careers@acme.com.")
    assert hit and "@acme.com" not in text


# --- observed on the 2026-08-27 live sweep (1,164 EU ad bodies, Personio + Recruitee) ---


def test_the_live_dutch_recruiter_line():
    """recruitee:alliade, live. Three things had to fire together: the role mailbox, the
    labelled name after `recruiter:`, and a mobile written with an EN DASH — which the
    ASCII-only separator class shipped whole until this sweep found it."""
    body = (
        "Neem dan contact op via recruitment@alliade.nl of bel/app met onze "
        "corporate recruiter: Annemiek Noord, via 06–29140410"
    )
    text, hit = redact_text(body)
    assert hit
    assert "Annemiek Noord" not in text
    assert "recruitment@alliade.nl" not in text
    assert "29140410" not in text, "en-dash mobile must not survive"


def test_the_live_german_whatsapp_deep_link():
    """personio:1sp-agency, live, on 131 of its rows: a WhatsApp link whose path *is* the
    recruiter's mobile number. The query tail goes with it."""
    body = "Schicke das Stichwort #bewerbung an: 030 5093 07522 oder klicke direkt: https://wa.me/4930509307522?text=%23bewerbung"
    text, hit = redact_text(body)
    assert hit
    assert "4930509307522" not in text and "wa.me" not in text
    assert "%23bewerbung" not in text
    assert "#bewerbung" in text, "the keyword the applicant has to send is not a contact"


def test_the_live_dutch_intermediary_line():
    body = (
        "Stuur dan je cv naar recruitment@1klick.nl of neem contact op met "
        "Bart Mulleneers, partner bij 1KLICK."
    )
    text, hit = redact_text(body)
    assert hit and "Bart Mulleneers" not in text
    assert "partner bij 1KLICK" in text, "the firm is not a natural person"
