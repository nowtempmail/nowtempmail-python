"""A full message, bodies included.

Fetched only by :meth:`~nowtempmail.mailbox.Mailbox.wait_for_message` and
``messages.get``. :meth:`~nowtempmail.mailbox.Mailbox.wait_for_code` never
builds one, because verification codes are extracted server-side at ingest and
ride on the summary -- which is why waiting for a code is one request per poll
and transfers no message content at all.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .types import Attachment, from_epoch

if TYPE_CHECKING:  # pragma: no cover
    from ._http import Http

__all__ = ["Message"]

#: Deliberately not an HTML parser. This finds URLs in either body, which is
#: what a test asserting on a magic link needs; anything more would be a parser
#: to keep secure against hostile input for no extra capability.
_URL = re.compile(r"https?://[^\s<>\"'\)\]]+")

#: Trailing punctuation that is almost always sentence structure rather than URL.
_TRAILING = ".,;:!?'\""


class Message:
    """One message. Immutable apart from lazily-fetched attachment bytes."""

    def __init__(self, http: Http, address: str, wire: dict[str, Any]) -> None:
        self._http = http
        self._address = address
        self.id: str = wire["id"]
        #: The raw ``From`` header -- often ``Display Name <addr@host>``.
        self.sender: str = wire["from"]
        self.to: str = wire.get("to", address)
        self.subject: str = wire["subject"]
        received = from_epoch(wire["receivedAt"])
        assert received is not None
        self.received_at: datetime = received
        self.text: str | None = wire.get("text")
        self.html: str | None = wire.get("html")
        self.verification_codes: tuple[str, ...] = tuple(wire.get("verificationCodes") or ())
        self.attachments: tuple[Attachment, ...] = tuple(
            Attachment(
                id=a["id"],
                filename=a["filename"],
                content_type=a["contentType"],
                size=a["size"],
            )
            for a in (wire.get("attachments") or ())
        )

    @property
    def code(self) -> str | None:
        """The first extracted verification code, or ``None``."""
        return self.verification_codes[0] if self.verification_codes else None

    def links(self) -> list[str]:
        """Every URL in the body, text first then HTML, de-duplicated in order.

        **These are links a stranger put in an email.** A password-reset or
        magic-link URL among them is a live credential with a working expiry --
        do not log them, and do not put them in a CI artifact. The pytest
        plugin's report redaction exists for exactly this reason.
        """
        seen: dict[str, None] = {}
        for body in (self.text, self.html):
            if not body:
                continue
            for match in _URL.finditer(body):
                url = match.group(0).rstrip(_TRAILING)
                # An href in HTML ends at the quote, which the character class
                # already excludes; a bare URL in prose usually ends at a full
                # stop, which it does not.
                if url:
                    seen.setdefault(url, None)
        return list(seen)

    def download(self, attachment_id: str) -> bytes:
        """The attachment's bytes.

        Streamed nowhere: attachments in a disposable inbox are small by
        construction, and a caller that wanted a file would have to buffer it
        anyway to check it.
        """
        result = self._http.request(
            "GET",
            f"/v1/mailboxes/{self._address}/messages/{self.id}/attachments/{attachment_id}",
            raw=True,
        )
        return result.data  # type: ignore[no-any-return]

    def __repr__(self) -> str:
        return (
            f"Message(id={self.id!r}, subject={self.subject!r}, "
            f"codes={len(self.verification_codes)}, attachments={len(self.attachments)})"
        )
