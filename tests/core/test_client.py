"""Client construction, create-then-confirm, and the https rule."""

from __future__ import annotations

import pytest
from helpers.mock_api import MOCK_KEY, MockApi

from nowtempmail import NowTempMail
from nowtempmail.errors import (
    ConfigError,
    MailboxMayExistError,
    NotFoundError,
    NowTempMailError,
    PlanUpgradeRequiredError,
    QuotaExceededError,
)


def client(api: MockApi, **kwargs: object) -> NowTempMail:
    return NowTempMail(MOCK_KEY, base_url=api.url, **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Construction                                                                 #
# --------------------------------------------------------------------------- #


def test_the_key_comes_from_the_environment(api: MockApi, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTM_API_KEY", MOCK_KEY)
    assert NowTempMail(base_url=api.url).me().key_id


def test_a_missing_key_names_where_to_get_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTM_API_KEY", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        NowTempMail()
    assert "NTM_API_KEY" in str(excinfo.value)
    assert "nowtempmail.com/developers" in str(excinfo.value)


def test_a_blank_key_is_treated_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTM_API_KEY", "   ")
    with pytest.raises(ConfigError):
        NowTempMail()


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "http://internal.corp", "http://10.0.0.1", "http://192.168.1.5"],
)
def test_http_is_refused_off_loopback(url: str) -> None:
    # Deliberately a literal-host check, not an "is it private" heuristic:
    # http://internal.corp/ puts a bearer token on a wire somebody else reads.
    with pytest.raises(ConfigError) as excinfo:
        NowTempMail(MOCK_KEY, base_url=url)
    assert "https" in str(excinfo.value)


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8787", "http://127.0.0.1:8787", "http://dev.localhost:1234"],
)
def test_http_is_allowed_on_an_explicit_loopback_host(url: str) -> None:
    assert NowTempMail(MOCK_KEY, base_url=url).base_url.startswith("http://")


def test_credentials_in_the_base_url_are_refused() -> None:
    with pytest.raises(ConfigError):
        NowTempMail(MOCK_KEY, base_url="https://user:pass@nowtempmail.com")


def test_a_nonsense_base_url_is_refused() -> None:
    with pytest.raises(ConfigError):
        NowTempMail(MOCK_KEY, base_url="not-a-url")


@pytest.mark.parametrize("bad", [0, -1, "thirty"])
def test_a_bad_timeout_is_refused(bad: object) -> None:
    with pytest.raises(ConfigError):
        NowTempMail(MOCK_KEY, base_url="https://nowtempmail.com", timeout=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, 1.5, True])
def test_a_bad_max_retries_is_refused(bad: object) -> None:
    with pytest.raises(ConfigError):
        NowTempMail(MOCK_KEY, base_url="https://nowtempmail.com", max_retries=bad)  # type: ignore[arg-type]


def test_the_repr_cannot_leak_the_key(api: MockApi) -> None:
    assert "ntm_live_" not in repr(client(api))


def test_a_trailing_slash_is_stripped(api: MockApi) -> None:
    assert NowTempMail(MOCK_KEY, base_url=api.url + "/").base_url == api.url


# --------------------------------------------------------------------------- #
# create-then-confirm                                                          #
# --------------------------------------------------------------------------- #


def test_create_confirms_before_returning(api: MockApi) -> None:
    box = client(api).mailboxes.create()
    assert box.confirmed is True
    paths = [r.path for r in api.requests]
    # POST, then a confirming GET on the created address.
    assert paths[-1].startswith("/v1/mailboxes/")
    assert any(r.method == "POST" for r in api.requests)


def test_create_still_succeeds_when_the_confirming_read_misses_twice(api: MockApi) -> None:
    api.confirm_misses = 2
    box = client(api).mailboxes.create()
    assert box.confirmed is True
    assert len([r for r in api.requests if r.method == "GET"]) == 3


def test_create_returns_unconfirmed_rather_than_raising_when_it_never_appears(
    api: MockApi,
) -> None:
    # A working mailbox with a slow read beats an exception over a mailbox that
    # exists and has already cost quota.
    api.confirm_misses = 99
    box = client(api).mailboxes.create()
    assert box.confirmed is False
    assert box.address


def test_create_is_never_retried(api: MockApi) -> None:
    api.fail_next = {"times": 5, "code": "internal_error", "status": 500}
    with pytest.raises(NowTempMailError):
        client(api, max_retries=3).mailboxes.create()
    assert len([r for r in api.requests if r.method == "POST"]) == 1


def test_a_transport_failure_on_create_raises_mailbox_may_exist() -> None:
    # Point at a closed port: the request goes out and the outcome is unknowable.
    ntm = NowTempMail(MOCK_KEY, base_url="http://127.0.0.1:1", timeout=1, max_retries=0)
    with pytest.raises(MailboxMayExistError) as excinfo:
        ntm.mailboxes.create()
    assert "will not send a second request" in str(excinfo.value)


def test_quota_exhaustion_is_reported_as_such(api: MockApi) -> None:
    api.plan["mailboxesPerDay"] = 1
    ntm = client(api)
    ntm.mailboxes.create()
    with pytest.raises(QuotaExceededError):
        ntm.mailboxes.create()


def test_a_gated_custom_address_names_the_parameter(api: MockApi) -> None:
    with pytest.raises(PlanUpgradeRequiredError) as excinfo:
        client(api).mailboxes.create(address="my-signup-test")
    assert excinfo.value.parameter == "address"
    assert excinfo.value.plan == "free"


def test_an_over_plan_ttl_carries_the_ceiling(api: MockApi) -> None:
    with pytest.raises(PlanUpgradeRequiredError) as excinfo:
        client(api).mailboxes.create(ttl_seconds=999_999)
    assert excinfo.value.max_ttl_seconds == 86_400


def test_create_accepts_keywords_or_a_params_object(api: MockApi) -> None:
    from nowtempmail import CreateMailboxParams

    ntm = client(api)
    a = ntm.mailboxes.create(ttl_seconds=900)
    b = ntm.mailboxes.create(CreateMailboxParams(ttl_seconds=900))
    assert a.ttl_seconds == b.ttl_seconds == 900
    with pytest.raises(TypeError):
        ntm.mailboxes.create(CreateMailboxParams(), ttl_seconds=900)


# --------------------------------------------------------------------------- #
# get / attach / delete                                                        #
# --------------------------------------------------------------------------- #


def test_get_verifies_with_one_read(api: MockApi) -> None:
    ntm = client(api)
    created = ntm.mailboxes.create()
    before = len(api.requests)
    fetched = ntm.mailboxes.get(created.address)
    assert fetched.confirmed is True
    assert len(api.requests) == before + 1


def test_get_on_a_missing_mailbox_is_not_found(api: MockApi) -> None:
    with pytest.raises(NotFoundError):
        client(api).mailboxes.get("nobody@nowhere.test")


def test_attach_makes_no_network_call(api: MockApi) -> None:
    ntm = client(api)
    before = len(api.requests)
    box = ntm.mailboxes.attach("Someone@Example.TEST")
    assert len(api.requests) == before
    assert box.address == "someone@example.test"
    assert box.confirmed is False
    # created_at at the epoch makes wait_for_* consider every existing message.
    assert box.created_at.year == 1970
    assert box.expires_in == float("inf")
    assert box.expired is False


def test_delete_reports_what_it_removed_and_refunds_nothing(api: MockApi) -> None:
    ntm = client(api)
    box = ntm.mailboxes.create()
    api.deliver(box.address)
    api.deliver(box.address)
    spent_before = api.state.mailboxes_today
    assert box.delete() == 2
    assert api.state.mailboxes_today == spent_before  # never decremented


# --------------------------------------------------------------------------- #
# me / domains                                                                 #
# --------------------------------------------------------------------------- #


def test_me_reports_plan_usage_and_paywall_state(api: MockApi) -> None:
    account = client(api).me()
    assert account.plan.mailboxes_per_day == 50
    assert account.paywall_enabled is False
    assert account.usage.mailboxes_remaining_today == 50
    assert account.usage.resets_at_day.tzinfo is not None


def test_me_does_not_spend_the_budget_it_reports_on(api: MockApi) -> None:
    ntm = client(api)
    for _ in range(5):
        ntm.me()
    assert api.state.requests_this_minute == 0


def test_domains_are_resolved_at_runtime(api: MockApi) -> None:
    domains = client(api).domains.list()
    assert len(domains) == 2
    assert sum(1 for d in domains if d.default) == 1
