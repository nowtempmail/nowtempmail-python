"""The public client.

Two things happen here that are not obvious from the method signatures:

1. **``base_url`` must be https**, with an explicit carve-out for loopback so a
   local dev server works. An http URL anywhere else puts the bearer token on
   the wire in clear text. The carve-out is a literal-host check rather than an
   "is it private" heuristic, deliberately: ``http://internal.corp/`` puts a
   token on a wire somebody else can read.
2. **``mailboxes.create()`` confirms before it returns.** The mail receiver drops
   inbound mail permanently if its inbox lookup misses, so a handle returned
   before the mailbox is readable hands the caller a race they cannot see. See
   :meth:`_MailboxesAPI._confirm`.

There is no browser refusal here, unlike the Node SDK. That check exists because
a browser build of that package is a published API key that also does not work
(``/v1`` rejects any request carrying an ``Origin`` header). Python has no
browser runtime, so the corresponding error class would be one that can never
fire, and shipping it would be a lie about the taxonomy.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlparse

from ._http import Http, sleep
from .errors import ConfigError, MailboxMayExistError, NowTempMailError
from .mailbox import Mailbox
from .types import (
    Account,
    CreateMailboxParams,
    Domain,
    MeteringSnapshot,
    Plan,
    Quota,
    Reservation,
    ReservationList,
    Subscription,
    Usage,
    WebhookEndpoint,
    from_epoch,
)

__all__ = ["DEFAULT_BASE_URL", "NowTempMail"]

#: NOT ``api.nowtempmail.com`` -- that hostname is unconfirmed and may not resolve.
DEFAULT_BASE_URL = "https://nowtempmail.com"

#: Attempts and spacing for the confirming read after a mailbox is created.
_CONFIRM_ATTEMPTS = 3
_CONFIRM_DELAY = 0.4

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "[::1]"}


class NowTempMail:
    """Disposable mailboxes and OTP codes.

    ::

        from nowtempmail import NowTempMail

        ntm = NowTempMail()                       # reads NTM_API_KEY
        inbox = ntm.mailboxes.create()            # returns once it can receive
        sign_up(inbox.address)
        code = inbox.wait_for_code()              # extracted server-side

    :param api_key: defaults to ``NTM_API_KEY`` from the environment.
    :param base_url: defaults to ``https://nowtempmail.com``.
    :param timeout: per request, seconds. Default 30.
    :param max_retries: ``GET``/``DELETE`` only. ``POST /v1/mailboxes`` is never
        retried -- see ``_http.py`` rule 2.
    :param on_low_quota: fires when ``X-Quota-Remaining`` drops below
        ``low_quota_threshold``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        user_agent_suffix: str | None = None,
        low_quota_threshold: int = 5,
        on_request: Callable[[dict[str, Any]], None] | None = None,
        on_response: Callable[[dict[str, Any]], None] | None = None,
        on_low_quota: Callable[[Quota], None] | None = None,
    ) -> None:
        key = (api_key or os.environ.get("NTM_API_KEY") or "").strip()
        if not key:
            raise ConfigError(
                "No API key. Pass NowTempMail(api_key=...) or set NTM_API_KEY in the "
                "environment. Create a key at https://nowtempmail.com/developers."
            )
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError("timeout must be a positive number of seconds.")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ConfigError("max_retries must be a non-negative integer.")

        resolved_base = _resolve_base_url(
            base_url or os.environ.get("NTM_BASE_URL") or DEFAULT_BASE_URL
        )
        self._http = Http(
            api_key=key,
            base_url=resolved_base,
            timeout=timeout,
            max_retries=max_retries,
            user_agent_suffix=user_agent_suffix or os.environ.get("NTM_USER_AGENT_SUFFIX"),
            low_quota_threshold=low_quota_threshold,
            on_request=on_request,
            on_response=on_response,
            on_low_quota=on_low_quota,
        )
        self.mailboxes = _MailboxesAPI(self._http)
        self.domains = _DomainsAPI(self._http)
        self.reservations = _ReservationsAPI(self._http)
        self.webhooks = _WebhooksAPI(self._http)

    @property
    def base_url(self) -> str:
        return self._http.base_url

    @property
    def http(self) -> Http:
        """The transport. Exposed for the fixtures, not for application code."""
        return self._http

    @property
    def metering(self) -> MeteringSnapshot:
        """The most recent metering snapshot. Both members are ``None`` until the
        first request completes."""
        return self._http.metering

    def me(self) -> Account:
        """Plan, limits and live usage for the calling key.

        Runs in peek mode server-side: **polling it does not consume the budget
        it reports on.** Branch on ``paywall_enabled`` rather than inferring a
        tier from the key prefix -- while it is false, every account gets the
        launch capabilities.
        """
        wire = self._http.request("GET", "/v1/me").data
        return _to_account(wire)

    def __repr__(self) -> str:
        # No api_key member is reachable from here, and this is deliberate: a
        # client object routinely ends up in a pytest fixture repr.
        return f"NowTempMail(base_url={self.base_url!r})"


class _DomainsAPI:
    def __init__(self, http: Http) -> None:
        self._http = http

    def list(self) -> list[Domain]:
        """Domains currently accepting mail. Exactly one is ``default``.

        The set rotates, so resolve it at runtime rather than hard-coding one.
        """
        wire = self._http.request("GET", "/v1/domains").data
        return [Domain(domain=d["domain"], default=d["default"]) for d in wire["domains"]]


class _MailboxesAPI:
    def __init__(self, http: Http) -> None:
        self._http = http

    def create(
        self,
        params: CreateMailboxParams | None = None,
        **kwargs: Any,
    ) -> Mailbox:
        """Create a mailbox, then confirm it is readable before returning.

        The confirming ``GET`` runs up to three times, 400ms apart. If none
        succeeds the handle is still returned, with ``confirmed=False`` -- a
        working mailbox with a slow read is far better than a raised error over
        a mailbox that exists and has already cost quota.

        **This call is never retried.** It is the only endpoint that consumes
        daily quota and the API has no idempotency key, so a transport failure
        raises :class:`~nowtempmail.errors.MailboxMayExistError` rather than
        sending a second one.
        """
        if params is not None and kwargs:
            raise TypeError("pass either a CreateMailboxParams or keyword arguments, not both.")
        resolved = params if params is not None else CreateMailboxParams(**kwargs)

        try:
            created = self._http.request(
                "POST", "/v1/mailboxes", body=resolved.to_wire(), retry=False
            ).data
        except NowTempMailError as err:
            # The request went out. Whether it landed is unknowable from here,
            # and guessing wrong costs a mailbox out of a daily budget that
            # deletion never refunds.
            if err.code == "connection_error":
                raise MailboxMayExistError(
                    "mailbox_may_exist",
                    "Mailbox creation failed in transit, so it is unknown whether one was "
                    "created. This SDK will not send a second request: creation is the only "
                    "call that spends daily quota and the API has no idempotency key. "
                    "Check me() for the quota actually spent.",
                ) from err
            raise

        confirmation = self._confirm(created["address"])
        return Mailbox(
            self._http,
            address=created["address"],
            domain=created["domain"],
            created_at=from_epoch(created["createdAt"]),
            expires_at=from_epoch(created["expiresAt"]),
            ttl_seconds=created["ttlSeconds"],
            reserved=created["reserved"],
            webhook_url=created.get("webhookUrl"),
            message_count=(confirmation or created).get("messageCount", 0),
            last_message_at=from_epoch((confirmation or {}).get("lastMessageAt")),
            confirmed=confirmation is not None,
        )

    def get(self, address: str) -> Mailbox:
        """A handle for an address that already exists, verified with one ``GET``.

        ``GET /v1/mailboxes/{address}`` is narrower than creation's response, so
        ``ttl_seconds`` is derived from the timestamps and ``reserved`` /
        ``webhook_url`` read as their defaults -- the API does not report them
        here. Raises ``NotFoundError`` if the mailbox is gone, expired, or owned
        by another key; the API cannot tell those apart on purpose.
        """
        normalised = _normalise_address(address)
        wire = self._http.request("GET", f"/v1/mailboxes/{quote(normalised, safe='')}").data
        return Mailbox(
            self._http,
            address=wire["address"],
            created_at=from_epoch(wire["createdAt"]),
            expires_at=from_epoch(wire["expiresAt"]),
            message_count=wire["messageCount"],
            last_message_at=from_epoch(wire.get("lastMessageAt")),
            confirmed=True,
        )

    def attach(self, address: str) -> Mailbox:
        """A handle with no network call, for when you already know it is live.

        Nothing about it is verified, so ``confirmed`` is False, ``created_at``
        reads as the epoch -- which makes ``wait_for_*`` consider every message
        already in the inbox -- and ``expires_in`` is ``inf`` until you call
        ``refresh()``.
        """
        return Mailbox(self._http, address=_normalise_address(address), confirmed=False)

    def delete(self, address: str) -> int:
        """Delete a mailbox and its messages. Quota is not refunded."""
        normalised = _normalise_address(address)
        wire = self._http.request("DELETE", f"/v1/mailboxes/{quote(normalised, safe='')}").data
        return int((wire or {}).get("deletedMessages", 0))

    def _confirm(self, address: str) -> dict[str, Any] | None:
        """Read the mailbox back until it is visible, or give up.

        The API can accept a mailbox before that mailbox is guaranteed readable,
        and **mail arriving inside that window is dropped rather than queued** --
        so "created" and "able to receive" are not the same instant. Confirming
        the read is the only way to close the gap from the client side.
        """
        path = f"/v1/mailboxes/{quote(address, safe='')}"
        for attempt in range(1, _CONFIRM_ATTEMPTS + 1):
            try:
                # retry=False: this loop *is* the retry policy, and a nested one
                # would make the number of requests unpredictable.
                return self._http.request("GET", path, retry=False).data  # type: ignore[no-any-return]
            except NowTempMailError:
                if attempt < _CONFIRM_ATTEMPTS:
                    sleep(_CONFIRM_DELAY)
        return None


class _ReservationsAPI:
    def __init__(self, http: Http) -> None:
        self._http = http

    def list(self) -> ReservationList:
        """Addresses this ACCOUNT holds across mailbox expiry, with the ceiling."""
        wire = self._http.request("GET", "/v1/reservations").data
        return ReservationList(
            reservations=tuple(
                Reservation(
                    id=r["id"],
                    address=r["address"],
                    created_at=_require(from_epoch(r["createdAt"])),
                )
                for r in wire["reservations"]
            ),
            limit=wire["limit"],
            used=wire["used"],
        )

    def release(self, address: str) -> None:
        """Give up the claim on an address.

        This does **not** delete a live mailbox on it -- the mailbox keeps
        receiving mail until its own expiry.
        """
        self._http.request(
            "DELETE", f"/v1/reservations/{quote(_normalise_address(address), safe='')}"
        )


class _WebhooksAPI:
    def __init__(self, http: Http) -> None:
        self._http = http

    def test(self, endpoint_id: str | None = None) -> dict[str, Any]:
        """Queue one synthetic ``message.received`` delivery.

        ``202`` means queued, not delivered -- check ``webhook_endpoints`` on
        ``me()`` if it does not arrive. Omit ``endpoint_id`` to use the newest
        endpoint on this key that is not disabled.
        """
        body: dict[str, Any] = {} if endpoint_id is None else {"endpointId": endpoint_id}
        return self._http.request("POST", "/v1/webhooks/test", body=body).data  # type: ignore[no-any-return]

    def delete(self, endpoint_id: str) -> None:
        """Deregister an endpoint. Deliveries stop immediately, queued ones too.

        Mailboxes that pointed at it keep running.
        """
        self._http.request("DELETE", f"/v1/webhooks/{quote(endpoint_id, safe='')}")


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _require(value: Any) -> Any:
    assert value is not None
    return value


def _resolve_base_url(raw: str) -> str:
    """https everywhere, http only on an explicit loopback host."""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        raise ConfigError(f"base_url is not a valid absolute URL: {raw!r}")
    if parsed.username or parsed.password:
        raise ConfigError("base_url must not embed credentials.")
    host = (parsed.hostname or "").lower()
    loopback = host in _LOOPBACK or host.endswith(".localhost")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ConfigError(
            f"base_url must use https (got {parsed.scheme}://{host}). Plain http is "
            "accepted only for localhost and 127.0.0.1, because anything else would "
            "send your API key in clear text."
        )
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}{parsed.path.rstrip('/')}"


def _normalise_address(address: str) -> str:
    trimmed = address.strip().lower()
    if not trimmed:
        raise ConfigError("An address is required.")
    return trimmed


def _to_account(wire: dict[str, Any]) -> Account:
    usage = wire["usage"]
    resets = usage["resetsAt"]
    subscription = wire.get("subscription")
    return Account(
        key_id=wire["keyId"],
        environment=wire["environment"],
        paywall_enabled=bool(wire["paywall"]["enabled"]),
        paywall_note=wire["paywall"].get("note"),
        plan=Plan.from_wire(wire["plan"]),
        usage=Usage(
            requests_this_minute=usage["requestsThisMinute"],
            mailboxes_today=usage["mailboxesToday"],
            mailboxes_remaining_today=usage["mailboxesRemainingToday"],
            reserved_addresses_used=usage["reservedAddressesUsed"],
            resets_at_minute=_require(from_epoch(resets["minute"])),
            resets_at_day=_require(from_epoch(resets["day"])),
        ),
        subscription=(
            Subscription(
                status=subscription["status"],
                plan_id=subscription["planId"],
                current_period_end=from_epoch(subscription.get("currentPeriodEnd")),
                cancel_at_period_end=subscription["cancelAtPeriodEnd"],
            )
            if subscription
            else None
        ),
        webhook_endpoints=tuple(
            WebhookEndpoint(
                id=e["id"],
                url=e["url"],
                status=e["status"],
                consecutive_failures=e["consecutiveFailures"],
                last_success_at=from_epoch(e.get("lastSuccessAt")),
                last_failure_at=from_epoch(e.get("lastFailureAt")),
                disabled_at=from_epoch(e.get("disabledAt")),
                disabled_reason=e.get("disabledReason"),
            )
            for e in (wire.get("webhookEndpoints") or ())
        ),
    )
