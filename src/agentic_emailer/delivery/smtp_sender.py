"""Gmail SMTP EmailSender adapter with exponential backoff retries."""

from __future__ import annotations

import logging
import smtplib
import time
from datetime import UTC, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from agentic_emailer.models import DeliveryReceipt, RecipientProfile

logger = logging.getLogger(__name__)


def _clean_header(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


class SmtpEmailSender:
    """EmailSender implementation delivering multipart digests via SMTP + STARTTLS."""

    provider_name = "gmail_smtp"

    def __init__(
        self,
        host: str,
        port: int,
        sender: str,
        password: str,
        *,
        max_retries: int = 3,
        timeout: float = 30.0,
        base_backoff_seconds: float = 2.0,
    ) -> None:
        self.host = host
        self.port = port
        self.sender = _clean_header(sender)
        self.password = password
        self.max_retries = max(1, max_retries)
        self.timeout = timeout
        self.base_backoff_seconds = base_backoff_seconds

    def send(
        self,
        recipient: RecipientProfile,
        subject: str,
        plain_text: str,
        html_body: str,
    ) -> DeliveryReceipt:
        """Send an email to a recipient, retrying transient errors and never raising."""
        if not self.sender or not self.password:
            return DeliveryReceipt(
                recipient_id=recipient.id,
                recipient_email=recipient.email_address,
                status="failed",
                provider=self.provider_name,
                error="Missing SMTP sender or password configuration.",
                sent_at=datetime.now(UTC),
            )

        message = MIMEMultipart("alternative")
        message["Subject"] = _clean_header(subject)
        message["From"] = self.sender
        message["To"] = _clean_header(recipient.email_address)
        message.attach(MIMEText(plain_text, "plain", "utf-8"))
        message.attach(MIMEText(html_body, "html", "utf-8"))

        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.info(
                    "Sending digest email to %s (%s) [attempt %d/%d]",
                    recipient.email_address,
                    recipient.id,
                    attempt,
                    self.max_retries,
                )
                with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as server:
                    server.ehlo()
                    server.starttls()
                    server.ehlo()
                    server.login(self.sender, self.password)
                    server.sendmail(self.sender, [recipient.email_address], message.as_string())

                logger.info("Digest email delivered to %s", recipient.email_address)
                return DeliveryReceipt(
                    recipient_id=recipient.id,
                    recipient_email=recipient.email_address,
                    status="sent",
                    provider=self.provider_name,
                    error=None,
                    sent_at=datetime.now(UTC),
                )
            except smtplib.SMTPAuthenticationError as exc:
                last_error = f"SMTP authentication failed: {exc}"
                logger.error("Non-retryable SMTP auth error for %s: %s", recipient.id, exc)
                break
            except (smtplib.SMTPException, TimeoutError, OSError) as exc:
                last_error = str(exc)
                logger.warning(
                    "Transient SMTP error for %s (attempt %d/%d): %s",
                    recipient.id,
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries:
                    delay = self.base_backoff_seconds * (2 ** (attempt - 1))
                    time.sleep(delay)
            except Exception as exc:
                last_error = str(exc)
                logger.error("Unexpected email delivery error for %s: %s", recipient.id, exc)
                break

        return DeliveryReceipt(
            recipient_id=recipient.id,
            recipient_email=recipient.email_address,
            status="failed",
            provider=self.provider_name,
            error=last_error or "Unknown SMTP delivery error",
            sent_at=datetime.now(UTC),
        )
