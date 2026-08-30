# pytest-nowtempmail

pytest fixtures for disposable inboxes and the OTP codes that land in them.

```bash
pip install pytest-nowtempmail
export NTM_API_KEY="ntm_live_..."     # get one at https://nowtempmail.com/developers
```

```python
def test_signup(page, ntm_inbox):
    page.goto("/signup")
    page.fill("#email", ntm_inbox.address)
    page.click("text=Create account")
    page.fill("#code", ntm_inbox.wait_for_code())  # only this test's mail
```

Verification codes are extracted server-side, so there is no regex to write and
no message body is transferred to find one.

| Fixture | Scope | What it is |
|---|---|---|
| `ntm_inbox` | session | One mailbox shared by every test, with per-test watermarking so waits stay isolated. **Start here.** |
| `ntm_fresh_inbox` | function | A mailbox of this test's own. Spends daily quota that is never refunded. |
| `ntm_client` | session | The underlying `NowTempMail` client. |
| `ntm_options` | session | Resolved configuration, for overriding in a conftest. |

This package is a thin shim: it declares the `pytest11` entry point and depends
on [`nowtempmail`](https://pypi.org/project/nowtempmail/), where the fixtures
actually live. Installing it gets you both.

**Full documentation — the sharing and quota model, watermarking and its
one-second limit, what does and does not reach your CI report, and the client
API — is in the [`nowtempmail`
README](https://github.com/nowtempmail/nowtempmail-python#readme).** It is not
duplicated here, because two copies of the same guidance drift and the wrong one
is always the one you read.

MIT licensed.
