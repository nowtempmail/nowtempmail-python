"""The transport. Everything that touches the network goes through :meth:`Http.request`.

Five properties of this module are load-bearing. None of them are style
preferences, and each has a test asserting it:

1. **Redirects are never followed.** ``urllib`` would otherwise replay the
   ``Authorization`` header to whatever host a ``Location`` names, and a bearer
   token that follows a redirect it did not choose is a leaked token.
   :class:`_NoRedirect` turns a 3xx into an error rather than a hop.
2. **``POST`` is never retried by default.** ``POST /v1/mailboxes`` is the only
   endpoint that consumes daily quota and the API has no idempotency key, so a
   blind retry can silently spend a second mailbox out of fifty. ``GET`` and
   ``DELETE`` retry.
3. **The ``Authorization`` header is assembled at the last possible moment** and
   is never in the mapping handed to ``on_request``. Hooks get a redacted copy.
4. **A generated ``X-Request-Id`` always matches**
   ``^req_[A-Za-z0-9_-]{16,32}$``, and a caller-supplied one is validated
   against the same pattern rather than forwarded verbatim -- this value is
   echoed into server logs, so an arbitrary string is a log-injection vector.
5. **Errors never carry a URL.** Method and path only, plus the request id,
   which is what support asks for.

Why the standard library and not ``httpx`` or ``requests``
----------------------------------------------------------
This package installs into test environments that already hold pytest,
Playwright, and usually an HTTP-client pin from some other SDK. A hard
dependency on one is a resolver conflict waiting to happen in exactly the
environments this package targets, and it buys very little: what is needed here
is JSON ``GET``/``POST``/``DELETE``, headers, a timeout, no redirects, and a
bytes download.

The cost is real and worth knowing: ``urllib`` has no connection pooling, so a
60-second wait at the 2-second poll floor opens about thirty TLS connections
rather than reusing one. That is small next to the sixty seconds of waiting and
it is confined to :meth:`Http.request`. **Do not "fix" it by adding a
dependency** without weighing that trade -- zero runtime dependencies is a
feature of this package, not an oversight.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

from .errors import (
    ConfigError,
    InternalError,
    NowTempMailError,
    TransportError,
    error_from_problem,
)
from .types import MeteringSnapshot, Quota, RateLimit, from_epoch

try:  # pragma: no cover - trivial, and the fallback only fires in a raw checkout
    from importlib.metadata import version as _pkg_version

    VERSION = _pkg_version("nowtempmail")
except Exception:  # PackageNotFoundError, or no metadata at all
    # An editable checkout that was never installed. The Node SDK hand-maintains
    # this constant and has a test keeping it honest; Python does not need that
    # workaround, so the only way to reach here is to run from source.
    VERSION = "0.0.0+local"

#: The shape ``X-Request-Id`` must have. Deliberately narrow -- see rule 4.
REQUEST_ID_PATTERN = re.compile(r"^req_[A-Za-z0-9_-]{16,32}$")

#: The floor on a derived poll interval, in milliseconds. See :meth:`Http.poll_interval_ms`.
MIN_POLL_INTERVAL_MS = 2_000

_RID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

#: A header value cannot carry CR, LF or NUL, and an over-long one is its own
#: problem. Callers supply the suffix, sometimes from a CI variable.
_HEADER_UNSAFE = re.compile(r"[^\t\x20-\x7e]")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def generate_request_id() -> str:
    """A fresh ``req_…`` id: 21 URL-safe characters, so 126 bits of entropy."""
    return "req_" + "".join(secrets.choice(_RID_ALPHABET) for _ in range(21))


def assert_valid_request_id(request_id: str) -> str:
    """Validate a caller-supplied request id, or raise :class:`ConfigError`."""
    if not REQUEST_ID_PATTERN.match(request_id):
        raise ConfigError(
            f"request_id must match {REQUEST_ID_PATTERN.pattern}. Received a value of "
            f"length {len(request_id)}. Request ids are echoed into server logs, so "
            "this SDK will not forward an arbitrary string."
        )
    return request_id


def sanitise_header_value(value: str, *, limit: int = 128) -> str:
    """Strip anything a header cannot carry, and cap the length."""
    return _HEADER_UNSAFE.sub("", value)[:limit]


def build_user_agent(suffix: str | None = None) -> str:
    py = f"{sys.version_info.major}.{sys.version_info.minor}"
    ua = f"nowtempmail-python/{VERSION} python/{py}"
    cleaned = sanitise_header_value(suffix.strip()) if suffix else ""
    return f"{ua} {cleaned}" if cleaned else ua


def parse_retry_after(raw: str | None) -> float | None:
    """``Retry-After`` in seconds. Both the delta-seconds and HTTP-date forms."""
    if raw is None:
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return seconds if seconds >= 0 else None
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
    except Exception:
        return None
    if when is None:
        return None
    import datetime as _dt

    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return max(0.0, (when - _dt.datetime.now(_dt.timezone.utc)).total_seconds())


def read_metering(headers: Mapping[str, str]) -> MeteringSnapshot:
    """Read ``X-RateLimit-*`` / ``X-Quota-*`` off a response."""

    def num(name: str) -> float | None:
        raw = headers.get(name)
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    rl_limit, rl_remaining, rl_reset = (
        num("X-RateLimit-Limit"),
        num("X-RateLimit-Remaining"),
        num("X-RateLimit-Reset"),
    )
    q_limit, q_remaining, q_reset = (
        num("X-Quota-Limit"),
        num("X-Quota-Remaining"),
        num("X-Quota-Reset"),
    )
    rate_limit = None
    if rl_limit is not None and rl_remaining is not None and rl_reset is not None:
        reset = from_epoch(rl_reset)
        assert reset is not None
        rate_limit = RateLimit(int(rl_limit), int(rl_remaining), reset)
    quota = None
    if q_limit is not None and q_remaining is not None and q_reset is not None:
        reset = from_epoch(q_reset)
        assert reset is not None
        quota = Quota(int(q_limit), int(q_remaining), reset)
    return MeteringSnapshot(rate_limit=rate_limit, quota=quota)


def sleep(seconds: float, cancel: threading.Event | None = None) -> None:
    """Interruptible sleep. Raises :class:`TransportError` if cancelled.

    ``Event.wait`` rather than ``time.sleep`` so a caller holding the cancel
    token can stop a sixty-second wait immediately instead of at the next poll.
    """
    if seconds <= 0:
        return
    if cancel is None:
        time.sleep(seconds)
        return
    if cancel.wait(seconds):
        raise TransportError("connection_error", "The request was cancelled.")


def raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise TransportError("connection_error", "The wait was cancelled.")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect.

    Returning ``None`` from ``redirect_request`` leaves the 3xx unhandled, so
    ``urllib`` falls through to the default error handler and raises rather than
    replaying the ``Authorization`` header to a host the API chose.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _Response:
    """What :meth:`Http.request` returns."""

    __slots__ = ("data", "status", "request_id", "metering", "headers")

    def __init__(
        self,
        data: Any,
        status: int,
        request_id: str,
        metering: MeteringSnapshot,
        headers: Mapping[str, str],
    ) -> None:
        self.data = data
        self.status = status
        self.request_id = request_id
        self.metering = metering
        self.headers = headers


class Http:
    """The transport. One per client."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        max_retries: int = 2,
        user_agent_suffix: str | None = None,
        low_quota_threshold: int = 5,
        on_request: Callable[[dict[str, Any]], None] | None = None,
        on_response: Callable[[dict[str, Any]], None] | None = None,
        on_low_quota: Callable[[Quota], None] | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = build_user_agent(user_agent_suffix)
        self._low_quota_threshold = low_quota_threshold
        self._on_request = on_request
        self._on_response = on_response
        self._on_low_quota = on_low_quota

        self._metering = MeteringSnapshot()
        self._watchers = 0
        self._watcher_lock = threading.Lock()
        self._last_low_quota_notified: int | None = None
        self._opener = urllib.request.build_opener(_NoRedirect)

    # -- metering ---------------------------------------------------------- #

    @property
    def metering(self) -> MeteringSnapshot:
        """The most recent snapshot.

        A response that omits a header set -- an attachment download carries no
        rate-limit headers -- leaves the previous value in place rather than
        clearing it.
        """
        return self._metering

    @property
    def watchers(self) -> int:
        """How many ``wait_for_*`` loops are currently sharing the rate limit."""
        return self._watchers

    def poll_interval_ms(self, watchers: int = 1) -> int:
        """An interval that keeps ``watchers`` concurrent watchers inside the
        key's per-minute budget, floored at :data:`MIN_POLL_INTERVAL_MS`.

        A Free key gets 30 requests a minute, so one watcher lands exactly on the
        2s floor and two watchers get 4s. **Without the division, N watchers on
        one key rate-limit each other and every one of them slows down.**
        """
        limit = self._metering.rate_limit.limit if self._metering.rate_limit else 30
        n = max(1, int(watchers))
        ideal = (60_000 * n) / limit if limit > 0 else MIN_POLL_INTERVAL_MS
        return max(MIN_POLL_INTERVAL_MS, int(-(-ideal // 1)))

    def register_watcher(self) -> Callable[[], None]:
        """Count a watcher in. The returned callable deregisters it, once."""
        with self._watcher_lock:
            self._watchers += 1
        released = threading.Event()

        def release() -> None:
            if released.is_set():
                return
            released.set()
            with self._watcher_lock:
                self._watchers = max(0, self._watchers - 1)

        return release

    # -- request ----------------------------------------------------------- #

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        retry: bool | None = None,
        raw: bool = False,
        request_id: str | None = None,
        cancel: threading.Event | None = None,
    ) -> _Response:
        rid = generate_request_id() if request_id is None else assert_valid_request_id(request_id)
        url = self._url(path, query)

        # POST is opt-in for retries, never opt-out-by-accident. See rule 2.
        retry_enabled = (method != "POST") if retry is None else retry
        max_attempts = max(0, self.max_retries) + 1 if retry_enabled else 1

        safe_headers: dict[str, str] = {
            "Accept": (
                "application/octet-stream, application/problem+json"
                if raw
                else "application/json, application/problem+json"
            ),
            "User-Agent": self.user_agent,
            "X-Request-Id": rid,
        }
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            safe_headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            attempt += 1
            raise_if_cancelled(cancel)
            self._fire(
                self._on_request,
                {
                    "method": method,
                    "path": path,
                    "request_id": rid,
                    "attempt": attempt,
                    # Built before Authorization exists, so there is no chance of
                    # leaking it, and copied so a hook cannot mutate the next attempt.
                    "headers": dict(safe_headers),
                },
            )

            started = time.monotonic()
            # Assembled last, and only into the object that goes on the wire.
            wire_headers = {**safe_headers, "Authorization": f"Bearer {self._api_key}"}
            req = urllib.request.Request(url, data=payload, headers=wire_headers, method=method)

            try:
                with self._opener.open(req, timeout=self.timeout) as response:
                    status = int(response.status)
                    headers = {k: v for k, v in response.headers.items()}
                    payload_bytes = response.read()
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                headers = {k: v for k, v in (exc.headers or {}).items()}
                try:
                    payload_bytes = exc.read()
                except Exception:
                    payload_bytes = b""
                finally:
                    # HTTPError IS a response object holding an open socket, not
                    # just an exception describing one -- the success path gets
                    # this free from its `with`, and this path does not.
                    #
                    # Without the close, every error response leaks a descriptor
                    # until the garbage collector notices. Refcounting usually
                    # collects it quickly, which is why this went unseen on 3.10
                    # through 3.13; Python 3.14 emits ResourceWarning for it, and
                    # this suite turns warnings into errors, so the whole 3.14
                    # matrix leg failed. That is the leg earning its place: a
                    # real leak, found because a version we do not develop on was
                    # in the matrix.
                    #
                    # It matters most exactly where errors repeat -- a retry loop
                    # waiting out 429s accumulates one per attempt.
                    exc.close()
                metering = self._absorb_metering(headers)
                self._fire_response(method, path, rid, status, started, metering)

                if 300 <= status < 400:
                    raise self._redirect_error(method, path, rid, status) from exc

                retry_after = parse_retry_after(headers.get("Retry-After"))
                problem = _read_problem(payload_bytes, status, headers.get("X-Request-Id") or rid)
                error = error_from_problem(
                    problem, method=method, path=path, retry_after=retry_after
                )
                if retry_enabled and attempt < max_attempts and status in _RETRYABLE_STATUS:
                    # On 429 the server told us exactly how long to wait. Honour
                    # it rather than guessing with a backoff curve -- it knows
                    # when the fixed minute window resets and we do not.
                    wait = (
                        retry_after
                        if status == 429 and retry_after is not None
                        else self._backoff(attempt)
                    )
                    sleep(wait, cancel)
                    continue
                raise error from None
            except urllib.error.URLError as exc:
                error = self._transport_error(exc, method, path, rid)
                if retry_enabled and attempt < max_attempts:
                    sleep(self._backoff(attempt), cancel)
                    continue
                raise error from exc
            except (TimeoutError, OSError) as exc:
                # A socket timeout surfaces as builtins.TimeoutError on 3.10+
                # rather than through URLError, and a torn-down connection as a
                # bare OSError. Both are the same thing to a caller.
                error = self._transport_error(exc, method, path, rid)
                if retry_enabled and attempt < max_attempts:
                    sleep(self._backoff(attempt), cancel)
                    continue
                raise error from exc

            metering = self._absorb_metering(headers)
            self._fire_response(method, path, rid, status, started, metering)
            echoed = headers.get("X-Request-Id") or rid
            data = _read_success(payload_bytes, status, raw, method, path, echoed)
            return _Response(data, status, echoed, metering, headers)

    # -- internals --------------------------------------------------------- #

    def _url(self, path: str, query: Mapping[str, Any] | None) -> str:
        url = f"{self.base_url}{path}"
        if query:
            pairs = [(k, str(v)) for k, v in query.items() if v is not None]
            if pairs:
                url = f"{url}?{urlencode(pairs)}"
        return url

    def _backoff(self, attempt: int) -> float:
        """Bounded exponential with full-ish jitter, so N clients that failed
        together do not retry together."""
        # `2 ** n` is int, so min() widens to Any without the float() here.
        base = min(8.0, float(0.25 * (2 ** (attempt - 1))))
        return base * (0.5 + random.random() * 0.5)

    def _redirect_error(
        self, method: str, path: str, request_id: str, status: int
    ) -> NowTempMailError:
        return TransportError(
            "connection_error",
            f"{method} {path} responded with HTTP {status}, a redirect. This SDK "
            "refuses to follow one, because doing so would replay your API key to "
            "a host the API chose rather than one you configured.",
            method=method,
            path=path,
            request_id=request_id,
            status=status,
        )

    def _transport_error(
        self, cause: BaseException, method: str, path: str, request_id: str
    ) -> NowTempMailError:
        reason = getattr(cause, "reason", None) or cause
        return TransportError(
            "connection_error",
            f"{method} {path} failed before a response was received: {reason}",
            method=method,
            path=path,
            request_id=request_id,
        )

    def _absorb_metering(self, headers: Mapping[str, str]) -> MeteringSnapshot:
        seen = read_metering(headers)
        merged = MeteringSnapshot(
            rate_limit=seen.rate_limit or self._metering.rate_limit,
            quota=seen.quota or self._metering.quota,
        )
        self._metering = merged

        quota = seen.quota
        if (
            quota is not None
            and self._on_low_quota is not None
            and quota.remaining < self._low_quota_threshold
            and quota.remaining != self._last_low_quota_notified
        ):
            self._last_low_quota_notified = quota.remaining
            self._fire(self._on_low_quota, quota)
        return merged

    def _fire_response(
        self,
        method: str,
        path: str,
        request_id: str,
        status: int,
        started: float,
        metering: MeteringSnapshot,
    ) -> None:
        self._fire(
            self._on_response,
            {
                "method": method,
                "path": path,
                "request_id": request_id,
                "status": status,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "metering": metering,
            },
        )

    @staticmethod
    def _fire(hook: Callable[[Any], None] | None, event: Any) -> None:
        """A raising hook is the caller's bug, not a failed request."""
        if hook is None:
            return
        with contextlib.suppress(Exception):
            hook(event)


# --------------------------------------------------------------------------- #
# Body readers                                                                 #
# --------------------------------------------------------------------------- #

_CODE_SHAPE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    402: "plan_upgrade_required",
    403: "forbidden",
    404: "not_found",
    409: "address_taken",
    422: "invalid_request",
    429: "rate_limit_exceeded",
}


def _code_for_status(status: int) -> str:
    if status in _STATUS_CODES:
        return _STATUS_CODES[status]
    return "internal_error" if status >= 500 else "invalid_request"


def _read_problem(payload: bytes, status: int, fallback_request_id: str) -> dict[str, Any]:
    """Coerce any error response into a problem document.

    The API always sends RFC 9457. Intermediaries do not: a 502 from a proxy is
    an HTML page, and a truncated response is nothing at all. Either way this
    must produce a usable error carrying a request id, never a parse crash.
    """
    raw: dict[str, Any] = {}
    text = ""
    if payload:
        try:
            text = payload.decode("utf-8", errors="replace")
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            raw = parsed

    code = raw.get("code")
    # Accept a code we have never heard of, as long as it is shaped like one --
    # a server growing a fifteenth code must not break older clients. The shape
    # check only stops a garbled body putting arbitrary text on err.code, which
    # callers branch on.
    if not (isinstance(code, str) and _CODE_SHAPE.match(code)):
        code = _code_for_status(status)

    detail = raw.get("detail") or raw.get("title")
    if not isinstance(detail, str) or not detail:
        detail = f"The API returned HTTP {status} with " + (
            "a body that is not a problem document." if text else "an empty body."
        )

    problem: dict[str, Any] = {
        "type": raw.get("type") or f"https://nowtempmail.com/developers/errors#{code}",
        "code": code,
        "title": raw.get("title") if isinstance(raw.get("title"), str) else f"HTTP {status}",
        "status": raw.get("status") if isinstance(raw.get("status"), int) else status,
        "detail": detail,
        # Never absent. Support asks for this value, and an error without one is
        # an error nobody can chase.
        "requestId": raw.get("requestId") or fallback_request_id,
    }
    for key in ("parameter", "plan", "maxTtlSeconds", "endpointId"):
        if key in raw:
            problem[key] = raw[key]
    return problem


def _read_success(
    payload: bytes, status: int, raw: bool, method: str, path: str, request_id: str
) -> Any:
    if raw:
        return payload
    if status == 204 or not payload:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise InternalError(
            "internal_error",
            f"{method} {path} returned HTTP {status} with a body that is not JSON.",
            status=status,
            request_id=request_id,
            method=method,
            path=path,
        ) from exc
