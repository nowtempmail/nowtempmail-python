# nowtempmail

Disposable mailboxes and OTP codes for your tests. **Verification codes are
extracted server-side, so there is no regex to write.**

```bash
pip install pytest-nowtempmail     # the fixtures (most people want this)
pip install nowtempmail            # the client on its own
```

```python
def test_signup(page, ntm_inbox):
    page.goto("/signup")
    page.fill("#email", ntm_inbox.address)
    page.click("text=Create account")
    page.fill("#code", ntm_inbox.wait_for_code())  # only this test's mail
```

Get a key at <https://nowtempmail.com/developers> and set `NTM_API_KEY`. There is
no config file and no `login` command — a key on a command line ends up in `ps`
output and shell history.

**Zero runtime dependencies.** This installs into test environments that already
hold pytest, Playwright and usually an HTTP-client pin from some other SDK, so
it deliberately brings nothing that could conflict with them.

**Server-side only.** `/v1` rejects any request carrying an `Origin` header. This
is a library for test runners, CI and backends — not for anything in a page.

---

## The fixtures

| Fixture | Scope | What it is |
|---|---|---|
| `ntm_inbox` | session | One mailbox, shared by every test. **Start here.** |
| `ntm_fresh_inbox` | function | A mailbox of this test's own. Costs quota — see below. |
| `ntm_client` | session | The `NowTempMail` client. |
| `ntm_options` | session | Resolved configuration, for overriding in a conftest. |

### Why `ntm_inbox` is shared, and why that is safe

`POST /v1/mailboxes` is the only endpoint that consumes daily quota, the counter
increments on creation, and **it is never decremented on delete**. A per-test
mailbox therefore spends the Free plan's 50 mailboxes/day inside a single CI
run — the suite goes green locally, then starts failing with `quota_exceeded`
partway through the third run of the afternoon, and the failure has nothing to
do with the code that triggered it.

Sharing is safe because **every wait is watermarked**. An autouse fixture
re-stamps a boundary at the start of each test, and `wait_for_code()` /
`wait_for_message()` default their `after` to it — so a code triggered by an
earlier test is not a candidate.

The watermark is floored to the whole second and set one second behind, because
`received_at` on the wire has one-second granularity and a CI runner's clock is
not the API's clock. **The consequence is a real limit:** two tests that trigger
mail within about a second of each other are not isolated from one another. Fix
that by disambiguating on content, which is what you want anyway:

```python
code = ntm_inbox.wait_for_code(sender="github", subject="Sign in")
```

Use `ntm_fresh_inbox` only where sharing genuinely cannot work — a test
asserting on an empty inbox, one that deletes messages out from under a sibling,
or one needing a different domain or TTL. A 40-test file using it burns 40 of
the Free plan's 50/day, and teardown does not give any of them back.

### Configuration

```toml
[tool.pytest.ini_options]
ntm_base_url = "https://nowtempmail.com"
ntm_attach_body = false          # read "What reaches your CI report" first
ntm_watermark_skew_ms = 1000
ntm_mailbox_ttl_seconds = 3600
ntm_mailbox_domain = ""          # empty = the rotation's choice
```

There is deliberately no `ntm_api_key` option. An API key in `pyproject.toml` is
an API key in git.

### What reaches your CI report

When a wait matches, the message is recorded on the test — and on failure it is
printed in the output, which is where "the wrong mail matched" actually gets
diagnosed.

**Bodies and links are withheld by default.** A report is zipped into an
artifact, downloaded by whoever has repo read access, and pasted into issues; a
password-reset link inside one is a live credential with a working expiry. What
you get is sender, subject, timestamp, a link *count*, and the extracted codes.

The codes are included deliberately even though they are secrets: a verification
code is single-use, already spent by the time anyone reads the report, and it is
the one value you cannot debug an OTP flow without.

Set `ntm_attach_body = true` to include bodies and links, and treat the whole
artifact as a secret if you do.

---

## The client on its own

```python
from nowtempmail import NowTempMail

ntm = NowTempMail()  # reads NTM_API_KEY
inbox = ntm.mailboxes.create()  # returns once it can RECEIVE
sign_up(inbox.address)
code = inbox.wait_for_code(sender="acme", timeout=120)
```

`mailboxes.create()` confirms the mailbox is readable before returning. The mail
receiver drops inbound mail if its inbox lookup misses, so "created" and "able
to receive" are not the same instant, and a handle returned before the second
one hands you a race you cannot see.

**Creation is never retried.** It is the only call that spends daily quota and
the API has no idempotency key, so a transport failure raises
`MailboxMayExistError` rather than quietly spending a second mailbox.

### Waiting

```python
inbox.wait_for_code(
    sender="github",  # substring, case-insensitive, or a compiled pattern
    subject=re.compile(r"\d{6}"),
    after=some_datetime,  # default: the mailbox's created_at
    ignore_existing=True,  # for resend flows, where the stale code is the bug
    timeout=120,
    cancel=threading.Event(),
)
```

`after` defaults to the mailbox's `created_at`, **not** to the moment you called.
Mail routinely lands before anyone waits for it — a form submits, mail arrives in
300ms, and the test gets around to waiting a second later. Defaulting to call
time makes that message invisible and produces a flake nobody can reproduce.

`wait_for_code()` never fetches a body: codes ride on the message summary
because they are extracted at ingest. One request per poll, no message content
transferred.

A timeout raises `WaitTimeoutError` carrying **`messages_seen`** — the number
that separates "nothing arrived" from "mail arrived but nothing matched".

### Errors

Branch on `err.code`, not on the class. The API may grow a problem code this
package has never heard of, and an unrecognised one arrives on the base class
carrying that code verbatim rather than being rewritten.

```python
from nowtempmail import NowTempMailError, QuotaExceededError

try:
    inbox = ntm.mailboxes.create()
except QuotaExceededError as err:
    print(err.retry_after)
except NowTempMailError as err:
    print(err.code, err.request_id)  # request_id is what support asks for
```

No error message, `repr()` or log line ever contains the API key.

**Two class names differ from the JavaScript SDK**, because the literal
translations shadow builtins that are `OSError` subclasses:

| JavaScript | Python |
|---|---|
| `TimeoutError` | `WaitTimeoutError` |
| `ConnectionError` | `TransportError` |

---

## Quota, plainly

| | |
|---|---|
| Spends daily quota | `mailboxes.create()` — nothing else |
| Refunded on delete | **No.** Never. |
| Free plan | 50 mailboxes/day, 30 requests/minute |
| Check what is left | `ntm.me().usage.mailboxes_remaining_today` |

Polling `me()` is free: it runs in peek mode server-side and does not consume
the budget it reports on.

---

## Links

- Docs and keys — <https://nowtempmail.com/developers>
- Source — <https://github.com/nowtempmail/nowtempmail-python>
- Issues — <https://nowtempmail.userjot.com>

MIT licensed.
