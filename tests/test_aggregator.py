"""Tests for aggregator.py module."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aggregator import (
    aggregator,
    format_articles_for_console,
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


def test_load_config_invalid_schema(tmp_path: Path):
    no_topics = tmp_path / "no_topics.json"
    no_topics.write_text(json.dumps({"email": {"sender": "a"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="must include at least one topic"):
        load_config(no_topics)

    no_email = tmp_path / "no_email.json"
    no_email.write_text(json.dumps({"topics": ["AI"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="must include an 'email' section"):
        load_config(no_email)


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
def test_aggregator_gather_only(mock_gather, sample_config, sample_articles):
    mock_gather.return_value = sample_articles
    result = aggregator(sample_config, gather_only=True)
    assert "Found 2 article(s):" in result


@patch("aggregator.send_digest")
@patch("aggregator.load_smtp_password", return_value="smtp-pass")
@patch("aggregator.build_digest")
@patch("aggregator.load_gemini_api_key", return_value="gemini-key")
@patch("aggregator.gather_all")
def test_aggregator_dry_run(
    mock_gather, mock_key, mock_build, mock_smtp_pass, mock_send, sample_config, sample_articles
):
    mock_gather.return_value = sample_articles
    mock_build.return_value = ("# MD Digest", "<h1>HTML Digest</h1>")

    result = aggregator(sample_config, dry_run=True)
    assert result == "# MD Digest"
    assert not mock_send.called


@patch("aggregator.send_digest")
@patch("aggregator.load_smtp_password", return_value="smtp-pass")
@patch("aggregator.build_digest")
@patch("aggregator.load_gemini_api_key", return_value="gemini-key")
@patch("aggregator.gather_all")
def test_aggregator_full_run(
    mock_gather, mock_key, mock_build, mock_smtp_pass, mock_send, sample_config, sample_articles
):
    mock_gather.return_value = sample_articles
    mock_build.return_value = ("# MD Digest", "<h1>HTML Digest</h1>")

    result = aggregator(sample_config)
    assert result == "# MD Digest"
    mock_send.assert_called_once_with(
        sample_config["email"],
        "# MD Digest",
        "<h1>HTML Digest</h1>",
        "smtp-pass",
        article_count=2,
        topic_count=2,
    )


def test_parse_args():
    args = parse_args(["--dry-run", "--config", "custom.json"])
    assert args.dry_run is True
    assert args.gather_only is False
    assert args.config == Path("custom.json")


@patch("aggregator.aggregator")
@patch("aggregator.load_config")
def test_main_success(mock_load_config, mock_aggregator, sample_config):
    mock_load_config.return_value = sample_config
    assert main(["--gather-only"]) == 0
    mock_aggregator.assert_called_once()


@patch("aggregator.load_config", side_effect=Exception("Failed to read file"))
def test_main_error(mock_load_config):
    assert main([]) == 1
