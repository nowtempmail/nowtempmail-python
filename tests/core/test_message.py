"""Message bodies, link extraction and attachments.

``links()`` is the part with real logic, and the part with a security note
attached: these are URLs a stranger put in an email, and a password-reset link
among them is a live credential with a working expiry.
"""

from __future__ import annotations

import pytest
from helpers.mock_api import MOCK_KEY, MockApi

from nowtempmail import NowTempMail
from nowtempmail.message import Message


def build(**overrides: object) -> Message:
    wire = {
        "id": "msg_0001",
        "from": "Acme <noreply@acme.test>",
        "to": "someone@allogebra.test",
        "subject": "Your code",
        "receivedAt": 1_786_097_700,
        "text": None,
        "html": None,
        "verificationCodes": ["482913"],
        "attachments": [],
        **overrides,
    }
    return Message(None, "someone@allogebra.test", wire)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Shape                                                                        #
# --------------------------------------------------------------------------- #


def test_fields_are_transcribed_from_the_wire() -> None:
    message = build()
    assert message.id == "msg_0001"
    assert message.sender == "Acme <noreply@acme.test>"
    assert message.subject == "Your code"
    assert message.verification_codes == ("482913",)


def test_received_at_is_timezone_aware() -> None:
    # Every boundary in this SDK is a comparison, and comparing a naive datetime
    # to an aware one raises.
    assert build().received_at.tzinfo is not None


def test_code_is_the_first_extracted_code_or_none() -> None:
    assert build().code == "482913"
    assert build(verificationCodes=["111", "222"]).code == "111"
    assert build(verificationCodes=[]).code is None


def test_verification_codes_is_an_immutable_tuple() -> None:
    with pytest.raises(AttributeError):
        build().verification_codes.append("999")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# links()                                                                      #
# --------------------------------------------------------------------------- #


def test_links_are_found_in_the_text_body() -> None:
    message = build(text="Reset here: https://acme.test/r/abc123 and then log in.")
    assert message.links() == ["https://acme.test/r/abc123"]


def test_trailing_sentence_punctuation_is_not_part_of_the_url() -> None:
    # A bare URL in prose usually ends at a full stop. Keeping it produces a
    # link that 404s, which reads as "the app sent a broken link".
    message = build(text="Go to https://acme.test/verify.")
    assert message.links() == ["https://acme.test/verify"]


def test_an_href_ends_at_the_quote() -> None:
    message = build(html='<a href="https://acme.test/x?t=1">Click</a>')
    assert message.links() == ["https://acme.test/x?t=1"]


def test_text_links_come_before_html_links_and_duplicates_collapse() -> None:
    message = build(
        text="see https://acme.test/one and https://acme.test/two",
        html='<a href="https://acme.test/two">t</a><a href="https://acme.test/three">3</a>',
    )
    assert message.links() == [
        "https://acme.test/one",
        "https://acme.test/two",
        "https://acme.test/three",
    ]


def test_no_body_means_no_links_rather_than_an_error() -> None:
    assert build().links() == []


def test_a_query_string_survives_intact() -> None:
    # The token is usually in the query, so truncating it produces a link that
    # looks right and does not work.
    url = "https://acme.test/reset?token=abc.def-ghi_jkl&uid=42"
    assert build(text=f"Reset: {url}").links() == [url]


def test_http_and_https_are_both_found() -> None:
    message = build(text="http://insecure.test/a https://secure.test/b")
    assert message.links() == ["http://insecure.test/a", "https://secure.test/b"]


def test_non_http_schemes_are_ignored() -> None:
    message = build(text="mailto:someone@acme.test ftp://files.test/x https://ok.test/y")
    assert message.links() == ["https://ok.test/y"]


# --------------------------------------------------------------------------- #
# Attachments                                                                  #
# --------------------------------------------------------------------------- #


def test_attachment_metadata_is_exposed() -> None:
    message = build(
        attachments=[
            {"id": "att_1", "filename": "invoice.pdf", "contentType": "application/pdf", "size": 91}
        ]
    )
    assert len(message.attachments) == 1
    attachment = message.attachments[0]
    assert attachment.filename == "invoice.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.size == 91


def test_the_repr_names_the_message_without_dumping_the_body() -> None:
    # A Message routinely ends up in a pytest fixture repr or a log line.
    text = repr(build(text="secret reset link https://acme.test/r/abc"))
    assert "msg_0001" in text
    assert "acme.test/r/abc" not in text


# --------------------------------------------------------------------------- #
# Against the live-ish API                                                     #
# --------------------------------------------------------------------------- #


def test_a_fetched_message_carries_its_body(api: MockApi) -> None:
    ntm = NowTempMail(MOCK_KEY, base_url=api.url)
    box = ntm.mailboxes.create()
    api.deliver(box.address, text="Code 482913. Reset: https://acme.test/r/zz")
    summary = box.page().messages[0]
    message = box.get(summary.id)
    assert message.text is not None
    assert message.code == "482913"
    assert message.links() == ["https://acme.test/r/zz"]
