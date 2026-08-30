"""What reaches a CI report, and what must not.

A report is zipped into an artifact, downloaded by whoever has repo read access,
and pasted into issues. A password-reset link inside one is a live credential
with a working expiry.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nowtempmail.message import Message
from nowtempmail.pytest_plugin import redact_for_report
from nowtempmail.types import MessageSummary

SECRET_LINK = "https://acme.test/reset?token=live-credential-abc123"


def summary() -> MessageSummary:
    return MessageSummary(
        id="msg_0001",
        sender="Acme <noreply@acme.test>",
        subject="Reset your password",
        received_at=datetime.fromtimestamp(1_786_097_700, tz=timezone.utc),
        attachment_count=0,
        verification_codes=("482913",),
    )


def message() -> Message:
    return Message(
        None,  # type: ignore[arg-type]
        "someone@allogebra.test",
        {
            "id": "msg_0002",
            "from": "Acme <noreply@acme.test>",
            "to": "someone@allogebra.test",
            "subject": "Reset your password",
            "receivedAt": 1_786_097_700,
            "text": f"Click here: {SECRET_LINK}",
            "html": f'<a href="{SECRET_LINK}">Reset</a>',
            "verificationCodes": ["482913"],
            "attachments": [],
        },
    )


def test_a_body_never_reaches_the_report_by_default() -> None:
    record = redact_for_report(message())
    assert record["redacted"] is True
    assert "text" not in record
    assert "html" not in record
    assert "links" not in record
    assert SECRET_LINK not in str(record)


def test_the_link_count_survives_but_the_links_do_not() -> None:
    # Enough to debug "the mail had no link" without handing over the link.
    record = redact_for_report(message())
    assert record["link_count"] == 1
    assert record["body_chars"] > 0


def test_what_a_report_actually_needs_is_kept() -> None:
    # Sender, subject, timestamp and codes are enough to debug "the wrong mail
    # matched", which is the only question a report is really asked.
    record = redact_for_report(summary())
    assert record["from"] == "Acme <noreply@acme.test>"
    assert record["subject"] == "Reset your password"
    assert record["received_at"].endswith("+00:00")
    assert record["id"] == "msg_0001"


def test_codes_are_recorded_deliberately_even_though_they_are_secrets() -> None:
    # Single-use, already consumed by the time the report is read, and the one
    # value you cannot debug an OTP flow without.
    assert redact_for_report(summary())["verification_codes"] == ["482913"]
    assert redact_for_report(message())["verification_codes"] == ["482913"]


def test_attach_body_opts_in_and_says_what_that_means() -> None:
    record = redact_for_report(message(), attach_body=True)
    assert record["redacted"] is False
    assert SECRET_LINK in (record["text"] or "")
    assert record["links"] == [SECRET_LINK]
    assert "live credential" in record["note"]


def test_the_redacted_note_says_how_to_get_the_body() -> None:
    note = redact_for_report(message())["note"]
    assert "ntm_attach_body" in note


def test_a_summary_with_no_body_says_so_without_pretending_it_was_redacted() -> None:
    # wait_for_code resolves from the summary and never fetches a body, so
    # body_chars is genuinely zero rather than withheld.
    record = redact_for_report(summary())
    assert record["body_chars"] == 0
    assert record["link_count"] == 0
