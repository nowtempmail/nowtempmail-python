"""Wire and public types for the NowTempMail Developer API.

Transcribed from the OpenAPI document served at
https://nowtempmail.com/v1/openapi.json, vendored in this package at
``spec/openapi.json``. ``python scripts/check_spec_drift.py`` fails if the two
diverge, so treat the spec as the source of truth and this file as its
transcription.

Two layers live here on purpose:

``Wire*``
    Exactly what ``/v1`` returns. Epoch **seconds**, optional where the spec says
    optional. Never widened, never prettified. ``TypedDict`` with ``total=False``
    on the genuinely optional members, because a wire type that lies about
    optionality is worse than no type at all.

public
    What a caller touches. Frozen dataclasses, and every epoch becomes a
    **timezone-aware UTC** :class:`~datetime.datetime`.

Keeping them separate is what makes the wire contract auditable against the spec
without reading the client.

**Aware, not naive.** Every boundary in this SDK is a comparison -- a watermark
against a ``received_at``, an expiry against now -- and comparing a naive
datetime to an aware one raises ``TypeError``. A naive timestamp here would turn
a mail-arrival check into a crash in someone's test suite, so the conversion
helper is the only way timestamps are built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

__all__ = [
    "PROBLEM_CODES",
    "Account",
    "Attachment",
    "CreateMailboxParams",
    "Domain",
    "MessagePage",
    "MessageSummary",
    "MeteringSnapshot",
    "Plan",
    "Quota",
    "RateLimit",
    "Reservation",
    "ReservationList",
    "Subscription",
    "Usage",
    "WebhookEndpoint",
    "from_epoch",
]

#: The stable machine-readable error codes. Branch on these; ``detail`` is prose.
PROBLEM_CODES: tuple[str, ...] = (
    "unauthorized",
    "invalid_key",
    "key_revoked",
    "account_suspended",
    "forbidden",
    "origin_not_allowed",
    "plan_upgrade_required",
    "not_found",
    "address_taken",
    "address_reserved",
    "invalid_request",
    "rate_limit_exceeded",
    "quota_exceeded",
    "internal_error",
)

PlanId = Literal["free", "starter", "scale"]
Environment = Literal["live", "test"]
SubscriptionStatus = Literal["TRIALING", "ACTIVE", "PAST_DUE", "PAUSED", "CANCELLED", "EXPIRED"]
WebhookEndpointStatus = Literal["active", "disabled"]


def from_epoch(seconds: float | int | None) -> datetime | None:
    """Epoch seconds to an aware UTC datetime. ``None`` passes through.

    The only way a timestamp enters this package. See the module docstring for
    why naive datetimes are not an option.
    """
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


# --------------------------------------------------------------------------- #
# Wire                                                                         #
# --------------------------------------------------------------------------- #


class WireProblem(TypedDict, total=False):
    """RFC 9457. ``code``, ``status``, ``detail`` and ``requestId`` are always sent."""

    type: str
    code: str
    title: str
    status: int
    detail: str
    requestId: str
    parameter: str
    plan: str
    maxTtlSeconds: int
    endpointId: str


class WirePlan(TypedDict):
    id: str
    label: str
    requestsPerMinute: int
    mailboxesPerDay: int
    maxTtlSeconds: int
    customAddress: bool
    reservedAddresses: int
    webhooks: bool


class WireMailboxCreated(TypedDict):
    """``POST /v1/mailboxes``. The fullest mailbox shape the API ever returns."""

    address: str
    domain: str
    createdAt: int
    expiresAt: int
    ttlSeconds: int
    reserved: bool
    webhookUrl: str | None
    messageCount: int


class WireMailbox(TypedDict):
    """``GET /v1/mailboxes/{address}``.

    Deliberately narrower than :class:`WireMailboxCreated` -- ``domain``,
    ``ttlSeconds``, ``reserved`` and ``webhookUrl`` are **not** returned here, so
    a refresh cannot repopulate them.
    """

    address: str
    createdAt: int
    expiresAt: int
    messageCount: int
    lastMessageAt: int | None


class WireMessageSummary(TypedDict):
    id: str
    #: The raw ``From`` header -- often ``Display Name <addr@host>``, not a bare address.
    from_: str
    subject: str
    receivedAt: int
    attachmentCount: int
    #: Extracted server-side on ingest. This is why no regex is needed.
    verificationCodes: list[str]


# --------------------------------------------------------------------------- #
# Public                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Plan:
    id: str
    label: str
    requests_per_minute: int
    mailboxes_per_day: int
    max_ttl_seconds: int
    custom_address: bool
    reserved_addresses: int
    webhooks: bool

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> Plan:
        return cls(
            id=wire["id"],
            label=wire["label"],
            requests_per_minute=wire["requestsPerMinute"],
            mailboxes_per_day=wire["mailboxesPerDay"],
            max_ttl_seconds=wire["maxTtlSeconds"],
            custom_address=wire["customAddress"],
            reserved_addresses=wire["reservedAddresses"],
            webhooks=wire["webhooks"],
        )


@dataclass(frozen=True)
class Usage:
    requests_this_minute: int
    mailboxes_today: int
    mailboxes_remaining_today: int
    reserved_addresses_used: int
    resets_at_minute: datetime
    resets_at_day: datetime


@dataclass(frozen=True)
class Subscription:
    status: str
    plan_id: str
    current_period_end: datetime | None
    cancel_at_period_end: bool


@dataclass(frozen=True)
class WebhookEndpoint:
    id: str
    url: str
    status: str
    consecutive_failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    disabled_at: datetime | None
    disabled_reason: str | None


@dataclass(frozen=True)
class Account:
    """``GET /v1/me``. Runs in peek mode server-side: polling it does not consume
    the budget it reports on."""

    key_id: str
    environment: str
    #: Whether paid plans are being sold yet. Branch on ``paywall_enabled`` rather
    #: than inferring a tier from the key prefix -- while it is false, every
    #: account receives the launch plan's capabilities regardless of what it pays.
    paywall_enabled: bool
    paywall_note: str | None
    plan: Plan
    usage: Usage
    subscription: Subscription | None
    webhook_endpoints: tuple[WebhookEndpoint, ...] = ()


@dataclass(frozen=True)
class Domain:
    domain: str
    #: True for the single domain ``POST /v1/mailboxes`` will currently choose.
    #: Which domain that is changes as the rotation weighs domain health, so
    #: resolve it at runtime rather than hard-coding one.
    default: bool


@dataclass(frozen=True)
class MessageSummary:
    id: str
    #: The raw ``From`` header. Often ``Display Name <addr@host>``.
    sender: str
    subject: str
    received_at: datetime
    attachment_count: int
    #: Extracted server-side at ingest. This is why ``wait_for_code`` never has
    #: to fetch a body, and why there is no regex to write.
    verification_codes: tuple[str, ...] = ()

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> MessageSummary:
        received = from_epoch(wire["receivedAt"])
        assert received is not None  # receivedAt is never null on the wire
        return cls(
            id=wire["id"],
            sender=wire["from"],
            subject=wire["subject"],
            received_at=received,
            attachment_count=wire["attachmentCount"],
            verification_codes=tuple(wire.get("verificationCodes") or ()),
        )


@dataclass(frozen=True)
class MessagePage:
    messages: tuple[MessageSummary, ...]
    has_more: bool
    #: Opaque. Its encoding is not part of the contract.
    next_cursor: str | None


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True)
class Reservation:
    id: str
    address: str
    created_at: datetime


@dataclass(frozen=True)
class ReservationList:
    reservations: tuple[Reservation, ...]
    limit: int
    used: int


@dataclass(frozen=True)
class RateLimit:
    limit: int
    remaining: int
    reset: datetime


@dataclass(frozen=True)
class Quota:
    limit: int
    remaining: int
    reset: datetime


@dataclass(frozen=True)
class MeteringSnapshot:
    """Read off ``X-RateLimit-*`` / ``X-Quota-*`` on every response that reached
    a handler. Both members are ``None`` until the first request completes."""

    rate_limit: RateLimit | None = None
    quota: Quota | None = None


@dataclass
class CreateMailboxParams:
    """Arguments to ``POST /v1/mailboxes``. Every field is optional.

    Not frozen: callers routinely build one up across a few lines, and there is
    no aliasing hazard because the client copies the resolved values out before
    sending.
    """

    #: A bare local part (``my-signup-test``) or a full address -- everything
    #: from the first ``@`` on is discarded. Requires ``plan.custom_address``.
    #: 3-64 chars, ``^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$``, no ``..``.
    address: str | None = None
    #: Must be active per ``domains.list()``. Never plan-gated.
    domain: str | None = None
    #: Minimum 300; maximum is ``plan.max_ttl_seconds``. Rejected, never silently
    #: clamped. Omit for 86400 (24h) on every plan.
    ttl_seconds: int | None = None
    #: Hold the local part beyond mailbox expiry. Requires ``address``.
    reserve: bool | None = None
    #: https, port 443/8443, public multi-label DNS name, no credentials.
    webhook_url: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Only the fields that were actually set. An explicit ``None`` and an
        omitted field are the same request, which matches the API's own reading."""
        wire: dict[str, Any] = {}
        if self.address is not None:
            wire["address"] = self.address
        if self.domain is not None:
            wire["domain"] = self.domain
        if self.ttl_seconds is not None:
            wire["ttlSeconds"] = self.ttl_seconds
        if self.reserve is not None:
            wire["reserve"] = self.reserve
        if self.webhook_url is not None:
            wire["webhookUrl"] = self.webhook_url
        return wire


@dataclass(frozen=True)
class WebhookEvent:
    """The body posted to a registered ``webhook_url``. A pointer, never the mail.

    **Mind the units on the wire.** Upstream emits ``createdAt`` in Unix
    *seconds* but ``data.receivedAt`` in Unix *milliseconds*, confirmed against
    live deliveries, while every ``receivedAt`` on the ``/v1`` message routes is
    in seconds. Converting both with the same multiplier puts one of them in the
    year 58347. This class normalises all three, so no caller has to know.
    """

    #: Equals the ``X-NTM-Delivery`` header. Stable across every retry -- dedupe on it.
    id: str
    type: str
    created_at: datetime
    message_id: str
    address: str
    received_at: datetime
    #: Present only on a synthetic delivery from ``POST /v1/webhooks/test``.
    deliberate: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
