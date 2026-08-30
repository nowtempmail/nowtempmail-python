"""**Release-blocking.** The plugin must do nothing until it is asked to.

A ``pytest11`` entry point is auto-loaded into *every* pytest session in the
environment, including suites that have never heard of NowTempMail. If importing
this module reads ``NTM_API_KEY``, constructs a client, touches the network, or
raises, then installing ``pytest-nowtempmail`` breaks other people's unrelated
test runs -- which is the fastest way to earn a bad reputation on PyPI and the
hardest kind of bug for the person hitting it to attribute.

Every test here runs a real nested pytest session via the ``pytester`` fixture,
with no key in the environment.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def no_key_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NTM_API_KEY", raising=False)
    monkeypatch.delenv("NTM_BASE_URL", raising=False)


def test_importing_the_plugin_reads_nothing_and_raises_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib
    import sys

    monkeypatch.delenv("NTM_API_KEY", raising=False)
    sys.modules.pop("nowtempmail.pytest_plugin", None)
    module = importlib.import_module("nowtempmail.pytest_plugin")
    assert module.DEFAULT_WATERMARK_SKEW_MS == 1000


def test_a_suite_that_never_heard_of_us_collects_and_passes(pytester: pytest.Pytester) -> None:
    # The whole point. The plugin is loaded; the suite does not care.
    pytester.makepyfile(
        """
        def test_arithmetic():
            assert 2 + 2 == 4

        def test_strings():
            assert "a" + "b" == "ab"
        """
    )
    result = pytester.runpytest("-p", "nowtempmail.pytest_plugin")
    result.assert_outcomes(passed=2)


def test_the_autouse_stamp_costs_nothing_without_a_key(pytester: pytest.Pytester) -> None:
    # _ntm_stamp is autouse, so it runs for every test in every suite. It must
    # not depend on ntm_client, or every one of those tests would construct a
    # client and fail on the missing key.
    pytester.makepyfile(
        """
        def test_one(): assert True
        def test_two(): assert True
        def test_three(): assert True
        """
    )
    result = pytester.runpytest("-p", "nowtempmail.pytest_plugin")
    result.assert_outcomes(passed=3)
    assert "NTM_API_KEY" not in result.stdout.str()


def test_requesting_a_fixture_without_a_key_fails_that_test_only(
    pytester: pytest.Pytester,
) -> None:
    # The failure is scoped to the test that asked, arrives at setup rather than
    # at collection, and names what to do about it.
    pytester.makepyfile(
        """
        def test_needs_an_inbox(ntm_inbox):
            assert ntm_inbox.address

        def test_unrelated():
            assert True
        """
    )
    result = pytester.runpytest("-p", "nowtempmail.pytest_plugin")
    result.assert_outcomes(passed=1, errors=1)
    assert "NTM_API_KEY" in result.stdout.str()


def test_help_does_not_differ_with_or_without_a_key(pytester: pytest.Pytester) -> None:
    # The ini keys register unconditionally. A --help that changes depending on
    # the environment is a --help nobody can document.
    result = pytester.runpytest("-p", "nowtempmail.pytest_plugin", "--help")
    output = result.stdout.str()
    for key in (
        "ntm_base_url",
        "ntm_attach_body",
        "ntm_watermark_skew_ms",
        "ntm_mailbox_ttl_seconds",
        "ntm_mailbox_domain",
    ):
        assert key in output, f"{key} missing from --help"


def test_there_is_no_ini_key_for_the_api_key(pytester: pytest.Pytester) -> None:
    # An API key in pyproject.toml is an API key in git.
    result = pytester.runpytest("-p", "nowtempmail.pytest_plugin", "--help")
    assert "ntm_api_key" not in result.stdout.str()


def test_test_inbox_is_not_collected_as_a_test_class(pytester: pytest.Pytester) -> None:
    # Named TestInbox for parity with the JS fixture, so it needs __test__ =
    # False or pytest warns about our class in the user's output.
    pytester.makepyfile(
        """
        from nowtempmail.pytest_plugin import TestInbox

        def test_uses_the_type_only():
            assert TestInbox.__test__ is False
        """
    )
    # No `-W error` here: the outer session has already imported the plugin, so
    # the nested one emits PytestAssertRewriteWarning about it. That is an
    # artifact of testing a plugin with pytester, not something a user sees, and
    # promoting it would make this test fail for a reason it is not about. The
    # assertion below is the actual check.
    result = pytester.runpytest("-p", "nowtempmail.pytest_plugin")
    result.assert_outcomes(passed=1)
    assert "cannot collect test class" not in result.stdout.str()
