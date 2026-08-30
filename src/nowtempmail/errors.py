"""The error hierarchy.

Two rules govern this module.

1. **Nothing here ever holds the API key.** Messages carry the method, the path,
   the problem code and the request id -- never a header, never a URL, which
   could carry a query secret. Tracebacks get pasted into issues.

2. **Branch on ``err.code``, not on the class**, wherever forward compatibility
   matters. The API may grow a problem code this package has never heard of;
   :func:`error_from_problem` deliberately lets an unrecognised-but-well-shaped
   code through on the base class rather than rewriting it to something from the
   status table. A caller matching on ``code`` sees the new value. A caller
   matching only on classes sees ``NowTempMailError`` and can still read it.

``isinstance`` *is* reliable here, unlike in the Node SDK -- that package is
dual-published as ESM and CJS, so a consumer can hold two copies of the classes
at once and ``instanceof`` quietly returns false. Python has no equivalent
hazard, so the ``Symbol.for()`` brand that exists there has no counterpart here.

Two class names deliberately diverge from the Node SDK, because the obvious
translation collides with a builtin:

===========================  ==========================  =========================
Node                         Here                        Why
===========================  ==========================  =========================
``TimeoutError``             :class:`WaitTimeoutError`   ``builtins.TimeoutError``
                                                         is an ``OSError``. A bare
                                                         ``except TimeoutError``
                                                         would catch a different
                                                         set of things depending
                                                         on what was imported.
``ConnectionError``          :class:`TransportError`     ``builtins.ConnectionError``
                                                         is likewise an ``OSError``.
===========================  ==========================  =========================

``httpx`` renames both for exactly this reason. Shadowing a builtin exception in
a library that lands in someone else's test suite is not a cost worth paying for
cross-SDK name symmetry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

__all__ = [
    "AccountSuspendedError",
    "AddressReservedError",
    "AddressTakenError",
    "ConfigError",
    "ForbiddenError",
    "InternalError",
    "InvalidKeyError",
    "InvalidRequestError",
    "KeyRevokedError",
    "MailboxMayExistError",
    "NotFoundError",
    "NowTempMailError",
    "OriginNotAllowedError",
    "PlanUpgradeRequiredError",
    "QuotaExceededError",
    "RateLimitExceededError",
    "TransportError",
    "UnauthorizedError",
    "WaitTimeoutError",
    "WebhookConfigError",
    "WebhookSignatureError",
    "error_from_problem",
]

KEYS_URL = "https://nowtempmail.com/developers"

LocalErrorCode = Literal[
    "timeout",
    "connection_error",
    "mailbox_may_exist",
    "webhook_signature",
    "webhook_config",
    "config_error",
]


class NowTempMailError(Exception):
    """Base class for everything this package raises.

    :param code: the stable machine-readable code. Branch on this.
    :param message: prose. May be reworded at any time; do not match on it.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
        method: str | None = None,
        path: str | None = None,
        parameter: str | None = None,
        plan: str | None = None,
        max_ttl_seconds: int | None = None,
        endpoint_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.request_id = request_id
        self.method = method
        #: Path only -- never a full URL, which could carry a query secret.
        self.path = path
        self.parameter = parameter
        self.plan = plan
        self.max_ttl_seconds = max_ttl_seconds
        self.endpoint_id = endpoint_id
        #: Seconds. Present on rate_limit_exceeded and quota_exceeded.
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        """True when retrying the identical request could plausibly succeed."""
        return self.code in ("rate_limit_exceeded", "internal_error", "connection_error")

    def to_log_string(self) -> str:
        """Compact one-liner for logs. Contains no credential material."""
        bits: list[str] = [self.code]
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.method and self.path:
            bits.append(f"{self.method} {self.path}")
        if self.request_id:
            bits.append(f"requestId={self.request_id}")
        if self.parameter:
            bits.append(f"parameter={self.parameter}")
        return " ".join(bits)

    def __repr__(self) -> str:
        # Deliberately built from the same fields as to_log_string. An
        # auto-generated repr over __dict__ would print whatever a future field
        # holds, and this object travels into tracebacks and CI logs.
        return f"{type(self).__name__}({self.to_log_string()!r})"


# --------------------------------------------------------------------------- #
# HTTP problem codes -- one class each                                         #
# --------------------------------------------------------------------------- #


class UnauthorizedError(NowTempMailError):
    """401 with no usable credential at all."""


class InvalidKeyError(NowTempMailError):
    """The key is malformed or unknown."""


class KeyRevokedError(NowTempMailError):
    """The key existed and was revoked. Create a new one; this will not recover."""


class AccountSuspendedError(NowTempMailError):
    """The account is suspended. A new key will not help."""


class ForbiddenError(NowTempMailError):
    """Authenticated, but not allowed to do this."""


class OriginNotAllowedError(NowTempMailError):
    """``/v1`` rejects any request carrying an ``Origin`` header.

    Reaching this from Python almost always means a proxy is adding the header,
    since nothing in this package sets one.
    """


class PlanUpgradeRequiredError(NowTempMailError):
    """A paid capability on a plan that does not have it.

    ``parameter`` names the field that was gated -- ``address``, ``reserve``,
    ``ttlSeconds`` or ``webhookUrl`` -- and ``max_ttl_seconds`` is populated when
    the TTL was the problem.
    """


class NotFoundError(NowTempMailError):
    """Gone, expired, or owned by another key.

    The API cannot tell those apart on purpose: distinguishing them would let a
    caller probe which addresses exist under someone else's key.
    """


class AddressTakenError(NowTempMailError):
    """A live mailbox already holds that local part."""


class AddressReservedError(NowTempMailError):
    """Another account reserved that local part, or it is on the reserved list."""


class InvalidRequestError(NowTempMailError):
    """400/422. ``parameter`` names the field the API objected to."""


class RateLimitExceededError(NowTempMailError):
    """429 against the per-minute budget. ``retry_after`` is authoritative."""


class QuotaExceededError(NowTempMailError):
    """The daily mailbox budget is spent. Waits still work; creation does not."""


class InternalError(NowTempMailError):
    """5xx, or a response that was not a usable problem document."""


# --------------------------------------------------------------------------- #
# Local codes -- raised by this package, never by the API                      #
# --------------------------------------------------------------------------- #


class WaitTimeoutError(NowTempMailError):
    """A ``wait_for_*`` window elapsed with no match.

    ``messages_seen`` is the field worth reading: it separates "nothing arrived"
    from "mail arrived but nothing matched the filter", which are different bugs
    with different fixes. Without it a timeout is undebuggable.

    Named for the builtin clash, not for style -- see the module docstring.
    """

    def __init__(
        self,
        *,
        address: str,
        waited_ms: int,
        messages_seen: int,
        what: str = "a message",
    ) -> None:
        seen = (
            "nothing arrived"
            if messages_seen == 0
            else f"{messages_seen} message{'' if messages_seen == 1 else 's'} arrived, none matched"
        )
        super().__init__(
            "timeout",
            f"Waited {waited_ms / 1000:.0f}s for {what} at {address} and gave up -- {seen}.",
        )
        self.address = address
        self.waited_ms = waited_ms
        self.messages_seen = messages_seen


class TransportError(NowTempMailError):
    """The request failed before a response was received.

    Named for the builtin clash, not for style -- see the module docstring.
    """


class MailboxMayExistError(NowTempMailError):
    """Mailbox creation failed in transit, and the outcome is unknowable.

    The request went out. Whether it landed cannot be determined from here, and
    guessing wrong costs a mailbox out of a daily budget that deletion never
    refunds -- so this package refuses to send a second one. Read
    ``GET /v1/mailboxes/{address}`` if you know the address you asked for, or
    check ``me()`` for the quota that was actually spent.
    """


class WebhookSignatureError(NowTempMailError):
    """A webhook body did not verify against the signing secret."""


class WebhookConfigError(NowTempMailError):
    """The webhook verifier was configured wrongly -- a missing secret, say."""


class ConfigError(NowTempMailError):
    """This package was constructed wrongly. Never a server fault."""

    def __init__(self, message: str) -> None:
        super().__init__("config_error", message)


# --------------------------------------------------------------------------- #
# RFC 9457 -> exception                                                        #
# --------------------------------------------------------------------------- #

_BY_CODE: dict[str, type[NowTempMailError]] = {
    "unauthorized": UnauthorizedError,
    "invalid_key": InvalidKeyError,
    "key_revoked": KeyRevokedError,
    "account_suspended": AccountSuspendedError,
    "forbidden": ForbiddenError,
    "origin_not_allowed": OriginNotAllowedError,
    "plan_upgrade_required": PlanUpgradeRequiredError,
    "not_found": NotFoundError,
    "address_taken": AddressTakenError,
    "address_reserved": AddressReservedError,
    "invalid_request": InvalidRequestError,
    "rate_limit_exceeded": RateLimitExceededError,
    "quota_exceeded": QuotaExceededError,
    "internal_error": InternalError,
}


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def error_from_problem(
    problem: Mapping[str, Any],
    *,
    method: str | None = None,
    path: str | None = None,
    retry_after: float | None = None,
) -> NowTempMailError:
    """Build the right exception from an RFC 9457 problem document.

    An unrecognised ``code`` lands on :class:`NowTempMailError` carrying that
    code verbatim, rather than being rewritten to whatever the status table
    suggests. That is what stops a server growing a fifteenth code from breaking
    every older client at once.
    """
    code = _str_or_none(problem.get("code")) or "internal_error"
    detail = (
        _str_or_none(problem.get("detail"))
        or _str_or_none(problem.get("title"))
        or f"The API returned a problem with code {code!r}."
    )
    cls = _BY_CODE.get(code, NowTempMailError)
    return cls(
        code,
        detail,
        status=_int_or_none(problem.get("status")),
        request_id=_str_or_none(problem.get("requestId")),
        method=method,
        path=path,
        parameter=_str_or_none(problem.get("parameter")),
        plan=_str_or_none(problem.get("plan")),
        max_ttl_seconds=_int_or_none(problem.get("maxTtlSeconds")),
        endpoint_id=_str_or_none(problem.get("endpointId")),
        retry_after=retry_after,
    )
