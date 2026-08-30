"""The watermark: flooring, skew, and precedence.

Without this mechanism a shared inbox is unusable -- test #7 resolves with the
code test #3 triggered and the suite passes while testing nothing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nowtempmail import MessageFilter
from nowtempmail.pytest_plugin import (
    DEFAULT_WATERMARK_SKEW_MS,
    apply_watermark,
    compute_watermark,
)


def test_the_watermark_is_floored_to_the_second() -> None:
    # Comparing a microsecond-precision local timestamp against a second-
    # precision server one discards up to 999ms of mail for no reason.
    mark = compute_watermark(1_786_097_700.987654, skew_ms=0)
    assert mark.microsecond == 0
    assert mark == datetime.fromtimestamp(1_786_097_700, tz=timezone.utc)


def test_the_default_skew_is_one_second_behind() -> None:
    # A CI runner's clock is not the API's clock. A watermark placed at exactly
    # the test's start instant can sit AFTER the timestamp of mail that test
    # caused, which shows up as a timeout with messages_seen > 0 -- the least
    # debuggable outcome available.
    started = 1_786_097_700.4
    assert compute_watermark(started) == datetime.fromtimestamp(1_786_097_699, tz=timezone.utc)
    assert DEFAULT_WATERMARK_SKEW_MS == 1_000


def test_the_skew_is_configurable_and_zero_is_honoured() -> None:
    started = 1_786_097_700.0
    assert compute_watermark(started, skew_ms=0).timestamp() == 1_786_097_700
    assert compute_watermark(started, skew_ms=5_000).timestamp() == 1_786_097_695


def test_a_negative_skew_is_treated_as_zero() -> None:
    started = 1_786_097_700.0
    assert compute_watermark(started, skew_ms=-9_000).timestamp() == 1_786_097_700


def test_the_watermark_never_goes_below_the_epoch() -> None:
    assert compute_watermark(0.0, skew_ms=10_000).timestamp() == 0


def test_the_watermark_is_timezone_aware() -> None:
    assert compute_watermark().tzinfo is not None


# --------------------------------------------------------------------------- #
# Precedence                                                                   #
# --------------------------------------------------------------------------- #

MARK = datetime.fromtimestamp(1_786_097_700, tz=timezone.utc)


def test_an_empty_filter_gets_the_watermark() -> None:
    assert apply_watermark(MessageFilter(), MARK).after == MARK


def test_an_explicit_after_wins() -> None:
    # A caller who names a boundary has out-argued the fixture.
    chosen = MARK - timedelta(hours=1)
    assert apply_watermark(MessageFilter(after=chosen), MARK).after == chosen


def test_ignore_existing_wins_outright() -> None:
    # "Only mail from now on" is strictly narrower than the watermark, so the
    # watermark must not be applied on top of it.
    result = apply_watermark(MessageFilter(ignore_existing=True), MARK)
    assert result.ignore_existing is True
    assert result.after is None


def test_applying_a_watermark_does_not_mutate_the_callers_filter() -> None:
    original = MessageFilter(sender="github", timeout=90)
    result = apply_watermark(original, MARK)
    assert original.after is None
    assert result.after == MARK
    # Everything else survives the copy.
    assert result.sender == "github"
    assert result.timeout == 90
