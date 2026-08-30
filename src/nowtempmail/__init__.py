"""``nowtempmail`` -- disposable mailboxes and OTP codes for your tests.

::

    from nowtempmail import NowTempMail

    ntm = NowTempMail()                     # reads NTM_API_KEY
    inbox = ntm.mailboxes.create()          # returns once it can receive
    sign_up(inbox.address)
    code = inbox.wait_for_code()            # extracted server-side

Using pytest? You almost certainly want the fixtures instead::

    pip install pytest-nowtempmail

    def test_signup(ntm_inbox):
        sign_up(ntm_inbox.address)
        assert verify(ntm_inbox.wait_for_code())

**Server-side only.** ``/v1`` rejects any request carrying an ``Origin`` header,
so this is a library for test runners, CI and backends -- not for anything that
runs in a page.

Two error class names deliberately differ from the JavaScript SDK, because the
literal translations shadow builtins: :class:`WaitTimeoutError` (not
``TimeoutError``) and :class:`TransportError` (not ``ConnectionError``). See
``nowtempmail.errors`` for the reasoning.
"""

from __future__ import annotations

from ._http import MIN_POLL_INTERVAL_MS, VERSION
from .client import DEFAULT_BASE_URL, NowTempMail
from .errors import (
    AccountSuspendedError,
    AddressReservedError,
    AddressTakenError,
    ConfigError,
    ForbiddenError,
    InternalError,
    InvalidKeyError,
    InvalidRequestError,
    KeyRevokedError,
    MailboxMayExistError,
    NotFoundError,
    NowTempMailError,
    OriginNotAllowedError,
    PlanUpgradeRequiredError,
    QuotaExceededError,
    RateLimitExceededError,
    TransportError,
    UnauthorizedError,
    WaitTimeoutError,
    WebhookConfigError,
    WebhookSignatureError,
    error_from_problem,
)
from .mailbox import Mailbox, MessageFilter
from .message import Message
from .types import (
    PROBLEM_CODES,
    Account,
    Attachment,
    CreateMailboxParams,
    Domain,
    MessagePage,
    MessageSummary,
    MeteringSnapshot,
    Plan,
    Quota,
    RateLimit,
    Reservation,
    ReservationList,
    Subscription,
    Usage,
    WebhookEndpoint,
    WebhookEvent,
)

__version__ = VERSION

__all__ = [
    "DEFAULT_BASE_URL",
    "MIN_POLL_INTERVAL_MS",
    "PROBLEM_CODES",
    "Account",
    "AccountSuspendedError",
    "AddressReservedError",
    "AddressTakenError",
    "Attachment",
    "ConfigError",
    "CreateMailboxParams",
    "Domain",
    "ForbiddenError",
    "InternalError",
    "InvalidKeyError",
    "InvalidRequestError",
    "KeyRevokedError",
    "Mailbox",
    "MailboxMayExistError",
    "Message",
    "MessageFilter",
    "MessagePage",
    "MessageSummary",
    "MeteringSnapshot",
    "NotFoundError",
    "NowTempMail",
    "NowTempMailError",
    "OriginNotAllowedError",
    "Plan",
    "PlanUpgradeRequiredError",
    "Quota",
    "QuotaExceededError",
    "RateLimit",
    "RateLimitExceededError",
    "Reservation",
    "ReservationList",
    "Subscription",
    "TransportError",
    "UnauthorizedError",
    "Usage",
    "WaitTimeoutError",
    "WebhookConfigError",
    "WebhookEndpoint",
    "WebhookEvent",
    "WebhookSignatureError",
    "__version__",
    "error_from_problem",
]
