#!/usr/bin/env python
"""Vendor the OpenAPI document, and detect drift against the live one.

    python scripts/spec.py vendor    # fetch and pretty-print into spec/
    python scripts/spec.py check     # fail if spec/ and the live document differ

``spec/openapi.json`` is **fetched, not hand-written**: it comes from
<https://nowtempmail.com/v1/openapi.json>, the same public unauthenticated
document any caller can GET. Never edit it by hand -- a hand-edited snapshot
makes drift detection meaningless, because it no longer matches the thing it is
being compared against.

Why this fetches rather than copying from ``npm-package/spec/``
--------------------------------------------------------------
It would be one line shorter to copy the file the Node package already vendors.
It would also couple two packages that otherwise share nothing, in a monorepo
where each is archived into its own public repo by prefix -- so the copy would
have to be generated, checked, and kept honest by a third mechanism. Both
packages fetch the same public URL instead. Neither depends on the other, and
the source of truth is the served document rather than whichever package
happened to update first.

What ``check`` compares
-----------------------
The operation set -- every ``METHOD /path`` the document declares -- and the
declared API version. Not the whole document: descriptions and examples change
for editorial reasons and failing CI over a reworded summary trains people to
ignore the check, which is worse than not having one.

Standard library only, like the package itself.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

SPEC_URL = "https://nowtempmail.com/v1/openapi.json"
SPEC_PATH = Path(__file__).resolve().parent.parent / "spec" / "openapi.json"


def fetch() -> dict:
    request = urllib.request.Request(
        SPEC_URL,
        headers={"Accept": "application/json", "User-Agent": "nowtempmail-python-spec-tool"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def operations(document: dict) -> set[str]:
    found: set[str] = set()
    for path, item in (document.get("paths") or {}).items():
        for method in item:
            if method.lower() in ("get", "post", "put", "patch", "delete", "head", "options"):
                found.add(f"{method.upper()} {path}")
    return found


def vendor() -> int:
    document = fetch()
    SPEC_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Pretty-printed so a change reviews cleanly line by line rather than as one
    # unreadable diff.
    SPEC_PATH.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"vendored {len(operations(document))} operations to {SPEC_PATH}")
    return 0


def check() -> int:
    if not SPEC_PATH.exists():
        print(f"no vendored spec at {SPEC_PATH}. Run: python scripts/spec.py vendor")
        return 1

    vendored = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    try:
        live = fetch()
    except Exception as exc:
        # A network failure is not drift. Say which one happened, or someone
        # will spend an afternoon looking for an API change that did not occur.
        print(f"could not reach {SPEC_URL}: {exc}")
        print("This is a fetch failure, not drift. Nothing has been changed.")
        return 1

    ours, theirs = operations(vendored), operations(live)
    added, removed = sorted(theirs - ours), sorted(ours - theirs)
    our_version = (vendored.get("info") or {}).get("version")
    their_version = (live.get("info") or {}).get("version")

    if not added and not removed and our_version == their_version:
        print(f"no drift: {len(ours)} operations, version {our_version}")
        return 0

    print("SPEC DRIFT\n")
    if our_version != their_version:
        print(f"  version   vendored {our_version} -> live {their_version}")
    for operation in added:
        print(f"  + {operation}   (live has it, this package does not)")
    for operation in removed:
        print(f"  - {operation}   (this package expects it, live does not)")
    print(
        "\nRe-vendor with `python scripts/spec.py vendor`, then update "
        "src/nowtempmail/types.py to match. The spec is the source of truth; "
        "types.py is its transcription."
    )
    return 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "vendor":
        return vendor()
    if command == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
