"""SMTP email delivery for digests."""

from __future__ import annotations

import logging
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

logger = logging.getLogger(__name__)


def _build_subject(
    email_config: dict[str, Any],
    article_count: int,
    topic_count: int,
) -> str:
    prefix = email_config.get("subject_prefix", "[AI Digest]")
    date_str = datetime.now().strftime("%Y-%m-%d")
    return f"{prefix} {date_str} — {article_count} updates across {topic_count} topics"


def send_digest(
    email_config: dict[str, Any],
    digest_md: str,
    digest_html: str,
    smtp_password: str,
    article_count: int,
    topic_count: int,
) -> None:
    sender = email_config["sender"]
    recipient = email_config["recipient"]
    smtp_host = email_config.get("smtp_host", "smtp.gmail.com")
    smtp_port = int(email_config.get("smtp_port", 587))

    message = MIMEMultipart("alternative")
    message["Subject"] = _build_subject(email_config, article_count, topic_count)
    message["From"] = sender
    message["To"] = recipient

    message.attach(MIMEText(digest_md, "plain", "utf-8"))
    message.attach(MIMEText(digest_html, "html", "utf-8"))

    logger.info("Sending digest email to %s", recipient)
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
        server.starttls()
        server.login(sender, smtp_password)
        server.sendmail(sender, [recipient], message.as_string())
    logger.info("Email sent successfully")
