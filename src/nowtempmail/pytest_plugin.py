"""pytest fixtures: a live inbox and the code that lands in it.

::

    def test_signup(page, ntm_inbox):
        page.goto("/signup")
        page.fill("#email", ntm_inbox.address)
        page.click("text=Create account")
        page.fill("#code", ntm_inbox.wait_for_code())   # only this test's mail

Four decisions here are counter-intuitive and all four are load-bearing.

1. **``ntm_inbox`` is session-scoped and shared.** A per-test mailbox is the
   obvious design and it is the wrong one -- see :func:`ntm_inbox`.
2. **Because it is shared, waits are watermarked per test.** An autouse fixture
   re-stamps a boundary at the start of every test and the waits default to it.
   Without that, test #7 happily resolves with the code test #3 triggered and
   the suite passes while testing nothing. See :func:`compute_watermark`.
3. **Report attachments are redacted by default.** Subject, sender, timestamp
   and extracted codes go into the report; bodies and links do not. See
   :func:`redact_for_report`.
4. **This module must be inert without a key.** It is loaded into *every* pytest
   session in the environment, including suites that have never heard of
   NowTempMail. See the note above :func:`ntm_client`.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

import pytest

from .client import NowTempMail
from .mailbox import Mailbox, MessageFilter
from .message import Message
from .types import MessagePage, MessageSummary

__all__ = [
    "DEFAULT_WATERMARK_SKEW_MS",
    "TestInbox",
    "apply_watermark",
    "compute_watermark",
    "redact_for_report",
]

# --------------------------------------------------------------------------- #
# Watermarking                                                                 #
# --------------------------------------------------------------------------- #

#: One second.
#:
#: ``received_at`` on the wire has one-second granularity and a CI runner's clock
#: is not the API's clock, so a watermark placed at exactly the test's start
#: instant can sit *after* the timestamp of mail that test caused. That failure
#: mode is a timeout with ``messages_seen > 0``, which reads as "the mail never
#: arrived" and is the least debuggable outcome available.
DEFAULT_WATERMARK_SKEW_MS = 1_000


def compute_watermark(
    started_at: float | None = None, skew_ms: int = DEFAULT_WATERMARK_SKEW_MS
) -> datetime:
    """The per-test high-water mark: the start of the second the test began in,
    minus ``skew_ms``.

    **Flooring to the second matters as much as the skew does.** Comparing a
    microsecond-precision local timestamp against a second-precision server one
    discards up to 999ms of mail for no reason.

    :param started_at: Unix seconds. Defaults to now.
    """
    now = time.time() if started_at is None else started_at
    skew = skew_ms / 1000 if skew_ms and skew_ms > 0 else 0
    floored = int(max(0.0, now - skew))
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def apply_watermark(spec: MessageFilter, watermark: datetime) -> MessageFilter:
    """Default a filter's ``after`` to the watermark, overriding nothing explicit.

    ``ignore_existing`` wins outright: it means "only mail from now on", which is
    strictly narrower than the watermark. An explicit ``after`` wins too -- a
    caller who names a boundary has out-argued the fixture.
    """
    if spec.ignore_existing or spec.after is not None:
        return spec
    return spec.replace(after=watermark)


# --------------------------------------------------------------------------- #
# Report redaction                                                             #
# --------------------------------------------------------------------------- #

_REDACTED_NOTE = (
    "Bodies and links are omitted. A CI report is a shareable artifact and a "
    "password-reset link inside one is a live credential, not a log line. Set "
    "ntm_attach_body = true in your pytest config to include them."
)

_UNREDACTED_NOTE = (
    "ntm_attach_body is true, so this record contains the full message body and "
    "every link in it. Treat this report as a secret: a password-reset or "
    "magic-link URL in here is a live credential that anyone with the artifact "
    "can spend."
)


def redact_for_report(
    message: MessageSummary | Message, *, attach_body: bool = False
) -> dict[str, Any]:
    """Build the report record for a matched message.

    **Redacted by default.** Sender, subject, ``received_at`` and the extracted
    codes are enough to debug "the wrong mail matched", which is the only
    question a report is really asked. Bodies and links are not, and a report is
    the wrong place to keep them: it is zipped into a CI artifact, downloaded by
    whoever has repo read access, and pasted into issues. A password-reset link
    inside one is a live credential with a working expiry.

    The codes are recorded deliberately even though they are secrets -- a
    verification code is single-use, already consumed by the time the report is
    read, and it is the one value you cannot debug an OTP flow without.
    """
    text = getattr(message, "text", None)
    html = getattr(message, "html", None)
    body_fetched = text is not None or html is not None
    links: list[str] = []
    if body_fetched and hasattr(message, "links"):
        try:
            links = message.links()
        except Exception:
            links = []

    record: dict[str, Any] = {
        "redacted": not attach_body,
        "id": getattr(message, "id", None),
        "from": message.sender,
        "subject": message.subject,
        "received_at": message.received_at.isoformat(),
        "verification_codes": list(message.verification_codes),
        # How many links the body contained. The links themselves are the secret.
        "link_count": len(links),
        "body_chars": len(text or "") + len(html or ""),
        "note": _UNREDACTED_NOTE if attach_body else _REDACTED_NOTE,
    }
    if attach_body:
        record["text"] = text
        record["html"] = html
        record["links"] = links
    return record


# --------------------------------------------------------------------------- #
# The inbox handle                                                             #
# --------------------------------------------------------------------------- #


class _InboxContext:
    """Mutable per-session cell.

    ``ntm_inbox`` is session-scoped, so it cannot know which test is running; the
    per-test autouse fixture writes that here and the inbox reads it. **This
    indirection is the watermark mechanism.**
    """

    __slots__ = ("watermark", "record", "attach_body", "skew_ms")

    def __init__(self, *, attach_body: bool, skew_ms: int) -> None:
        self.watermark = datetime.fromtimestamp(0, tz=timezone.utc)
        self.record: Callable[[str, object], None] | None = None
        self.attach_body = attach_body
        self.skew_ms = skew_ms


class TestInbox:
    """A :class:`~nowtempmail.mailbox.Mailbox` that knows which test is looking at it.

    Every wait defaults its ``after`` boundary to the current test's watermark,
    and every match is recorded on the test report -- redacted unless
    ``ntm_attach_body`` says otherwise. Reach through to :attr:`mailbox` for
    anything this wrapper does not forward.
    """

    #: pytest collects any class named ``Test*`` that it finds in a test module.
    #: The moment someone writes ``from nowtempmail.pytest_plugin import
    #: TestInbox`` to annotate a fixture, this class becomes a collection
    #: candidate and pytest emits "cannot collect test class TestInbox because
    #: it has a __init__ constructor" -- a warning about our class, in their
    #: output, that they cannot act on. This opts out.
    __test__ = False

    def __init__(self, mailbox: Mailbox, ctx: _InboxContext) -> None:
        #: The unwrapped handle. No watermarking, no report record.
        self.mailbox = mailbox
        self._ctx = ctx

    # -- forwarded ------------------------------------------------------ #

    @property
    def address(self) -> str:
        return self.mailbox.address

    @property
    def local_part(self) -> str:
        return self.mailbox.local_part

    @property
    def domain(self) -> str:
        return self.mailbox.domain

    @property
    def created_at(self) -> datetime:
        return self.mailbox.created_at

    @property
    def expires_at(self) -> datetime:
        return self.mailbox.expires_at

    @property
    def expired(self) -> bool:
        return self.mailbox.expired

    @property
    def expires_in(self) -> float:
        return self.mailbox.expires_in

    @property
    def message_count(self) -> int:
        return self.mailbox.message_count

    @property
    def confirmed(self) -> bool:
        return self.mailbox.confirmed

    @property
    def watermark(self) -> datetime:
        """The boundary this test's waits start from.

        Re-stamped at the start of every test, which is what stops a shared
        inbox from resolving with a neighbour's code. Read it if you need to
        pass it somewhere else; you never need to set it.
        """
        return self._ctx.watermark

    def refresh(self) -> Mailbox:
        return self.mailbox.refresh()

    def delete(self) -> int:
        return self.mailbox.delete()

    def page(self, **kwargs: Any) -> MessagePage:
        """One page of summaries, newest first. Not watermarked -- an explicit read."""
        return self.mailbox.page(**kwargs)

    # -- watermarked ---------------------------------------------------- #

    def wait_for_code(self, filter: MessageFilter | None = None, **kwargs: Any) -> str:
        """Wait for a verification code from **this test's** mail.

        ``after`` defaults to :attr:`watermark`, so a code triggered by an
        earlier test in the same session is not a candidate. Pass ``after``
        yourself, or ``ignore_existing=True``, to override that.
        """
        spec = self._resolve(filter, kwargs)

        # wait_for_code returns a bare string, but the report needs the message
        # it came from. Rather than spend a request fetching the body back --
        # which would also undo "never fetches a body" -- record every summary
        # the filter accepted and pick the one carrying the code handed back.
        candidates: list[MessageSummary] = []
        user_match = spec.match

        def capture(message: MessageSummary) -> bool:
            if user_match is not None and not user_match(message):
                return False
            candidates.append(message)
            return True

        code = self.mailbox.wait_for_code(spec.replace(match=capture))
        for candidate in reversed(candidates):
            if candidate.verification_codes and candidate.verification_codes[0] == code:
                self._attach(candidate)
                break
        return code

    def wait_for_message(self, filter: MessageFilter | None = None, **kwargs: Any) -> Message:
        """Wait for a message from **this test's** mail. Watermarked as above."""
        message = self.mailbox.wait_for_message(self._resolve(filter, kwargs))
        self._attach(message)
        return message

    def _resolve(self, filter: MessageFilter | None, kwargs: dict[str, Any]) -> MessageFilter:
        if filter is not None and kwargs:
            raise TypeError("pass either a MessageFilter or keyword arguments, not both.")
        spec = filter if filter is not None else MessageFilter(**kwargs)
        return apply_watermark(spec, self._ctx.watermark)

    def _attach(self, message: MessageSummary | Message) -> None:
        """Recording to the report must never be the reason a passing test fails."""
        record = self._ctx.record
        if record is None:
            return
        with contextlib.suppress(Exception):
            record("nowtempmail", redact_for_report(message, attach_body=self._ctx.attach_body))

    def __repr__(self) -> str:
        return f"TestInbox(address={self.address!r}, watermark={self.watermark.isoformat()})"


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #

_INI = {
    "ntm_base_url": "Override the API host. Defaults to NTM_BASE_URL, then production.",
    "ntm_attach_body": "Put message bodies and links in the test report. Default false.",
    "ntm_watermark_skew_ms": f"Watermark skew in ms. Default {DEFAULT_WATERMARK_SKEW_MS}.",
    "ntm_mailbox_ttl_seconds": "TTL for fixture mailboxes. Default: the API's own.",
    "ntm_mailbox_domain": "Domain for fixture mailboxes. Default: the rotation's choice.",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ini keys.

    Unconditionally, and with no environment lookup: ``pytest --help`` must not
    differ depending on whether a key is set. There is deliberately no
    ``ntm_api_key`` ini option -- an API key in ``pyproject.toml`` is an API key
    in git.
    """
    for name, help_text in _INI.items():
        parser.addini(name, help_text, default=None)


def _ini(config: pytest.Config, name: str) -> str | None:
    value = config.getini(name)
    return str(value) if value not in (None, "") else None


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def ntm_options(pytestconfig: pytest.Config) -> dict[str, Any]:
    """Resolved configuration. Override in a conftest to change it per project."""
    skew = _ini(pytestconfig, "ntm_watermark_skew_ms")
    ttl = _ini(pytestconfig, "ntm_mailbox_ttl_seconds")
    attach = (_ini(pytestconfig, "ntm_attach_body") or "").strip().lower()
    return {
        "base_url": _ini(pytestconfig, "ntm_base_url"),
        "attach_body": attach in ("1", "true", "yes", "on"),
        "watermark_skew_ms": int(skew) if skew else DEFAULT_WATERMARK_SKEW_MS,
        "mailbox": {
            "ttl_seconds": int(ttl) if ttl else None,
            "domain": _ini(pytestconfig, "ntm_mailbox_domain"),
        },
    }


@pytest.fixture(scope="session")
def ntm_client(ntm_options: dict[str, Any]) -> NowTempMail:
    """The client, one per session.

    **Lazy on purpose.** This module is loaded into every pytest session in the
    environment via the ``pytest11`` entry point, including suites that have
    never heard of NowTempMail. Constructing a client at import time -- or
    anywhere other than inside a fixture a test actually requested -- would make
    a missing ``NTM_API_KEY`` break collection for those suites. Nothing above
    this line reads the environment or touches the network.
    """
    kwargs: dict[str, Any] = {"user_agent_suffix": "pytest-nowtempmail"}
    if ntm_options["base_url"]:
        kwargs["base_url"] = ntm_options["base_url"]
    return NowTempMail(**kwargs)


@pytest.fixture(scope="session")
def _ntm_context(ntm_options: dict[str, Any]) -> _InboxContext:
    """The mutable cell the watermark lives in. Internal; do not depend on it.

    Deliberately does **not** depend on ``ntm_client``: this is pulled in by an
    autouse fixture, so every test in the project would otherwise construct a
    client and fail on a missing key even if it never sends mail.
    """
    return _InboxContext(
        attach_body=ntm_options["attach_body"],
        skew_ms=ntm_options["watermark_skew_ms"],
    )


@pytest.fixture(autouse=True)
def _ntm_stamp(_ntm_context: _InboxContext, request: pytest.FixtureRequest) -> Iterator[None]:
    """Stamp this test's watermark. The whole reason a shared inbox is usable.

    Runs before every test, moves the boundary to this test's start, and points
    the inbox at this test's report. **It makes no network call to do it** --
    which is what lets it be autouse without costing anything in a suite that
    never touches NowTempMail.

    It appends to ``node.user_properties`` directly rather than requesting the
    ``record_property`` fixture, and that is not a style choice. Requesting
    ``record_property`` emits ``PytestWarning: record_property is incompatible
    with junit_family 'xunit2'`` **at fixture setup**, whether or not anything
    is ever recorded -- and since this fixture is autouse, that warning would
    fire for *every test in every suite in the environment* the moment someone
    ran with ``--junitxml`` under pytest's default family. A plugin nobody asked
    for, warning about a fixture nobody used, on every line of someone else's
    output. Appending to the same list by hand has identical effect and says
    nothing.
    """
    _ntm_context.watermark = compute_watermark(skew_ms=_ntm_context.skew_ms)
    node = request.node

    def record(name: str, value: object) -> None:
        node.user_properties.append((name, value))

    _ntm_context.record = record
    try:
        yield
    finally:
        _ntm_context.record = None


def _create(client: NowTempMail, options: dict[str, Any]) -> Mailbox:
    params = {k: v for k, v in options["mailbox"].items() if v is not None}
    return client.mailboxes.create(**params)


@pytest.fixture(scope="session")
def ntm_inbox(
    ntm_client: NowTempMail, ntm_options: dict[str, Any], _ntm_context: _InboxContext
) -> TestInbox:
    """One mailbox, **reused by every test in the session.** This is deliberate.

    ``POST /v1/mailboxes`` is the only endpoint that consumes daily quota, the
    counter increments on creation, and **it is never decremented on delete**. A
    per-test mailbox therefore spends the Free plan's 50 mailboxes/day inside a
    single CI run -- the suite goes green locally, then starts failing with
    ``quota_exceeded`` partway through the third run of the afternoon, and the
    failure has nothing to do with the code that triggered it.

    Under ``pytest-xdist`` session scope means one mailbox per worker process,
    which is what you want: N workers, N mailboxes, not one shared across
    processes that cannot see each other's watermarks.

    The cost of sharing is that the address is not unique to a test, so **tests
    must disambiguate on content, not on address.** That is handled for you:
    the waits default their ``after`` boundary to a watermark re-stamped at the
    start of every test, so a code from an earlier test is not a candidate. Add
    ``subject`` or ``sender`` when two tests trigger mail from the same sender
    within a second of each other.

    Not deleted on teardown: the quota is already spent either way, and leaving
    it to expire means a post-mortem can still read what actually arrived.
    """
    return TestInbox(_create(ntm_client, ntm_options), _ntm_context)


@pytest.fixture
def ntm_fresh_inbox(
    ntm_client: NowTempMail, ntm_options: dict[str, Any], request: pytest.FixtureRequest
) -> Iterator[TestInbox]:
    """A mailbox of this test's own.

    **Costs one mailbox out of the daily quota per test, and the quota is never
    refunded** -- a 40-test file burns 40 of the Free plan's 50/day, and deleting
    the mailbox on teardown does not give any of them back.

    Use it only for the tests that genuinely cannot share: one asserting on an
    empty inbox, one that deletes messages out from under a sibling, or one that
    needs a different domain or TTL. Everything else should use
    :func:`ntm_inbox`, whose watermarking already isolates tests from each other.

    Because it is fresh, its waits default to the mailbox's own ``created_at``
    rather than to a watermark -- every message in it belongs to this test by
    construction.
    """
    mailbox = _create(ntm_client, ntm_options)
    ctx = _InboxContext(
        attach_body=ntm_options["attach_body"],
        skew_ms=ntm_options["watermark_skew_ms"],
    )
    ctx.watermark = mailbox.created_at
    # Same reasoning as _ntm_stamp: never request the record_property fixture.
    ctx.record = lambda name, value: request.node.user_properties.append((name, value))
    try:
        yield TestInbox(mailbox, ctx)
    finally:
        # Cleanup failure is not a test failure, and the quota is spent anyway.
        with contextlib.suppress(Exception):
            mailbox.delete()


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    """Put the matched message into the failure output.

    ``record_property`` already carries it into JUnit XML, which is what a CI
    system reads. This adds the same record to the section a *human* reads when
    a test fails, which is where "the wrong mail matched" actually gets
    diagnosed. Only on failure -- a passing test does not need it.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when != "call" or report.passed:
        return
    for name, value in getattr(report, "user_properties", []):
        if name == "nowtempmail":
            report.sections.append(("NowTempMail message", _format_record(value)))


def _format_record(value: object) -> str:
    if not isinstance(value, dict):
        return str(value)
    lines = [
        f"from         {value.get('from')}",
        f"subject      {value.get('subject')}",
        f"received_at  {value.get('received_at')}",
        f"codes        {', '.join(value.get('verification_codes') or []) or '(none)'}",
        f"links        {value.get('link_count', 0)} (not shown)"
        if value.get("redacted")
        else f"links        {', '.join(value.get('links') or []) or '(none)'}",
    ]
    if not value.get("redacted"):
        lines.append(f"body\n{value.get('text') or value.get('html') or ''}")
    lines.append("")
    lines.append(str(value.get("note", "")))
    return "\n".join(lines)
