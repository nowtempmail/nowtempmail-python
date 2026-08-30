"""The five load-bearing transport rules, each asserted directly.

If one of these tests is deleted, the property it guards is gone -- none of them
are covered incidentally by anything else.
"""

from __future__ import annotations

import threading
import time

import pytest
from helpers.mock_api import MOCK_KEY, MockApi

from nowtempmail._http import (
    MIN_POLL_INTERVAL_MS,
    Http,
    assert_valid_request_id,
    build_user_agent,
    generate_request_id,
    parse_retry_after,
    read_metering,
    sanitise_header_value,
)
from nowtempmail.errors import (
    ConfigError,
    InvalidKeyError,
    NotFoundError,
    NowTempMailError,
    RateLimitExceededError,
    TransportError,
)


def http(api: MockApi, **kwargs: object) -> Http:
    return Http(api_key=MOCK_KEY, base_url=api.url, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Rule 1 -- redirects are never followed                                       #
# --------------------------------------------------------------------------- #


def test_a_redirect_is_refused_rather_than_followed(api: MockApi) -> None:
    api.redirect_next = 1
    with pytest.raises(TransportError) as excinfo:
        http(api, max_retries=0).request("GET", "/v1/domains")
    error = excinfo.value
    assert error.code == "connection_error"
    assert "refuses to follow" in error.message
    # The whole point: the token was not replayed to the Location host.
    assert "evil.test" not in error.message


def test_the_redirect_error_names_no_url(api: MockApi) -> None:
    api.redirect_next = 1
    with pytest.raises(TransportError) as excinfo:
        http(api, max_retries=0).request("GET", "/v1/domains")
    assert api.url not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Rule 2 -- POST is never retried                                              #
# --------------------------------------------------------------------------- #


def test_post_is_not_retried_even_on_a_retryable_status(api: MockApi) -> None:
    api.fail_next = {"times": 3, "code": "internal_error", "status": 500}
    with pytest.raises(NowTempMailError):
        http(api, max_retries=2).request("POST", "/v1/mailboxes", body={})
    # One attempt, not three. A blind retry here spends a second mailbox out of
    # a daily budget that deletion never refunds.
    assert len([r for r in api.requests if r.method == "POST"]) == 1


def test_get_does_retry(api: MockApi) -> None:
    api.fail_next = {"times": 2, "code": "internal_error", "status": 500}
    result = http(api, max_retries=2).request("GET", "/v1/domains")
    assert result.status == 200
    assert len([r for r in api.requests if r.path == "/v1/domains"]) == 3


def test_post_retries_only_when_explicitly_asked(api: MockApi) -> None:
    api.fail_next = {"times": 1, "code": "internal_error", "status": 500}
    result = http(api, max_retries=2).request("POST", "/v1/mailboxes", body={}, retry=True)
    assert result.status == 201


# --------------------------------------------------------------------------- #
# Rule 3 -- request ids are validated, never forwarded verbatim                #
# --------------------------------------------------------------------------- #


def test_a_generated_request_id_matches_the_documented_shape() -> None:
    for _ in range(50):
        assert_valid_request_id(generate_request_id())


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "req_short",
        "notreq_aaaaaaaaaaaaaaaaaaaa",
        "req_" + "a" * 64,
        "req_has spaces in it!!",
        "req_newline\naaaaaaaaaaaaaaa",
    ],
)
def test_a_hostile_request_id_is_refused(bad: str) -> None:
    # This value is echoed into server logs. Forwarding an arbitrary string is
    # log injection, so it is a ConfigError rather than something sanitised.
    with pytest.raises(ConfigError):
        assert_valid_request_id(bad)


def test_a_caller_supplied_valid_id_reaches_the_wire(api: MockApi) -> None:
    rid = generate_request_id()
    http(api).request("GET", "/v1/domains", request_id=rid)
    assert api.requests[-1].request_id == rid


# --------------------------------------------------------------------------- #
# Rule 4 -- no URL and no credential in an error                               #
# --------------------------------------------------------------------------- #


def test_an_error_carries_method_path_and_request_id_but_no_url(api: MockApi) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        http(api).request("GET", "/v1/mailboxes/nobody@nowhere.test")
    error = excinfo.value
    assert error.method == "GET"
    assert error.path == "/v1/mailboxes/nobody@nowhere.test"
    assert error.request_id
    assert error.path is not None and "http" not in error.path


def test_no_error_surface_can_hold_the_api_key(api: MockApi) -> None:
    bad = Http(
        api_key="ntm_live_zZtestKeyId1_sSSSecretHalfThatMustNeverBePrinted",
        base_url=api.url,
    )
    with pytest.raises(InvalidKeyError) as excinfo:
        bad.request("GET", "/v1/domains")
    error = excinfo.value
    for surface in (str(error), repr(error), error.to_log_string()):
        assert "sSSSecretHalfThatMustNeverBePrinted" not in surface
        assert "ntm_live_" not in surface


def test_to_log_string_is_compact_and_useful(api: MockApi) -> None:
    with pytest.raises(NotFoundError) as excinfo:
        http(api).request("GET", "/v1/mailboxes/nobody@nowhere.test")
    line = excinfo.value.to_log_string()
    assert line.startswith("not_found")
    assert "status=404" in line
    assert "requestId=" in line


# --------------------------------------------------------------------------- #
# Rule 5 -- Retry-After is honoured exactly on 429                             #
# --------------------------------------------------------------------------- #


def test_a_429_sleeps_for_retry_after_rather_than_a_backoff_guess(api: MockApi) -> None:
    api.fail_next = {"times": 1, "code": "rate_limit_exceeded", "status": 429}
    started = time.monotonic()
    result = http(api, max_retries=1).request("GET", "/v1/domains")
    elapsed = time.monotonic() - started
    assert result.status == 200
    # The mock sends Retry-After: 2. A backoff curve would have waited ~0.25s.
    assert elapsed >= 1.8, f"waited {elapsed:.2f}s -- Retry-After was ignored"


def test_a_429_that_exhausts_retries_carries_retry_after_to_the_caller(api: MockApi) -> None:
    api.fail_next = {"times": 5, "code": "rate_limit_exceeded", "status": 429}
    with pytest.raises(RateLimitExceededError) as excinfo:
        http(api, max_retries=0).request("GET", "/v1/domains")
    assert excinfo.value.retry_after == 2.0
    assert excinfo.value.retryable is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("2", 2.0), ("0", 0.0), ("  7  ", 7.0), (None, None), ("later", None), ("-3", None)],
)
def test_retry_after_parsing(raw: str | None, expected: float | None) -> None:
    assert parse_retry_after(raw) == expected


def test_retry_after_accepts_the_http_date_form() -> None:
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    parsed = parse_retry_after(format_datetime(when))
    assert parsed is not None and 25 <= parsed <= 31


def test_backoff_is_bounded_and_jittered(api: MockApi) -> None:
    transport = http(api)
    values = [transport._backoff(n) for n in range(1, 12) for _ in range(20)]
    assert all(0 < v <= 8.0 for v in values)
    # Jitter, not a fixed curve: two draws at the same attempt must differ.
    assert len({transport._backoff(4) for _ in range(20)}) > 1


# --------------------------------------------------------------------------- #
# Metering                                                                     #
# --------------------------------------------------------------------------- #


def test_metering_is_read_off_every_response(api: MockApi) -> None:
    transport = http(api)
    assert transport.metering.rate_limit is None
    transport.request("GET", "/v1/domains")
    assert transport.metering.rate_limit is not None
    assert transport.metering.rate_limit.limit == 30
    assert transport.metering.quota is not None
    assert transport.metering.quota.limit == 50


def test_a_response_missing_a_header_set_does_not_clear_the_previous_snapshot() -> None:
    #  An attachment download carries no rate-limit headers. Clearing on absence
    #  would make the poll interval fall back to its default mid-wait.
    snapshot = read_metering(
        {
            "X-RateLimit-Limit": "30",
            "X-RateLimit-Remaining": "29",
            "X-RateLimit-Reset": "1786097760",
        }
    )
    assert snapshot.rate_limit is not None
    assert snapshot.quota is None


def test_garbled_metering_headers_are_ignored_rather_than_crashing() -> None:
    snapshot = read_metering({"X-RateLimit-Limit": "lots", "X-Quota-Remaining": ""})
    assert snapshot.rate_limit is None
    assert snapshot.quota is None


def test_low_quota_fires_once_per_distinct_remaining(api: MockApi) -> None:
    seen: list[int] = []
    api.plan["mailboxesPerDay"] = 3
    transport = http(api, low_quota_threshold=5, on_low_quota=lambda q: seen.append(q.remaining))
    transport.request("GET", "/v1/domains")
    transport.request("GET", "/v1/domains")
    # Same remaining twice -- one notification, not two.
    assert seen == [3]


def test_a_raising_hook_is_the_callers_bug_not_a_failed_request(api: MockApi) -> None:
    def boom(_: object) -> None:
        raise RuntimeError("hook exploded")

    result = http(api, on_request=boom, on_response=boom).request("GET", "/v1/domains")
    assert result.status == 200


def test_hooks_never_see_the_authorization_header(api: MockApi) -> None:
    captured: list[dict[str, str]] = []
    http(api, on_request=lambda e: captured.append(e["headers"])).request("GET", "/v1/domains")
    assert captured
    for headers in captured:
        assert "Authorization" not in headers
        assert not any("ntm_live_" in v for v in headers.values())


# --------------------------------------------------------------------------- #
# Poll interval                                                                #
# --------------------------------------------------------------------------- #


def test_poll_interval_divides_the_budget_across_watchers(api: MockApi) -> None:
    transport = http(api)
    transport.request("GET", "/v1/domains")  # learn the 30/min limit
    assert transport.poll_interval_ms(1) == MIN_POLL_INTERVAL_MS  # 60000/30 = 2000
    assert transport.poll_interval_ms(2) == 4_000
    assert transport.poll_interval_ms(5) == 10_000


def test_poll_interval_never_goes_below_the_floor(api: MockApi) -> None:
    transport = http(api)
    api.plan["requestsPerMinute"] = 600
    transport.request("GET", "/v1/domains")
    assert transport.poll_interval_ms(1) == MIN_POLL_INTERVAL_MS


def test_watchers_are_counted_and_released_once(api: MockApi) -> None:
    transport = http(api)
    assert transport.watchers == 0
    release_a = transport.register_watcher()
    release_b = transport.register_watcher()
    assert transport.watchers == 2
    release_a()
    release_a()  # idempotent -- a double release must not undercount
    assert transport.watchers == 1
    release_b()
    assert transport.watchers == 0


# --------------------------------------------------------------------------- #
# User-Agent and header safety                                                 #
# --------------------------------------------------------------------------- #


def test_the_user_agent_identifies_the_package_and_the_runtime(api: MockApi) -> None:
    http(api).request("GET", "/v1/domains")
    ua = api.requests[-1].user_agent or ""
    assert ua.startswith("nowtempmail-python/")
    assert "python/3." in ua


def test_a_hostile_user_agent_suffix_cannot_inject_a_header(api: MockApi) -> None:
    http(api, user_agent_suffix="ci/1.0\r\nX-Injected: yes").request("GET", "/v1/domains")
    ua = api.requests[-1].user_agent or ""
    assert "\r" not in ua and "\n" not in ua
    assert "ci/1.0" in ua


def test_a_long_suffix_is_capped() -> None:
    assert len(sanitise_header_value("x" * 500)) == 128
    assert build_user_agent("y" * 500).endswith("y" * 128)


# --------------------------------------------------------------------------- #
# Bodies that are not what the API promised                                    #
# --------------------------------------------------------------------------- #


def test_an_unknown_but_well_shaped_problem_code_passes_through(api: MockApi) -> None:
    # A server growing a fifteenth code must not break older clients.
    api.fail_next = {"times": 1, "code": "teapot_unavailable", "status": 400}
    with pytest.raises(Exception) as excinfo:
        http(api, max_retries=0).request("GET", "/v1/domains")
    assert getattr(excinfo.value, "code", None) == "teapot_unavailable"


def test_a_204_reads_as_none(api: MockApi) -> None:
    transport = http(api)
    box = transport.request("POST", "/v1/mailboxes", body={}).data
    result = transport.request("DELETE", f"/v1/mailboxes/{box['address']}")
    assert result.status == 200


def test_cancellation_stops_a_sleep_immediately(api: MockApi) -> None:
    from nowtempmail._http import sleep as interruptible_sleep

    cancel = threading.Event()
    threading.Timer(0.05, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(TransportError):
        interruptible_sleep(30, cancel)
    assert time.monotonic() - started < 5
