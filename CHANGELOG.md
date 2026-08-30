# Changelog

All notable changes to this package are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`nowtempmail` and `pytest-nowtempmail` are released together from this
repository and share a version number.

## [Unreleased]

## [0.1.1] - 2026-08-30

First release on PyPI. Identical package code to 0.1.0 — the only changes are
to the release pipeline and this file.

### Fixed

- **The release workflow's smoke check rejected a working package.** It ran
  `pytest --version --trace-config | grep -q nowtempmail` to prove the
  `pytest11` entry point had registered. `--version` makes pytest print the
  version and exit *before* emitting any plugin trace, so the grep matched
  nothing and the step failed after every real check had passed — the install
  succeeded, the import succeeded, the fixtures were registered. It now asserts
  against installed metadata instead: the entry point resolves, loads, and
  exposes `ntm_inbox`, `ntm_fresh_inbox` and `ntm_client`.
- **`CHANGELOG.md` did not exist**, while `pyproject.toml` advertised a
  Changelog URL. The PyPI project page would have carried a dead link.

## [0.1.0] - 2026-08-30

Published to **TestPyPI only**. Its release run failed the smoke gate described
above, so it was never promoted to PyPI. Nothing is wrong with the 0.1.0
artifacts; they are simply superseded by 0.1.1.

### Added

- **`NowTempMail` client** — `me()`, `domains.list()`,
  `mailboxes.create/get/attach/delete`, `reservations`, `webhooks`. Zero runtime
  dependencies, on `urllib`: this package installs into test environments that
  already hold pytest, Playwright and somebody else's HTTP client, and a hard
  `httpx` requirement would be a resolver conflict in exactly those places.
- **`Mailbox`** with `wait_for_code()` and `wait_for_message()`. Verification
  codes are extracted server-side at ingest, so `wait_for_code()` never fetches
  a message body — one request per poll, and no message content transferred.
- **pytest fixtures**, via the `pytest-nowtempmail` distribution:
  `ntm_inbox` (session-scoped and shared, because mailbox creation is the only
  call that spends daily quota and deleting one never refunds it),
  `ntm_fresh_inbox`, `ntm_client` and `ntm_options`. Every wait is watermarked
  per test, so a shared inbox cannot resolve with an earlier test's code.
- **Report redaction.** A matched message is recorded on the test and printed
  on failure — sender, subject, timestamp and extracted codes, but **not**
  bodies or links. A CI artifact is downloaded by anyone with repository read
  access, and a password-reset link inside one is a live credential.
- Full typing with `py.typed`, and an error taxonomy that branches on
  `err.code` so an unrecognised problem code from a newer API cannot break an
  older client.

### Notes

- Two error class names differ from the JavaScript SDK because the literal
  translations shadow `OSError` builtins: `WaitTimeoutError` (not
  `TimeoutError`) and `TransportError` (not `ConnectionError`).
- `ignore_existing` carries a one-second ambiguity that cannot be removed —
  `receivedAt` on the wire has one-second granularity. It resolves by excluding,
  because including risks silently returning the stale code the flag exists to
  avoid. See `MessageFilter.ignore_existing`.

[Unreleased]: https://github.com/nowtempmail/nowtempmail-python/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/nowtempmail/nowtempmail-python/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nowtempmail/nowtempmail-python/releases/tag/v0.1.0
