"""A development mail transport: messages are written to a directory.

Local development has no mail provider, and the verification code must never
travel in an API response or a log line. This is the third place it can go: a
file on the developer's own disk, one JSON document per message, readable only
by the account that runs the server. `Settings.validate_auth` refuses to start
a production deployment with it configured.

Enable it with `EMAIL_OUTBOX_DIR=data/outbox` (`python dev.py` does this for
you when no provider is configured), then read the newest file in that folder.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

from app.backend.email.provider import EmailDeliveryError
from app.backend.email.templates import (
    VERIFICATION_CODE_SUBJECT,
    verification_code_email_text,
    verification_email_text,
)


class OutboxEmailProvider:
    """Writes each message to `<outbox>/<timestamp>-<random>.json`."""

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    def _write(self, message: dict) -> None:
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            path = self._directory / f"{stamp}-{secrets.token_hex(4)}.json"
            # 0600: the file holds a live credential.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(message, handle, indent=2)
        except OSError as exc:
            raise EmailDeliveryError("The development outbox could not be written.") from exc

    def send_verification_email(
        self, *, to_address: str, display_name: str, verification_url: str
    ) -> None:
        self._write(
            {
                "to": to_address,
                "subject": "Confirm your ASTRION email address",
                "text": verification_email_text(
                    display_name=display_name, verification_url=verification_url
                ),
            }
        )

    def send_verification_code(
        self, *, to_address: str, display_name: str, code: str, expires_minutes: int
    ) -> None:
        self._write(
            {
                "to": to_address,
                "subject": VERIFICATION_CODE_SUBJECT,
                "code": code,
                "text": verification_code_email_text(
                    display_name=display_name, code=code, expires_minutes=expires_minutes
                ),
            }
        )
