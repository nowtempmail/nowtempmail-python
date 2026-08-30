"""The mailbox handle. This is the object a test spends its time with.

The interesting decisions all live in :meth:`Mailbox._watch`, and all of them
come from how a live inbox actually behaves rather than from what reads nicely:

- **The high-water mark defaults to the mailbox's ``created_at``**, not to the
  moment ``wait_for_code`` was called. Mail routinely lands before anyone waits
  for it -- a signup form submits, the mail arrives in 300ms, and the test gets
  around to waiting a second later. Defaulting to call time makes that message
  invisible and produces a flake nobody can reproduce.
- **It polls page one and never paginates.** New mail arrives at the *front* of
  the list, so a cursor walk started at page one can miss a message that lands
  mid-walk.
- **One request in flight per handle.** Two overlapping polls double the request
  rate for no extra freshness and bring the 429 forward.
- **A 429 sleeps for exactly ``Retry-After``** rather than a backoff guess,
  because the server knows when the fixed minute window resets and we do not.
"""

from __future__ import annotations

import random
import re
import threading
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from typing import Any

from ._http import Http, raise_if_cancelled, sleep
from .errors import NowTempMailError, WaitTimeoutError
from .message import Message
from .types import MessagePage, MessageSummary, from_epoch

__all__ = ["Mailbox", "MessageFilter"]

#: +/-15%, so N watchers on one key do not synchronise into a thundering herd.
_JITTER = 0.15

_EPOCH = datetime.fromtimestamp(0, tz=timezone.utc)


class MessageFilter:
    """Which mail a wait will accept, and how patiently.

    :param sender: matched against the raw ``From`` header. A string matches
        case-insensitively as a substring; a compiled pattern is searched.
    :param subject: same, against the subject.
    :param after: only consider mail received at or after this. Defaults to the
        mailbox's ``created_at`` -- see the module docstring for why that is not
        "now".
    :param ignore_existing: shorthand for ``after = now``. For resend flows,
        where the stale code is the bug.

        **There is a one-second ambiguity here and it cannot be removed.**
        ``received_at`` on the wire has one-second granularity, so a message
        stamped in the same second the wait began might have arrived just before
        the call or just after it -- nothing in the data distinguishes them.
        This package resolves that by **excluding** such a message: including it
        risks handing back exactly the stale code ``ignore_existing`` exists to
        avoid, which is a silent wrong pass, whereas excluding it produces a
        loud :class:`~nowtempmail.errors.WaitTimeoutError`. If the mail you are
        waiting for can arrive inside the same second you start waiting, use an
        explicit ``after`` instead.
    :param match: an arbitrary predicate over the summary, applied last.
    :param timeout: seconds. Default 60.
    :param poll_interval: seconds, honoured verbatim -- no floor and no jitter is
        applied to an explicit value. Omit it and the interval is derived from
        the key's rate limit divided across active watchers.
    :param cancel: a :class:`threading.Event`. Setting it stops the wait at once.
    """

    __slots__ = (
        "sender",
        "subject",
        "after",
        "ignore_existing",
        "match",
        "timeout",
        "poll_interval",
        "cancel",
    )

    def __init__(
        self,
        *,
        sender: str | re.Pattern[str] | None = None,
        subject: str | re.Pattern[str] | None = None,
        after: datetime | None = None,
        ignore_existing: bool = False,
        match: Callable[[MessageSummary], bool] | None = None,
        timeout: float = 60.0,
        poll_interval: float | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.sender = sender
        self.subject = subject
        self.after = after
        self.ignore_existing = ignore_existing
        self.match = match
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.cancel = cancel

    def accepts(self, message: MessageSummary) -> bool:
        if self.sender is not None and not _text_matches(message.sender, self.sender):
            return False
        if self.subject is not None and not _text_matches(message.subject, self.subject):
            return False
        if self.match is not None and not self.match(message):
            return False
        return True

    def replace(self, **changes: Any) -> MessageFilter:
        """A copy with some fields changed. Used by the pytest fixtures to apply
        a watermark without mutating what the caller passed in."""
        current = {name: getattr(self, name) for name in MessageFilter.__slots__}
        current.update(changes)
        return MessageFilter(**current)


def _text_matches(value: str, pattern: str | re.Pattern[str]) -> bool:
    if isinstance(pattern, str):
        return pattern.lower() in value.lower()
    return pattern.search(value) is not None


class Mailbox:
    """A live mailbox. Obtain one from ``client.mailboxes.create()``."""

    def __init__(
        self,
        http: Http,
        *,
        address: str,
        domain: str | None = None,
        created_at: datetime | None = None,
        expires_at: datetime | None = None,
        ttl_seconds: int | None = None,
        reserved: bool = False,
        webhook_url: str | None = None,
        message_count: int = 0,
        last_message_at: datetime | None = None,
        confirmed: bool = False,
    ) -> None:
        self._http = http
        self.address = address.strip().lower()
        at = self.address.rfind("@")
        self.local_part = self.address if at == -1 else self.address[:at]
        self.domain = domain if domain is not None else ("" if at == -1 else self.address[at + 1 :])

        self._created_at = created_at
        self._expires_at = expires_at
        self._message_count = message_count
        self._last_message_at = last_message_at
        self._confirmed = confirmed

        # POST reports these three; GET /v1/mailboxes/{address} does not. They
        # are set once, at construction, and refresh() leaves them alone --
        # overwriting them with the None a narrower response carries would
        # silently destroy them.
        self.ttl_seconds = (
            ttl_seconds if ttl_seconds is not None else _derived_ttl(created_at, expires_at)
        )
        self.reserved = reserved
        self.webhook_url = webhook_url

        #: Serialises every request this handle makes. See the module docstring.
        self._lock = threading.Lock()

    # -- properties -------------------------------------------------------- #

    @property
    def created_at(self) -> datetime:
        """Reads as the Unix epoch on an ``attach()`` handle that has not talked
        to the API -- deliberately, since that makes ``wait_for_*`` consider
        every message already in the inbox."""
        return self._created_at or _EPOCH

    @property
    def expires_at(self) -> datetime:
        """Reads as the Unix epoch until known. Check :attr:`expired` instead of
        comparing."""
        return self._expires_at or _EPOCH

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def last_message_at(self) -> datetime | None:
        """Timestamp of the newest message, as of the last read."""
        return self._last_message_at

    @property
    def confirmed(self) -> bool:
        """False when create-then-confirm could not verify readability in time."""
        return self._confirmed

    @property
    def expired(self) -> bool:
        """False when the expiry is not known yet."""
        if self._expires_at is None:
            return False
        return datetime.now(timezone.utc) >= self._expires_at

    @property
    def expires_in(self) -> float:
        """Seconds until expiry. ``inf`` when the expiry is not known yet."""
        if self._expires_at is None:
            return float("inf")
        return max(0.0, (self._expires_at - datetime.now(timezone.utc)).total_seconds())

    def _path(self) -> str:
        from urllib.parse import quote

        return f"/v1/mailboxes/{quote(self.address, safe='')}"

    # -- reads ------------------------------------------------------------- #

    def refresh(self) -> Mailbox:
        """Re-read the mailbox metadata.

        Only ``created_at``, ``expires_at``, ``message_count`` and
        ``last_message_at`` are updated -- ``GET /v1/mailboxes/{address}`` does
        not return the rest, so those keep whatever creation told us.
        """
        with self._lock:
            wire = self._http.request("GET", self._path()).data
        self._created_at = from_epoch(wire["createdAt"])
        self._expires_at = from_epoch(wire["expiresAt"])
        self._message_count = wire["messageCount"]
        self._last_message_at = from_epoch(wire.get("lastMessageAt"))
        self._confirmed = True
        return self

    def delete(self) -> int:
        """Delete the mailbox and every message in it.

        Not reversible, and **no quota is refunded** -- the daily counter
        increments on creation and never comes back down.

        :returns: how many messages were deleted.
        """
        with self._lock:
            wire = self._http.request("DELETE", self._path()).data
        self._message_count = 0
        return int((wire or {}).get("deletedMessages", 0))

    def page(self, *, limit: int | None = None, cursor: str | None = None) -> MessagePage:
        """One page of summaries, newest first. ``limit`` 1-100, default 25."""
        with self._lock:
            wire = self._http.request(
                "GET", f"{self._path()}/messages", query={"limit": limit, "cursor": cursor}
            ).data
        messages = tuple(MessageSummary.from_wire(m) for m in wire["messages"])
        if messages and cursor is None:
            self._last_message_at = messages[0].received_at
        return MessagePage(
            messages=messages, has_more=wire["hasMore"], next_cursor=wire.get("nextCursor")
        )

    def list(self, *, limit: int | None = None) -> Iterator[MessageSummary]:
        """Walk the mailbox's **history**, auto-paginating until the list ends.

        This is **not** how you watch a live inbox -- use :meth:`wait_for_message`
        or :meth:`wait_for_code` for that. Messages are ordered newest first, so
        a message arriving while this iterator is partway through lands at the
        *front* of the list, behind the cursor, and is never yielded. A cursor
        walk is only correct over a set that is not growing.
        """
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.page(limit=limit, cursor=cursor)
            for message in page.messages:
                # A deleted message shifts the window; never yield an id twice.
                if message.id in seen:
                    continue
                seen.add(message.id)
                yield message
            if not page.has_more or not page.next_cursor:
                return
            cursor = page.next_cursor

    def get(self, message_id: str) -> Message:
        """The full message: bodies and attachment metadata."""
        from urllib.parse import quote

        with self._lock:
            wire = self._http.request(
                "GET", f"{self._path()}/messages/{quote(message_id, safe='')}"
            ).data
        return Message(self._http, self.address, wire)

    def delete_message(self, message_id: str) -> None:
        """Delete one message and its stored body. A second call is a 404."""
        from urllib.parse import quote

        with self._lock:
            self._http.request("DELETE", f"{self._path()}/messages/{quote(message_id, safe='')}")
        self._message_count = max(0, self._message_count - 1)

    # -- waits ------------------------------------------------------------- #

    def wait_for_code(self, filter: MessageFilter | None = None, **kwargs: Any) -> str:
        """The hero method. The first verification code on the first matching message.

        **Never fetches a body.** Codes ride on the summary because they are
        extracted server-side at ingest, so this is one request per poll and no
        message content is transferred -- which is also why nothing sensitive
        ends up in a trace.

        Keyword arguments are forwarded to :class:`MessageFilter`, so the common
        case reads ``inbox.wait_for_code(sender="github", timeout=120)``.
        """
        resolved = _resolve_filter(filter, kwargs)
        summary = self._watch("a verification code", resolved, lambda m: bool(m.verification_codes))
        return summary.verification_codes[0]

    def wait_for_message(self, filter: MessageFilter | None = None, **kwargs: Any) -> Message:
        """Wait for a matching message and return it in full, bodies included."""
        resolved = _resolve_filter(filter, kwargs)
        summary = self._watch("a message", resolved, lambda m: True)
        return self.get(summary.id)

    def _watch(
        self,
        what: str,
        spec: MessageFilter,
        usable: Callable[[MessageSummary], bool],
    ) -> MessageSummary:
        import time

        started = time.monotonic()
        after = _high_water_mark(spec, self.created_at)
        seen: set[str] = set()
        messages_seen = 0

        release = self._http.register_watcher()
        try:
            while True:
                raise_if_cancelled(spec.cancel)

                try:
                    page = self.page(limit=25)
                except NowTempMailError as err:
                    # The transport already retried and slept for Retry-After.
                    # Landing here means the window is genuinely exhausted, so
                    # wait it out rather than failing a wait the caller expects
                    # to be patient.
                    if err.code != "rate_limit_exceeded":
                        raise
                    remaining = spec.timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        break
                    sleep(min(err.retry_after or 1.0, remaining), spec.cancel)
                    continue

                # Newest first, as the API returns it: on a resend the freshest
                # code is the one the caller wants.
                for message in page.messages:
                    if message.id in seen:
                        continue
                    seen.add(message.id)
                    messages_seen += 1
                    if message.received_at < after:
                        continue
                    if not spec.accepts(message):
                        continue
                    if usable(message):
                        return message

                remaining = spec.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    break
                wait = (
                    spec.poll_interval
                    if spec.poll_interval is not None
                    else _jitter(self._http.poll_interval_ms(self._http.watchers) / 1000)
                )
                sleep(min(wait, remaining), spec.cancel)
        finally:
            release()

        raise WaitTimeoutError(
            address=self.address,
            waited_ms=int((time.monotonic() - started) * 1000),
            messages_seen=messages_seen,
            what=what,
        )

    def __repr__(self) -> str:
        return (
            f"Mailbox(address={self.address!r}, messages={self._message_count}, "
            f"confirmed={self._confirmed})"
        )


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _resolve_filter(filter: MessageFilter | None, kwargs: dict[str, Any]) -> MessageFilter:
    if filter is not None and kwargs:
        raise TypeError(
            "pass either a MessageFilter or keyword arguments, not both -- "
            "silently merging them would make precedence unguessable."
        )
    if filter is not None:
        return filter
    return MessageFilter(**kwargs)


def _derived_ttl(created_at: datetime | None, expires_at: datetime | None) -> int:
    if created_at is None or expires_at is None:
        return 0
    return max(0, round((expires_at - created_at).total_seconds()))


def _high_water_mark(spec: MessageFilter, created_at: datetime) -> datetime:
    """Mail older than this is ignored.

    Precedence, and it matters: ``ignore_existing`` wins outright because it is
    a strictly narrower request; then an explicit ``after``, because a caller who
    names a boundary has out-argued the default; then the mailbox's own
    ``created_at``.
    """
    if spec.ignore_existing:
        return datetime.now(timezone.utc)
    if spec.after is not None:
        return spec.after
    return created_at


def _jitter(seconds: float) -> float:
    return seconds * (1 - _JITTER + random.random() * 2 * _JITTER)
