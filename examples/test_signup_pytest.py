"""The shape almost every suite wants: a shared inbox and a per-test code.

Run with ``NTM_API_KEY`` set. Nothing here needs a browser -- swap the fake
``sign_up`` for your own app, or for a Playwright page.
"""

from __future__ import annotations


def sign_up(email: str) -> None:
    """Stand-in for whatever your app does. Yours will send real mail."""
    raise NotImplementedError("point this at your application")


def test_signup_sends_a_code(ntm_inbox):
    sign_up(ntm_inbox.address)

    # Only mail from THIS test is a candidate -- the fixture watermarks the
    # wait, so a code an earlier test triggered is invisible here.
    code = ntm_inbox.wait_for_code(timeout=120)

    assert code.isdigit() and len(code) == 6


def test_two_senders_in_one_suite(ntm_inbox):
    """When two tests trigger mail close together, disambiguate on content.

    The watermark is second-granular, so it cannot separate tests that run
    within about a second of each other. ``sender`` and ``subject`` can.
    """
    sign_up(ntm_inbox.address)
    code = ntm_inbox.wait_for_code(sender="noreply@acme.test", subject="verify")
    assert code


def test_the_reset_flow_must_not_accept_the_old_code(ntm_inbox):
    """``ignore_existing`` is for resends, where the stale code is the bug."""
    sign_up(ntm_inbox.address)
    first = ntm_inbox.wait_for_code()

    request_resend(ntm_inbox.address)
    second = ntm_inbox.wait_for_code(ignore_existing=True, timeout=120)

    assert second != first


def test_the_email_itself(ntm_inbox):
    """``wait_for_message`` when you need the body, not just the code."""
    sign_up(ntm_inbox.address)
    message = ntm_inbox.wait_for_message(subject="Welcome")

    assert "Welcome" in message.subject
    assert message.links(), "the welcome mail should link somewhere"
    # Careful with links in logs: a magic link is a live credential.


def test_an_empty_inbox_needs_a_mailbox_of_its_own(ntm_fresh_inbox):
    """The case a shared inbox genuinely cannot serve.

    Costs one mailbox out of the daily budget, which is never refunded -- so use
    it only where sharing is actually wrong, not by default.
    """
    assert ntm_fresh_inbox.page().messages == ()


def request_resend(email: str) -> None:
    raise NotImplementedError("point this at your application")
