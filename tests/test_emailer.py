"""Tests for emailer.py module."""

from unittest.mock import MagicMock, patch

import pytest
from emailer import _build_subject, send_digest


def test_build_subject():
    email_config = {"subject_prefix": "[Custom Digest]"}
    subject = _build_subject(email_config, article_count=5, topic_count=2)

    assert subject.startswith("[Custom Digest]")
    assert "5 updates across 2 topics" in subject


@patch("emailer.smtplib.SMTP")
def test_send_digest(mock_smtp_cls, sample_config):
    mock_server = MagicMock()
    mock_smtp_cls.return_value.__enter__.return_value = mock_server

    digest_md = "# Digest\n\nSome text."
    digest_html = "<h1>Digest</h1><p>Some text.</p>"

    send_digest(
        email_config=sample_config["email"],
        digest_md=digest_md,
        digest_html=digest_html,
        smtp_password="secret-password",
        article_count=3,
        topic_count=2,
    )

    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=30)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("sender@example.com", "secret-password")

    assert mock_server.sendmail.called
    args = mock_server.sendmail.call_args[0]
    assert args[0] == "sender@example.com"
    assert args[1] == ["recipient@example.com"]

    import email
    from email.header import decode_header

    msg = email.message_from_string(args[2])
    raw_subject = msg["Subject"]
    decoded_parts = decode_header(raw_subject)
    subject_str = "".join(
        part.decode(enc or "utf-8") if isinstance(part, bytes) else part
        for part, enc in decoded_parts
    )
    assert "[Test Digest]" in subject_str
    assert "3 updates across 2 topics" in subject_str
    assert msg["From"] == "sender@example.com"
    assert msg["To"] == "recipient@example.com"


