"""The client without pytest: create an inbox, wait for the code, tidy up.

NTM_API_KEY=ntm_live_... python examples/signup_otp.py
"""

from __future__ import annotations

import sys

from nowtempmail import (
    MailboxMayExistError,
    NowTempMail,
    QuotaExceededError,
    WaitTimeoutError,
)


def main() -> int:
    ntm = NowTempMail()  # reads NTM_API_KEY

    account = ntm.me()  # free: peek mode, does not spend what it reports
    left = account.usage.mailboxes_remaining_today
    print(f"plan {account.plan.label}, {left} mailboxes left today")

    try:
        # Returns once the mailbox can actually RECEIVE, not merely once it
        # exists -- mail arriving before that is dropped, not queued.
        inbox = ntm.mailboxes.create(ttl_seconds=900)
    except QuotaExceededError as err:
        print(f"daily budget spent; resets at {err.retry_after}s", file=sys.stderr)
        return 1
    except MailboxMayExistError:
        # The request went out and the outcome is unknowable. The SDK will not
        # send a second one, because creation is the only call that spends quota
        # and there is no idempotency key.
        print("creation failed in transit -- check me() before retrying", file=sys.stderr)
        return 1

    print(f"address  {inbox.address}")
    print(f"expires  {inbox.expires_at.isoformat()}")
    if not inbox.confirmed:
        print("warning: not readable yet; mail in the next second may be dropped", file=sys.stderr)

    print("\nSend a message with a verification code to that address.")
    try:
        # Never fetches a body: codes are extracted server-side at ingest.
        code = inbox.wait_for_code(timeout=180)
    except WaitTimeoutError as err:
        # messages_seen is the number that matters -- it separates "nothing
        # arrived" from "mail arrived but nothing matched".
        print(f"\ngave up: {err.message}", file=sys.stderr)
        return 4

    print(f"\ncode     {code}")

    deleted = inbox.delete()
    print(f"deleted  {deleted} message(s) -- quota is NOT refunded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
