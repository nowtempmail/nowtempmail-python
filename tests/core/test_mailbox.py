"""The watch loop.

Every assertion here corresponds to a decision documented at the top of
``mailbox.py``, and every one of those decisions came from a live inbox
misbehaving rather than from what read nicely.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from helpers.mock_api import MOCK_KEY, MockApi

from nowtempmail import Mailbox, MessageFilter, NowTempMail
from nowtempmail.errors import TransportError, WaitTimeoutError

FAST = {"poll_interval": 0.02, "timeout": 2.0}


def client(api: MockApi, **kwargs: object) -> NowTempMail:
    return NowTempMail(MOCK_KEY, base_url=api.url, **kwargs)  # type: ignore[arg-type]


def inbox(api: MockApi) -> tuple[NowTempMail, Mailbox]:
    ntm = client(api)
    return ntm, ntm.mailboxes.create()


# --------------------------------------------------------------------------- #
# The high-water mark                                                          #
# --------------------------------------------------------------------------- #


def test_mail_that_landed_before_the_wait_started_is_still_found(api: MockApi) -> None:
    # The whole reason the default boundary is created_at and not "now": a
    # signup form submits, the mail arrives in 300ms, and the test gets around
    # to waiting a second later. Defaulting to call time makes that message
    # invisible and produces a flake nobody can reproduce.
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["112233"])
    time.sleep(0.05)
    assert box.wait_for_code(**FAST) == "112233"


def test_ignore_existing_skips_a_pre_existing_code(api: MockApi) -> None:
    # The resend case, where the stale code is the bug.
    #
    # The new message is stamped a full second ahead deliberately. `received_at`
    # on the wire has ONE-SECOND granularity, so a message delivered 200ms after
    # an `ignore_existing` wait begins carries the same second as the boundary
    # and is genuinely ambiguous -- it could have arrived before the call or
    # after it, and nothing in the data distinguishes them. A test that
    # delivered it "shortly after" would be asserting a property the wire cannot
    # support, and would fail roughly whenever the call landed late in a second.
    # See MessageFilter.ignore_existing for which way that ambiguity is resolved.
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["000000"])

    def deliver_later() -> None:
        time.sleep(0.2)
        api.deliver(box.address, codes=["999888"], received_at=int(time.time()) + 1)

    threading.Thread(target=deliver_later, daemon=True).start()
    assert box.wait_for_code(ignore_existing=True, **FAST) == "999888"


def test_ignore_existing_resolves_a_same_second_ambiguity_by_excluding(api: MockApi) -> None:
    # The deliberate choice, asserted so it cannot drift: within the one-second
    # window where "arrived just before the call" and "arrived just after" are
    # indistinguishable, mail is EXCLUDED. Including it would risk handing back
    # the stale code that ignore_existing exists to avoid -- a silent wrong pass.
    # Excluding it produces a loud timeout instead, which is the better failure.
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["000000"], received_at=int(time.time()))
    with pytest.raises(WaitTimeoutError) as excinfo:
        box.wait_for_code(ignore_existing=True, poll_interval=0.02, timeout=0.3)
    assert excinfo.value.messages_seen == 1


def test_an_explicit_after_beats_the_default(api: MockApi) -> None:
    ntm, box = inbox(api)
    old = int(time.time()) - 3600
    api.deliver(box.address, codes=["old111"], received_at=old)
    api.deliver(box.address, codes=["new222"])
    boundary = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert box.wait_for_code(after=boundary, **FAST) == "new222"


def test_mail_older_than_the_boundary_is_invisible(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["old111"], received_at=int(time.time()) - 3600)
    with pytest.raises(WaitTimeoutError) as excinfo:
        box.wait_for_code(after=datetime.now(timezone.utc), **FAST)
    # It was seen but rejected -- that is the number that separates the two bugs.
    assert excinfo.value.messages_seen == 1


# --------------------------------------------------------------------------- #
# Timeout reporting                                                            #
# --------------------------------------------------------------------------- #


def test_a_timeout_reports_zero_when_nothing_arrived(api: MockApi) -> None:
    ntm, box = inbox(api)
    with pytest.raises(WaitTimeoutError) as excinfo:
        box.wait_for_code(poll_interval=0.02, timeout=0.3)
    error = excinfo.value
    assert error.messages_seen == 0
    assert "nothing arrived" in error.message
    assert error.address == box.address
    assert error.code == "timeout"


def test_a_timeout_distinguishes_mail_without_a_code(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, codes=[], subject="Just a newsletter")
    with pytest.raises(WaitTimeoutError) as excinfo:
        box.wait_for_code(poll_interval=0.02, timeout=0.3)
    assert excinfo.value.messages_seen == 1
    assert "none matched" in excinfo.value.message


def test_a_wait_that_times_out_deregisters_its_watcher(api: MockApi) -> None:
    ntm, box = inbox(api)
    with pytest.raises(WaitTimeoutError):
        box.wait_for_code(poll_interval=0.02, timeout=0.2)
    assert ntm.http.watchers == 0


# --------------------------------------------------------------------------- #
# Filters                                                                      #
# --------------------------------------------------------------------------- #


def test_sender_and_subject_match_case_insensitively_as_substrings(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, sender="GitHub <noreply@github.com>", subject="Sign in", codes=["1"])
    api.deliver(box.address, sender="Acme <hi@acme.test>", subject="Welcome", codes=["2"])
    assert box.wait_for_code(sender="github", **FAST) == "1"
    assert box.wait_for_code(subject="WELCOME", **FAST) == "2"


def test_a_compiled_pattern_is_searched(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, subject="Code 482913 expires soon", codes=["482913"])
    assert box.wait_for_code(subject=re.compile(r"\d{6}"), **FAST) == "482913"


def test_a_stateful_global_pattern_does_not_skip_alternate_calls(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, subject="Verify now", codes=["424242"])
    pattern = re.compile(r"Verify")
    for _ in range(3):
        assert box.wait_for_code(subject=pattern, **FAST) == "424242"


def test_every_supplied_filter_must_pass(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, sender="GitHub <x@github.com>", subject="Welcome", codes=["7"])
    with pytest.raises(WaitTimeoutError):
        box.wait_for_code(
            sender="github", subject="password reset", poll_interval=0.02, timeout=0.3
        )


def test_an_arbitrary_predicate_is_applied_last(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["1234"])
    api.deliver(box.address, codes=["123456"])
    got = box.wait_for_code(match=lambda m: len(m.verification_codes[0]) == 6, **FAST)
    assert got == "123456"


def test_a_filter_object_and_keywords_cannot_be_mixed(api: MockApi) -> None:
    ntm, box = inbox(api)
    with pytest.raises(TypeError):
        box.wait_for_code(MessageFilter(timeout=1), sender="x")


def test_filter_replace_does_not_mutate_the_original() -> None:
    original = MessageFilter(sender="github", timeout=90)
    copy = original.replace(after=datetime.now(timezone.utc))
    assert original.after is None
    assert copy.after is not None
    assert copy.sender == "github" and copy.timeout == 90


# --------------------------------------------------------------------------- #
# Newest first, and dedupe                                                     #
# --------------------------------------------------------------------------- #


def test_the_freshest_code_wins_on_a_resend(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["111111"])
    api.deliver(box.address, codes=["222222"])  # newest, at the front
    assert box.wait_for_code(**FAST) == "222222"


def test_the_same_message_is_never_counted_twice(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, codes=[], subject="no code here")
    with pytest.raises(WaitTimeoutError) as excinfo:
        box.wait_for_code(poll_interval=0.02, timeout=0.4)
    # Several polls, one message. messages_seen counts messages, not reads.
    assert excinfo.value.messages_seen == 1


# --------------------------------------------------------------------------- #
# wait_for_message                                                             #
# --------------------------------------------------------------------------- #


def test_wait_for_message_returns_bodies(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, text="Reset here: https://acme.test/r/abc123 .", codes=["5"])
    message = box.wait_for_message(**FAST)
    assert message.text is not None and "Reset here" in message.text
    assert message.code == "5"
    assert message.links() == ["https://acme.test/r/abc123"]


def test_wait_for_code_never_fetches_a_body(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, codes=["424242"])
    before = len(api.requests)
    box.wait_for_code(**FAST)
    fetched = [r for r in api.requests[before:] if re.search(r"/messages/msg_", r.path)]
    assert fetched == [], "wait_for_code fetched a message body"


# --------------------------------------------------------------------------- #
# Pagination and history                                                       #
# --------------------------------------------------------------------------- #


def test_page_returns_newest_first(api: MockApi) -> None:
    ntm, box = inbox(api)
    api.deliver(box.address, subject="first")
    api.deliver(box.address, subject="second")
    page = box.page()
    assert [m.subject for m in page.messages] == ["second", "first"]
    assert page.has_more is False


def test_list_auto_paginates_over_history(api: MockApi) -> None:
    ntm, box = inbox(api)
    for n in range(7):
        api.deliver(box.address, subject=f"msg {n}")
    seen = list(box.list(limit=2))
    assert len(seen) == 7
    assert len({m.id for m in seen}) == 7


def test_the_watch_loop_never_paginates(api: MockApi) -> None:
    # New mail arrives at the FRONT of the list, so a cursor walk started at
    # page one can miss a message that lands mid-walk.
    ntm, box = inbox(api)
    for n in range(40):
        api.deliver(box.address, subject=f"filler {n}", codes=[])
    with pytest.raises(WaitTimeoutError):
        box.wait_for_code(poll_interval=0.02, timeout=0.3)
    assert all("cursor" not in (r.path or "") for r in api.requests)


# --------------------------------------------------------------------------- #
# Cancellation                                                                 #
# --------------------------------------------------------------------------- #


def test_a_cancel_event_stops_a_long_wait(api: MockApi) -> None:
    ntm, box = inbox(api)
    cancel = threading.Event()
    threading.Timer(0.15, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(TransportError):
        box.wait_for_code(timeout=60, poll_interval=0.05, cancel=cancel)
    assert time.monotonic() - started < 10


def test_an_already_set_cancel_stops_before_the_first_request(api: MockApi) -> None:
    ntm, box = inbox(api)
    before = len(api.requests)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(TransportError):
        box.wait_for_code(cancel=cancel, **FAST)
    assert len(api.requests) == before


# --------------------------------------------------------------------------- #
# Metadata                                                                     #
# --------------------------------------------------------------------------- #


def test_refresh_does_not_destroy_fields_the_narrow_read_omits(api: MockApi) -> None:
    # GET /v1/mailboxes/{address} returns neither domain, ttlSeconds, reserved
    # nor webhookUrl. Overwriting them with the None it carries would silently
    # lose what creation told us.
    ntm, box = inbox(api)
    assert box.ttl_seconds == 86_400
    domain, ttl, reserved = box.domain, box.ttl_seconds, box.reserved
    box.refresh()
    assert (box.domain, box.ttl_seconds, box.reserved) == (domain, ttl, reserved)


def test_refresh_updates_counts_and_timestamps(api: MockApi) -> None:
    ntm, box = inbox(api)
    assert box.message_count == 0
    api.deliver(box.address)
    box.refresh()
    assert box.message_count == 1
    assert box.last_message_at is not None
    assert box.last_message_at.tzinfo is not None


def test_timestamps_are_timezone_aware(api: MockApi) -> None:
    # Every boundary in this SDK is a comparison, and comparing a naive datetime
    # to an aware one raises.
    ntm, box = inbox(api)
    assert box.created_at.tzinfo is not None
    assert box.expires_at.tzinfo is not None
    assert box.expires_at > box.created_at
    assert 86_000 < box.expires_in <= 86_400


def test_deleting_a_message_decrements_the_local_count(api: MockApi) -> None:
    ntm, box = inbox(api)
    message = api.deliver(box.address)
    box.refresh()
    assert box.message_count == 1
    box.delete_message(message["id"])
    assert box.message_count == 0


def test_requests_from_one_handle_are_serialised(api: MockApi) -> None:
    # Two overlapping polls double the request rate for no extra freshness and
    # bring the 429 forward.
    ntm, box = inbox(api)
    api.deliver(box.address)
    overlaps = 0
    active = 0
    lock = threading.Lock()

    original = box._http.request

    def counting(*args: object, **kwargs: object) -> object:
        nonlocal overlaps, active
        with lock:
            active += 1
            if active > 1:
                overlaps += 1
        try:
            return original(*args, **kwargs)  # type: ignore[arg-type]
        finally:
            with lock:
                active -= 1

    box._http.request = counting  # type: ignore[assignment,method-assign]
    threads = [threading.Thread(target=box.page) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlaps == 0
