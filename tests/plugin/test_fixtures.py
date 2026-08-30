"""The fixtures, driven end to end through real nested pytest sessions.

``pytester`` runs an actual pytest against the mock API, which is the only way
to prove the things that matter here: that the session inbox is created once,
that the autouse stamp really does re-watermark between tests, and that xdist
gets one mailbox per worker rather than one shared across processes.
"""

from __future__ import annotations

import pathlib

import pytest
from helpers.mock_api import MOCK_KEY, MockApi, mock_api

pytest_plugins = ["pytester"]


@pytest.fixture
def live(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch):
    """A running mock API, with the environment a nested session will read.

    Ordering matters: ``pytester`` is constructed first (it is a parameter), and
    part of what it does is delete ``PYTHONPATH`` to isolate the sessions it
    runs. Restoring it has to happen after that, which is why it lives here
    rather than in conftest -- an xdist worker is a clean subprocess and cannot
    import the package from source without it.
    """
    from conftest import SUBPROCESS_PYTHONPATH

    with mock_api() as api:
        monkeypatch.setenv("PYTHONPATH", SUBPROCESS_PYTHONPATH)
        monkeypatch.setenv("NTM_API_KEY", MOCK_KEY)
        monkeypatch.setenv("NTM_BASE_URL", api.url)
        pytester.makeini(
            f"""
            [pytest]
            ntm_base_url = {api.url}
            """
        )
        yield api


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest("-p", "nowtempmail.pytest_plugin", *args)


def _skip_unless_importable_from_a_clean_subprocess() -> None:
    """Skip unless `nowtempmail` is genuinely installed, not just on sys.path.

    The rest of this suite runs against `src/` via a sys.path insert in
    conftest, which deliberately keeps a bare checkout runnable with nothing but
    pytest -- proving the zero-dependency claim on a clean machine matters more
    than convenience here.

    xdist is the one thing that cannot work that way. Its workers are execnet
    gateways: fresh interpreters that bootstrap before any conftest runs and do
    not reliably inherit PYTHONPATH. So this test needs the package actually
    installed, which is what CI does and what a user does. Skipping is honest;
    faking it by injecting a path the worker may or may not read would produce a
    test that passes without proving anything.
    """
    import subprocess
    import sys

    probe = subprocess.run(
        [sys.executable, "-c", "import nowtempmail"],
        capture_output=True,
        cwd=str(pathlib.Path(__file__).resolve().parent),
    )
    if probe.returncode != 0:
        pytest.skip(
            "nowtempmail is not installed in this interpreter, so xdist workers "
            "cannot import it. Run `pip install -e .` to exercise this test."
        )


# --------------------------------------------------------------------------- #
# Sharing                                                                      #
# --------------------------------------------------------------------------- #


def test_the_session_inbox_is_created_once_for_the_whole_run(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    # POST /v1/mailboxes is the only endpoint that spends daily quota and the
    # counter is never decremented, so a per-test mailbox burns the Free plan's
    # 50/day inside one CI run.
    pytester.makepyfile(
        """
        def test_a(ntm_inbox): assert ntm_inbox.address
        def test_b(ntm_inbox): assert ntm_inbox.address
        def test_c(ntm_inbox): assert ntm_inbox.address
        def test_d(ntm_inbox): assert ntm_inbox.address
        """
    )
    run(pytester, "-q").assert_outcomes(passed=4)
    assert live.state.mailboxes_today == 1, "the session inbox was not shared"


def test_every_test_sees_the_same_address(pytester: pytest.Pytester, live: MockApi) -> None:
    pytester.makepyfile(
        """
        seen = set()

        def test_a(ntm_inbox): seen.add(ntm_inbox.address)
        def test_b(ntm_inbox): seen.add(ntm_inbox.address)
        def test_c(ntm_inbox):
            seen.add(ntm_inbox.address)
            assert len(seen) == 1
        """
    )
    run(pytester, "-q").assert_outcomes(passed=3)


def test_a_fresh_inbox_costs_a_mailbox_per_test_and_is_deleted(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    pytester.makepyfile(
        """
        def test_a(ntm_fresh_inbox): assert ntm_fresh_inbox.address
        def test_b(ntm_fresh_inbox): assert ntm_fresh_inbox.address
        """
    )
    run(pytester, "-q").assert_outcomes(passed=2)
    assert live.state.mailboxes_today == 2
    # Deleted on teardown -- but the quota above stayed spent, which is the point.
    assert live.state.mailboxes == {}


# --------------------------------------------------------------------------- #
# Watermarking, in a real session                                              #
# --------------------------------------------------------------------------- #


def test_a_later_test_does_not_resolve_with_an_earlier_tests_code(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    # THE failure this whole mechanism exists to prevent: without the per-test
    # watermark, test_second happily returns the code test_first triggered and
    # the suite passes while testing nothing.
    pytester.makepyfile(
        f"""
        import json, time, urllib.request
        import pytest
        from nowtempmail import WaitTimeoutError

        BASE = {live.url!r}

        def deliver(address, codes):
            # The nested session runs in this process but cannot reach the outer
            # MockApi object, so it uses the out-of-band delivery hook -- the
            # same shape the real mail receiver has.
            body = json.dumps({{"address": address, "codes": codes}}).encode()
            req = urllib.request.Request(
                BASE + "/__deliver", data=body, method="POST",
                headers={{"Content-Type": "application/json"}},
            )
            urllib.request.urlopen(req).read()

        def test_first(ntm_inbox):
            deliver(ntm_inbox.address, ["111111"])
            assert ntm_inbox.wait_for_code(poll_interval=0.02, timeout=5) == "111111"
            # Sleep HERE, at the end of the first test, not at the start of the
            # second: the watermark is stamped when a test begins, so sleeping
            # inside test_second would happen after its boundary was already
            # fixed and would prove nothing. This puts test_second's start more
            # than the one-second skew past test_first's mail.
            time.sleep(2.5)

        def test_second(ntm_inbox):
            # Nothing new is delivered, and the mailbox still holds test_first's
            # code. Without the per-test watermark this returns "111111" and the
            # suite passes while testing nothing.
            with pytest.raises(WaitTimeoutError) as excinfo:
                ntm_inbox.wait_for_code(poll_interval=0.02, timeout=1.5)
            assert excinfo.value.messages_seen == 1
        """
    )
    result = run(pytester, "-q")
    result.assert_outcomes(passed=2)


def test_two_tests_inside_one_second_can_still_collide(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    """The documented limit of the mechanism, pinned so it cannot drift silently.

    The watermark is floored to the second and set a second behind, so two tests
    that trigger mail within about a second of each other are NOT isolated from
    one another -- the second one can match the first one's mail. That is the
    price of the skew, which exists because a CI clock is not the API's clock.

    Users hit this in real suites, so it is documented on ``ntm_inbox`` with the
    fix: add ``sender`` or ``subject`` to the filter. This test exists so that
    if someone later "improves" the watermark, they see which guarantee they
    changed rather than discovering it from a bug report.
    """
    pytester.makepyfile(
        f"""
        import json, urllib.request

        BASE = {live.url!r}

        def deliver(address, codes):
            body = json.dumps({{"address": address, "codes": codes}}).encode()
            req = urllib.request.Request(
                BASE + "/__deliver", data=body, method="POST",
                headers={{"Content-Type": "application/json"}},
            )
            urllib.request.urlopen(req).read()

        def test_first(ntm_inbox):
            deliver(ntm_inbox.address, ["111111"])
            assert ntm_inbox.wait_for_code(poll_interval=0.02, timeout=5) == "111111"

        def test_second_immediately_after(ntm_inbox):
            # No sleep. The watermark cannot separate these, and it does not.
            assert ntm_inbox.wait_for_code(poll_interval=0.02, timeout=3) == "111111"

        def test_disambiguating_on_content_is_the_documented_fix(ntm_inbox):
            import pytest
            from nowtempmail import WaitTimeoutError
            with pytest.raises(WaitTimeoutError):
                ntm_inbox.wait_for_code(
                    subject="a subject no message has", poll_interval=0.02, timeout=1
                )
        """
    )
    run(pytester, "-q").assert_outcomes(passed=3)


def test_the_watermark_moves_between_tests(pytester: pytest.Pytester, live: MockApi) -> None:
    pytester.makepyfile(
        """
        import time
        marks = []

        def test_a(ntm_inbox):
            marks.append(ntm_inbox.watermark)
            # At the END of the test: the watermark is stamped when a test
            # BEGINS, so a sleep inside test_b would run after its boundary was
            # already fixed and would prove nothing.
            time.sleep(1.2)

        def test_b(ntm_inbox):
            marks.append(ntm_inbox.watermark)
            assert marks[1] > marks[0], "the watermark did not advance"
        """
    )
    run(pytester, "-q").assert_outcomes(passed=2)


def test_an_explicit_after_still_overrides_the_fixture(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    pytester.makepyfile(
        """
        from datetime import datetime, timezone, timedelta

        def test_override(ntm_inbox):
            chosen = datetime.now(timezone.utc) - timedelta(days=1)
            # Reaches the mailbox unchanged rather than being replaced.
            from nowtempmail.pytest_plugin import apply_watermark
            from nowtempmail import MessageFilter
            spec = apply_watermark(MessageFilter(after=chosen), ntm_inbox.watermark)
            assert spec.after == chosen
        """
    )
    run(pytester, "-q").assert_outcomes(passed=1)


# --------------------------------------------------------------------------- #
# Reporting                                                                    #
# --------------------------------------------------------------------------- #


def test_a_failing_test_gets_the_matched_message_in_its_output(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    # The path a human actually reads. "The wrong mail matched" is the only
    # question a report is really asked, and this is where it gets answered.
    pytester.makepyfile(
        f"""
        import json, urllib.request

        BASE = {live.url!r}

        def deliver(address, codes):
            body = json.dumps({{"address": address, "codes": codes}}).encode()
            req = urllib.request.Request(
                BASE + "/__deliver", data=body, method="POST",
                headers={{"Content-Type": "application/json"}},
            )
            urllib.request.urlopen(req).read()

        def test_fails_after_matching(ntm_inbox):
            deliver(ntm_inbox.address, ["314159"])
            code = ntm_inbox.wait_for_code(poll_interval=0.02, timeout=5)
            assert code == "not-this", "deliberate failure"
        """
    )
    result = run(pytester, "-q")
    result.assert_outcomes(failed=1)
    output = result.stdout.str()
    assert "NowTempMail message" in output
    assert "314159" in output  # the code -- single-use, already spent
    assert "not shown" in output  # links withheld by default


def test_the_autouse_stamp_does_not_warn_under_the_default_junit_family(
    pytester: pytest.Pytester, live: MockApi
) -> None:
    # Requesting the record_property FIXTURE warns at setup under xunit2, which
    # for an autouse fixture would mean a warning on every test in every suite
    # in the environment. The plugin appends to node.user_properties by hand for
    # exactly this reason.
    pytester.makepyfile(
        """
        def test_plain(): assert True
        def test_also_plain(): assert True
        """
    )
    xml = pytester.path / "report.xml"
    result = run(pytester, "-q", f"--junitxml={xml}")
    result.assert_outcomes(passed=2)
    assert "record_property is incompatible" not in result.stdout.str()


# --------------------------------------------------------------------------- #
# Configuration                                                                #
# --------------------------------------------------------------------------- #


def test_ini_options_reach_the_client(pytester: pytest.Pytester, live: MockApi) -> None:
    pytester.makeini(
        f"""
        [pytest]
        ntm_base_url = {live.url}
        ntm_attach_body = true
        ntm_watermark_skew_ms = 3000
        ntm_mailbox_ttl_seconds = 900
        """
    )
    pytester.makepyfile(
        """
        def test_options(ntm_options, ntm_inbox):
            assert ntm_options["attach_body"] is True
            assert ntm_options["watermark_skew_ms"] == 3000
            assert ntm_options["mailbox"]["ttl_seconds"] == 900
            assert ntm_inbox.mailbox.ttl_seconds == 900
        """
    )
    run(pytester, "-q").assert_outcomes(passed=1)


def test_the_user_agent_identifies_the_plugin(pytester: pytest.Pytester, live: MockApi) -> None:
    # Channel attribution: without a distinct suffix there is no way to tell a
    # key that arrived through the fixtures from one that did not.
    pytester.makepyfile(
        """
        def test_anything(ntm_inbox): assert ntm_inbox.address
        """
    )
    run(pytester, "-q").assert_outcomes(passed=1)
    agents = [r.user_agent or "" for r in live.requests]
    assert any("pytest-nowtempmail" in ua for ua in agents)
    assert any("nowtempmail-python/" in ua for ua in agents)


# --------------------------------------------------------------------------- #
# xdist                                                                        #
# --------------------------------------------------------------------------- #


def test_xdist_gets_one_mailbox_per_worker(pytester: pytest.Pytester, live: MockApi) -> None:
    """Session scope under xdist means one mailbox per worker PROCESS.

    That is the behaviour we want, and it is a coincidence of how xdist works
    rather than something this plugin arranges -- so it gets a test rather than
    an assumption. Two workers must produce two mailboxes: not one (workers
    cannot see each other's watermarks, so sharing an address across processes
    would reintroduce exactly the cross-talk the watermark prevents), and not
    four (that would mean session scope had silently degraded to per-test and
    the daily quota was being burned).
    """
    pytest.importorskip("xdist", reason="pytest-xdist is not installed")
    _skip_unless_importable_from_a_clean_subprocess()
    pytester.makepyfile(
        """
        def test_a(ntm_inbox): assert ntm_inbox.address
        def test_b(ntm_inbox): assert ntm_inbox.address
        def test_c(ntm_inbox): assert ntm_inbox.address
        def test_d(ntm_inbox): assert ntm_inbox.address
        """
    )
    run(pytester, "-q", "-n", "2").assert_outcomes(passed=4)
    assert live.state.mailboxes_today == 2
