"""Email delivery protocol, HTML/plain-text renderer, and Gmail SMTP adapter."""

from agentic_emailer.delivery.protocol import EmailSender
from agentic_emailer.delivery.renderer import (
    build_fallback_draft,
    render_html,
    render_plain_text,
    render_subject,
)
from agentic_emailer.delivery.smtp_sender import SmtpEmailSender

__all__ = [
    "EmailSender",
    "SmtpEmailSender",
    "build_fallback_draft",
    "render_html",
    "render_plain_text",
    "render_subject",
]
