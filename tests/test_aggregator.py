"""Tests for aggregator.py module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aggregator import (
    aggregator,
    discover_recipients,
    format_articles_for_console,
    is_valid_email,
    load_config,
    load_gemini_api_key,
    load_secret_file,
    load_smtp_password,
    main,
    parse_args,
)



def test_load_config_valid(tmp_path: Path, sample_config):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    loaded = load_config(config_file)
    assert loaded["topics"] == sample_config["topics"]
    assert loaded["email"]["sender"] == sample_config["email"]["sender"]


def test_load_config_missing_file(tmp_path: Path):
    missing_file = tmp_path / "nonexistent.json"
    with pytest.raises(FileNotFoundError, match="Config not found"):
        load_config(missing_file)


def test_discover_recipients_with_file(tmp_path: Path, sample_config):
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(sample_config), encoding="utf-8")

    rec_file = tmp_path / "recipients.json"
    rec_file.write_text(
        json.dumps([
            {
                "id": "user1",
                "email_address": "user1@example.com",
                "topics": ["AI Research"],
                "enabled": True,
            },
            {
                "id": "user2",
                "email_address": "user2@example.com",
                "topics": ["Robotics"],
                "enabled": False,
            },
        ]),
        encoding="utf-8",
    )

    profiles = discover_recipients(config_file, sample_config)
    assert len(profiles) == 1
    assert profiles[0]["id"] == "user1"
    assert profiles[0]["email_address"] == "user1@example.com"
    assert profiles[0]["topics"] == ["AI Research"]


def test_discover_recipients_with_env_var(monkeypatch, tmp_path: Path, sample_config):
    config_file = tmp_path / "config.json"
    env_data = json.dumps([
        {"id": "env_user", "email_address": "envuser@example.com", "topics": ["LLMs"]},
        {"id": "disabled_user", "email_address": "disabled@example.com", "enabled": False},
    ])
    monkeypatch.setenv("RECIPIENTS_JSON", env_data)

    profiles = discover_recipients(config_file, sample_config)
    assert len(profiles) == 1
    assert profiles[0]["id"] == "env_user"
    assert profiles[0]["email_address"] == "envuser@example.com"


def test_discover_recipients_filter(tmp_path: Path, sample_config):
    config_file = tmp_path / "config.json"
    rec_file = tmp_path / "recipients.json"
    rec_file.write_text(
        json.dumps([
            {"id": "alice", "email_address": "alice@example.com", "topics": ["AI"]},
            {"id": "bob", "email_address": "bob@example.com", "topics": ["Robotics"]},
        ]),
        encoding="utf-8",
    )

    profiles_all = discover_recipients(config_file, sample_config)
    assert len(profiles_all) == 2

    profiles_alice = discover_recipients(config_file, sample_config, recipient_filter="alice@example.com")
    assert len(profiles_alice) == 1
    assert profiles_alice[0]["id"] == "alice"

    profiles_bob_id = discover_recipients(config_file, sample_config, recipient_filter="bob")
    assert len(profiles_bob_id) == 1
    assert profiles_bob_id[0]["id"] == "bob"


def test_discover_recipients_fallback(tmp_path: Path, sample_config):
    config_file = tmp_path / "config.json"
    profiles = discover_recipients(config_file, sample_config)
    assert len(profiles) == 1
    assert profiles[0]["email_address"] == sample_config["email"]["recipient"]



def test_load_secret_file(tmp_path: Path):
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("  my-secret-token  \n", encoding="utf-8")

    assert load_secret_file(secret_file) == "my-secret-token"
    assert load_secret_file(tmp_path / "missing.txt") is None

    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("   \n", encoding="utf-8")
    assert load_secret_file(empty_file) is None


def test_load_gemini_api_key(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini-key")
    assert load_gemini_api_key() == "env-gemini-key"

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake_path = tmp_path / "gemini_api_key.txt"
    fake_path.write_text("file-gemini-key", encoding="utf-8")

    with patch("aggregator.GEMINI_KEY_PATH", fake_path):
        assert load_gemini_api_key() == "file-gemini-key"

    fake_path.unlink()
    with patch("aggregator.GEMINI_KEY_PATH", fake_path):
        with pytest.raises(FileNotFoundError, match="Gemini API key not found"):
            load_gemini_api_key()


def test_load_smtp_password(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("EMAIL_PASSWORD", "env-smtp-password")
    assert load_smtp_password() == "env-smtp-password"

    monkeypatch.delenv("EMAIL_PASSWORD", raising=False)
    fake_path = tmp_path / "smtp_password.txt"
    fake_path.write_text("file-smtp-password", encoding="utf-8")

    with patch("aggregator.SMTP_PASSWORD_PATH", fake_path):
        assert load_smtp_password() == "file-smtp-password"

    fake_path.unlink()
    with patch("aggregator.SMTP_PASSWORD_PATH", fake_path):
        with pytest.raises(FileNotFoundError, match="SMTP password not found"):
            load_smtp_password()


def test_format_articles_for_console(sample_articles):
    formatted = format_articles_for_console(sample_articles)
    assert "Found 2 article(s):" in formatted
    assert "1. New Gemini Model Released" in formatted
    assert "2. Open Source LLM Benchmark Breakthrough" in formatted


@patch("aggregator.gather_all")
def test_aggregator_gather_only(mock_gather, tmp_path: Path, sample_config, sample_articles):
    mock_gather.return_value = sample_articles
    config_file = tmp_path / "config.json"
    result = aggregator(sample_config, config_path=config_file, gather_only=True)
    assert "=== Recipient: recipient@example.com (default) ===" in result
    assert "Found 2 article(s):" in result


@patch("aggregator.send_digest")
@patch("aggregator.load_smtp_password", return_value="smtp-pass")
@patch("aggregator.build_digest")
@patch("aggregator.load_gemini_api_key", return_value="gemini-key")
@patch("aggregator.gather_all")
def test_aggregator_dry_run(
    mock_gather, mock_key, mock_build, mock_smtp_pass, mock_send, tmp_path: Path, sample_config, sample_articles
):
    mock_gather.return_value = sample_articles
    mock_build.return_value = ("# MD Digest", "<h1>HTML Digest</h1>")
    config_file = tmp_path / "config.json"

    result = aggregator(sample_config, config_path=config_file, dry_run=True)
    assert "# MD Digest" in result
    assert not mock_send.called


@patch("aggregator.send_digest")
@patch("aggregator.load_smtp_password", return_value="smtp-pass")
@patch("aggregator.build_digest")
@patch("aggregator.load_gemini_api_key", return_value="gemini-key")
@patch("aggregator.gather_all")
def test_aggregator_full_run(
    mock_gather, mock_key, mock_build, mock_smtp_pass, mock_send, tmp_path: Path, sample_config, sample_articles
):
    mock_gather.return_value = sample_articles
    mock_build.return_value = ("# MD Digest", "<h1>HTML Digest</h1>")
    config_file = tmp_path / "config.json"

    result = aggregator(sample_config, config_path=config_file)
    assert result == "# MD Digest"
    mock_send.assert_called_once_with(
        sample_config["email"],
        "# MD Digest",
        "<h1>HTML Digest</h1>",
        "smtp-pass",
        article_count=2,
        topic_count=2,
    )


@patch("aggregator.send_digest")
@patch("aggregator.load_smtp_password", return_value="smtp-pass")
@patch("aggregator.build_digest")
@patch("aggregator.load_gemini_api_key", return_value="gemini-key")
@patch("aggregator.gather_all")
def test_aggregator_multi_recipient_run(
    mock_gather, mock_key, mock_build, mock_smtp_pass, mock_send, tmp_path: Path, sample_config, sample_articles
):
    config_file = tmp_path / "config.json"
    rec_file = tmp_path / "recipients.json"
    rec_file.write_text(
        json.dumps([
            {"id": "user_a", "email_address": "usera@example.com", "topics": ["Artificial Intelligence"]},
            {"id": "user_b", "email_address": "userb@example.com", "topics": ["LLMs"]},
        ]),
        encoding="utf-8",
    )

    mock_gather.return_value = sample_articles
    mock_build.return_value = ("# MD Digest", "<h1>HTML Digest</h1>")

    aggregator(sample_config, config_path=config_file)

    assert mock_send.call_count == 2
    recipients_sent = [call[0][0]["recipient"] for call in mock_send.call_args_list]
    assert "usera@example.com" in recipients_sent
    assert "userb@example.com" in recipients_sent



def test_parse_args():
    args = parse_args(["--dry-run", "--config", "custom.json", "--recipient", "alice@example.com"])
    assert args.dry_run is True
    assert args.gather_only is False
    assert args.config == Path("custom.json")
    assert args.recipient == "alice@example.com"


@patch("aggregator.aggregator")
@patch("aggregator.load_config")
def test_main_success(mock_load_config, mock_aggregator, sample_config):
    mock_load_config.return_value = sample_config
    assert main(["--gather-only", "--recipient", "default"]) == 0
    mock_aggregator.assert_called_once()


@patch("aggregator.load_config", side_effect=Exception("Failed to read file"))
def test_main_error(mock_load_config):
    assert main([]) == 1


def test_is_valid_email():
    assert is_valid_email("user@example.com") is True
    assert is_valid_email("siddhantban+ai@gmail.com") is True
    assert is_valid_email("not_an_email") is False
    assert is_valid_email("@domain.com") is False
    assert is_valid_email("") is False
    assert is_valid_email(None) is False


def test_discover_recipients_invalid_email(tmp_path: Path, sample_config):
    config_file = tmp_path / "config.json"
    rec_file = tmp_path / "recipients.json"
    rec_file.write_text(
        json.dumps([
            {"id": "bad_email", "email_address": "not_an_email", "topics": ["AI"]},
            {"id": "good_email", "email_address": "good@example.com", "topics": ["AI"]},
        ]),
        encoding="utf-8",
    )

    profiles = discover_recipients(config_file, sample_config)
    assert len(profiles) == 1
    assert profiles[0]["id"] == "good_email"
    assert profiles[0]["email_address"] == "good@example.com"


@patch("aggregator.send_digest")
@patch("aggregator.load_smtp_password", return_value="smtp-pass")
@patch("aggregator.build_digest")
@patch("aggregator.load_gemini_api_key", return_value="gemini-key")
@patch("aggregator.gather_all")
def test_aggregator_send_digest_isolated_errors(
    mock_gather, mock_key, mock_build, mock_smtp_pass, mock_send, tmp_path: Path, sample_config, sample_articles
):
    config_file = tmp_path / "config.json"
    rec_file = tmp_path / "recipients.json"
    rec_file.write_text(
        json.dumps([
            {"id": "user_fail", "email_address": "fail@example.com", "topics": ["AI"]},
            {"id": "user_ok", "email_address": "ok@example.com", "topics": ["AI"]},
        ]),
        encoding="utf-8",
    )

    mock_gather.return_value = sample_articles
    mock_build.return_value = ("# MD Digest", "<h1>HTML Digest</h1>")
    mock_send.side_effect = [Exception("SMTPRecipientsRefused: 550"), None]

    aggregator(sample_config, config_path=config_file)

    assert mock_send.call_count == 2


