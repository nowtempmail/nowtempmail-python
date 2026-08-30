"""A faithful-enough ``/v1`` on a real socket.

A real ``ThreadingHTTPServer`` rather than a monkeypatched opener, deliberately:
the properties this SDK's design rests on are *transport* properties -- that a
redirect is refused, that a 429 carries ``Retry-After``, that metering headers
ride on every response -- and a fake that returns objects instead of bytes
cannot prove any of them.

It is deliberately faithful about the things that matter:

- metering headers on every response that reached a handler
- RFC 9457 problem bodies carrying a ``requestId``
- ``Retry-After`` on 429
- the origin guard running *before* auth, exactly as ``/v1`` does
- quota consumed by mailbox creation and **never** refunded by deletion
- ``GET /v1/mailboxes/{address}`` returning a *narrower* shape than creation

Usage::

    with mock_api() as api:
        client = NowTempMail(api_key=MOCK_KEY, base_url=api.url)
        ...
        api.deliver("addr@example.com", subject="Verify", codes=["482913"])
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

#: A full-shaped key that is not a real one. Realistic on purpose: the redaction
#: tests need a credential that looks like a credential or they prove nothing.
MOCK_KEY = "ntm_live_k7Rm2xQp9Ldv_4nT8wKmZqRv2sYbXdF6hJ3pLcN9gTuA"

FREE_PLAN: dict[str, Any] = {
    "id": "free",
    "label": "Free",
    "requestsPerMinute": 30,
    "mailboxesPerDay": 50,
    "maxTtlSeconds": 86_400,
    "customAddress": False,
    "reservedAddresses": 0,
    "webhooks": False,
}

DEFAULT_DOMAIN = "allogebra.com"


@dataclass
class MockMailbox:
    address: str
    domain: str
    created_at: int
    expires_at: int
    ttl_seconds: int
    reserved: bool
    webhook_url: str | None
    messages: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RecordedRequest:
    method: str
    path: str
    request_id: str | None
    #: Recorded because it is the only thing ``/v1`` learns about who invoked a
    #: caller. A suite that never asserts on it cannot catch attribution breaking.
    user_agent: str | None


class MockState:
    def __init__(self) -> None:
        self.mailboxes: dict[str, MockMailbox] = {}
        self.mailboxes_today = 0
        self.requests_this_minute = 0
        self.reservations: list[dict[str, Any]] = []


class MockApi:
    """The server handle. Assert against :attr:`requests` and :attr:`state`."""

    def __init__(self, *, plan: dict[str, Any] | None = None) -> None:
        self.plan = {**FREE_PLAN, **(plan or {})}
        self.state = MockState()
        self.requests: list[RecordedRequest] = []
        self.url = ""
        #: Force the next N requests to fail with this code, then behave normally.
        self.fail_next: dict[str, Any] | None = None
        #: Make the first N confirming reads 404, to exercise create-then-confirm.
        self.confirm_misses = 0
        #: Serve a redirect instead of the real response, to prove it is refused.
        self.redirect_next = 0
        self._seq = 0
        self._lock = threading.Lock()

    def next_id(self, prefix: str) -> str:
        with self._lock:
            self._seq += 1
            return f"{prefix}{self._seq:04d}"

    def deliver(
        self,
        address: str,
        *,
        sender: str = "Acme <noreply@acme.test>",
        subject: str = "Your verification code",
        codes: list[str] | None = None,
        text: str | None = None,
        html: str | None = None,
        received_at: int | None = None,
    ) -> dict[str, Any]:
        """Put a message in a mailbox, as the receiver would."""
        box = self.state.mailboxes[address.lower()]
        message = {
            "id": self.next_id("msg_"),
            "from": sender,
            "to": address,
            "subject": subject,
            "receivedAt": received_at if received_at is not None else int(time.time()),
            "text": text if text is not None else "Your code is 482913.",
            "html": html,
            "verificationCodes": list(codes if codes is not None else ["482913"]),
            "attachments": [],
        }
        # Newest first, as the API returns it.
        box.messages.insert(0, message)
        return message

    def reset(self) -> None:
        self.state = MockState()
        self.requests.clear()
        self.fail_next = None
        self.confirm_misses = 0
        self.redirect_next = 0


def _handler_for(api: MockApi) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # noqa: D102 - silence the console
            pass

        # -- plumbing --------------------------------------------------- #

        def _send(
            self, status: int, body: Any, extra: dict[str, str] | None = None, *, raw: bool = False
        ) -> None:
            payload = body if raw else json.dumps(body).encode("utf-8")
            if body is None and not raw:
                payload = b""
            self.send_response(status)
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            if not raw:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _metering(self) -> dict[str, str]:
            now = int(time.time())
            return {
                "X-RateLimit-Limit": str(api.plan["requestsPerMinute"]),
                "X-RateLimit-Remaining": str(
                    max(0, api.plan["requestsPerMinute"] - api.state.requests_this_minute)
                ),
                "X-RateLimit-Reset": str(now + 60),
                "X-Quota-Limit": str(api.plan["mailboxesPerDay"]),
                "X-Quota-Remaining": str(
                    max(0, api.plan["mailboxesPerDay"] - api.state.mailboxes_today)
                ),
                "X-Quota-Reset": str(now + 3600),
            }

        def _problem(
            self, code: str, status: int, detail: str, request_id: str, **extra: Any
        ) -> None:
            headers = {**self._metering()}
            if status == 429:
                headers["Retry-After"] = "2"
            else:
                headers["X-Request-Id"] = request_id
            body = {
                "type": f"https://nowtempmail.com/developers/errors#{code}",
                "code": code,
                "title": code.replace("_", " ").title(),
                "status": status,
                "detail": detail,
                "requestId": request_id,
                **extra,
            }
            self._send(status, body, headers)

        def _ok(self, body: Any, status: int = 200) -> None:
            headers = {**self._metering(), "X-Request-Id": self._rid}
            self._send(status, body, headers)

        # -- routing ----------------------------------------------------- #

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            self._rid = self.headers.get("X-Request-Id") or api.next_id("req_mock")
            api.requests.append(
                RecordedRequest(
                    method=method,
                    path=path,
                    request_id=self.headers.get("X-Request-Id"),
                    user_agent=self.headers.get("User-Agent"),
                )
            )

            if api.redirect_next > 0:
                api.redirect_next -= 1
                self._send(302, None, {"Location": "https://evil.test/stolen"})
                return

            # Out-of-band delivery hook. Not part of /v1 and never authenticated,
            # because it stands in for the mail receiver rather than for an API
            # caller -- inbound mail does not carry an API key either.
            #
            # It exists for the pytest-plugin tests, which run nested pytest
            # sessions in a separate process and so cannot reach this MockApi
            # object directly. Without it those tests could not deliver mail at
            # all, which would leave the watermark mechanism untested end to end.
            if path == "/__deliver" and method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                message = api.deliver(
                    body["address"],
                    codes=body.get("codes"),
                    subject=body.get("subject", "Your verification code"),
                    sender=body.get("sender", "Acme <noreply@acme.test>"),
                )
                self._send(200, {"delivered": message["id"]})
                return

            # The origin guard runs before auth, exactly as /v1 does.
            if self.headers.get("Origin"):
                self._problem(
                    "origin_not_allowed",
                    403,
                    "This API cannot be called from a browser.",
                    self._rid,
                )
                return

            if api.fail_next and api.fail_next.get("times", 0) > 0:
                api.fail_next["times"] -= 1
                code = api.fail_next.get("code", "internal_error")
                status = api.fail_next.get("status", 500)
                self._problem(code, status, "Injected failure.", self._rid)
                return

            auth = self.headers.get("Authorization")
            if auth != f"Bearer {MOCK_KEY}":
                self._problem("invalid_key", 401, "That key was not recognised.", self._rid)
                return

            is_peek = path == "/v1/me"
            if not is_peek:
                api.state.requests_this_minute += 1

            try:
                self._route(method, path, query)
            except _NotFoundError as exc:
                self._problem("not_found", 404, str(exc) or "No such thing.", self._rid)

        def _route(self, method: str, path: str, query: dict[str, list[str]]) -> None:
            parts = [unquote(p) for p in path.strip("/").split("/")]

            if path == "/v1/me" and method == "GET":
                return self._ok(self._me())
            if path == "/v1/domains" and method == "GET":
                return self._ok(
                    {
                        "domains": [
                            {"domain": DEFAULT_DOMAIN, "default": True},
                            {"domain": "mailvex.test", "default": False},
                        ]
                    }
                )
            if path == "/v1/mailboxes" and method == "POST":
                return self._create_mailbox()
            if path == "/v1/reservations" and method == "GET":
                return self._ok(
                    {
                        "reservations": api.state.reservations,
                        "limit": api.plan["reservedAddresses"],
                        "used": len(api.state.reservations),
                    }
                )

            # /v1/mailboxes/{address}[...]
            if len(parts) >= 3 and parts[0] == "v1" and parts[1] == "mailboxes":
                return self._mailbox_route(method, parts, query)

            raise _NotFoundError("No such route.")

        def _me(self) -> dict[str, Any]:
            now = int(time.time())
            return {
                "keyId": MOCK_KEY.split("_")[2],
                "environment": "live",
                "paywall": {"enabled": False, "note": "launch pricing"},
                "subscription": None,
                "plan": api.plan,
                "usage": {
                    "requestsThisMinute": api.state.requests_this_minute,
                    "mailboxesToday": api.state.mailboxes_today,
                    "mailboxesRemainingToday": max(
                        0, api.plan["mailboxesPerDay"] - api.state.mailboxes_today
                    ),
                    "reservedAddressesUsed": len(api.state.reservations),
                    "resetsAt": {"minute": now + 60, "day": now + 3600},
                },
                "webhookEndpoints": [],
            }

        def _create_mailbox(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}

            if api.state.mailboxes_today >= api.plan["mailboxesPerDay"]:
                return self._problem(
                    "quota_exceeded", 429, "The daily mailbox budget is spent.", self._rid
                )

            requested = body.get("address")
            if requested is not None and not api.plan["customAddress"]:
                return self._problem(
                    "plan_upgrade_required",
                    402,
                    "A custom address needs a paid plan.",
                    self._rid,
                    parameter="address",
                    plan=api.plan["id"],
                )
            ttl = body.get("ttlSeconds")
            if ttl is not None and (ttl < 300 or ttl > api.plan["maxTtlSeconds"]):
                return self._problem(
                    "plan_upgrade_required"
                    if ttl > api.plan["maxTtlSeconds"]
                    else "invalid_request",
                    402 if ttl > api.plan["maxTtlSeconds"] else 422,
                    "That TTL is out of range for this plan.",
                    self._rid,
                    parameter="ttlSeconds",
                    maxTtlSeconds=api.plan["maxTtlSeconds"],
                )

            local = requested or api.next_id("crystal")
            domain = body.get("domain") or DEFAULT_DOMAIN
            address = f"{local}@{domain}".lower()
            if address in api.state.mailboxes:
                return self._problem(
                    "address_taken",
                    409,
                    "A live mailbox already holds that address.",
                    self._rid,
                )

            now = int(time.time())
            ttl_seconds = ttl or 86_400
            box = MockMailbox(
                address=address,
                domain=domain,
                created_at=now,
                expires_at=now + ttl_seconds,
                ttl_seconds=ttl_seconds,
                reserved=bool(body.get("reserve")),
                webhook_url=body.get("webhookUrl"),
            )
            api.state.mailboxes[address] = box
            # Consumed on creation, and NEVER refunded on delete.
            api.state.mailboxes_today += 1
            self._ok(
                {
                    "address": box.address,
                    "domain": box.domain,
                    "createdAt": box.created_at,
                    "expiresAt": box.expires_at,
                    "ttlSeconds": box.ttl_seconds,
                    "reserved": box.reserved,
                    "webhookUrl": box.webhook_url,
                    "messageCount": 0,
                },
                201,
            )

        def _mailbox_route(
            self, method: str, parts: list[str], query: dict[str, list[str]]
        ) -> None:
            address = parts[2].lower()

            if len(parts) == 3:
                if method == "GET":
                    if api.confirm_misses > 0:
                        api.confirm_misses -= 1
                        raise _NotFoundError("Not readable yet.")
                    box = api.state.mailboxes.get(address)
                    if box is None:
                        raise _NotFoundError("No such mailbox.")
                    newest = box.messages[0]["receivedAt"] if box.messages else None
                    # Deliberately narrower than creation: no domain, ttlSeconds,
                    # reserved or webhookUrl.
                    return self._ok(
                        {
                            "address": box.address,
                            "createdAt": box.created_at,
                            "expiresAt": box.expires_at,
                            "messageCount": len(box.messages),
                            "lastMessageAt": newest,
                        }
                    )
                if method == "DELETE":
                    box = api.state.mailboxes.pop(address, None)
                    if box is None:
                        raise _NotFoundError("No such mailbox.")
                    # Quota is not refunded. That is the whole point.
                    return self._ok(
                        {"address": address, "deleted": True, "deletedMessages": len(box.messages)}
                    )

            box = api.state.mailboxes.get(address)
            if box is None:
                raise _NotFoundError("No such mailbox.")

            if len(parts) == 4 and parts[3] == "messages" and method == "GET":
                limit = int(query.get("limit", ["25"])[0])
                cursor = query.get("cursor", [None])[0]
                start = int(cursor) if cursor else 0
                window = box.messages[start : start + limit]
                has_more = start + limit < len(box.messages)
                return self._ok(
                    {
                        "messages": [
                            {
                                "id": m["id"],
                                "from": m["from"],
                                "subject": m["subject"],
                                "receivedAt": m["receivedAt"],
                                "attachmentCount": len(m["attachments"]),
                                "verificationCodes": m["verificationCodes"],
                            }
                            for m in window
                        ],
                        "hasMore": has_more,
                        "nextCursor": str(start + limit) if has_more else None,
                    }
                )

            if len(parts) == 5 and parts[3] == "messages":
                message = next((m for m in box.messages if m["id"] == parts[4]), None)
                if message is None:
                    raise _NotFoundError("No such message.")
                if method == "GET":
                    return self._ok(message)
                if method == "DELETE":
                    box.messages.remove(message)
                    return self._ok({"id": message["id"], "deleted": True})

            raise _NotFoundError("No such route.")

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._dispatch("DELETE")

    return Handler


class _NotFoundError(Exception):
    """Internal control flow: unwinds to a 404. Never escapes the handler."""


@contextmanager
def mock_api(**kwargs: Any) -> Iterator[MockApi]:
    """Start the server, yield the handle, shut it down."""
    api = MockApi(**kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for(api))
    api.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
