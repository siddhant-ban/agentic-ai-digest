"""Delivery protocol definition for pluggable email providers."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentic_emailer.models import DeliveryReceipt, RecipientProfile


@runtime_checkable
class EmailSender(Protocol):
    """Protocol implemented by email delivery providers (e.g., Gmail SMTP)."""

    def send(
        self,
        recipient: RecipientProfile,
        subject: str,
        plain_text: str,
        html_body: str,
    ) -> DeliveryReceipt:
        """Deliver a rendered digest email to a single recipient and return a receipt."""
        ...
