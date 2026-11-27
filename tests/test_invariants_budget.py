"""Thin deterministic invariants for budget guardrails and SSRF protection (zero mocks)."""

from __future__ import annotations

import pytest

from agentic_emailer.budget import BudgetExceededError, RunBudgetTracker
from agentic_emailer.ingestion.extractor import _is_safe_public_url


def test_budget_tracker_enforces_llm_call_and_token_caps() -> None:
    call_tracker = RunBudgetTracker(max_llm_calls=2, max_total_tokens=10_000)
    call_tracker.consume_llm_call(
        input_tokens=500, output_tokens=200, model="gemini-3.1-flash-lite"
    )
    call_tracker.consume_llm_call(
        input_tokens=500, output_tokens=200, model="gemini-3.1-flash-lite"
    )
    assert not call_tracker.can_call_llm()
    assert call_tracker.estimated_cost_usd() > 0.0

    with pytest.raises(BudgetExceededError):
        call_tracker.consume_llm_call(input_tokens=100, output_tokens=50)

    token_tracker = RunBudgetTracker(max_llm_calls=10, max_total_tokens=1_000)
    token_tracker.consume_llm_call(input_tokens=600, output_tokens=200)
    assert token_tracker.total_tokens == 800

    with pytest.raises(BudgetExceededError):
        token_tracker.consume_llm_call(input_tokens=200, output_tokens=100)

    # Longest-prefix model pricing matches versioned lite models accurately
    lite_tracker = RunBudgetTracker()
    lite_tracker.consume_llm_call(
        input_tokens=100_000, output_tokens=0, model="gemini-2.0-flash-lite-001"
    )
    assert lite_tracker.estimated_cost_usd() == pytest.approx(0.0075)


def test_budget_tracker_enforces_scrape_and_search_caps() -> None:
    tracker = RunBudgetTracker(max_scrapes=2, max_searches=1)

    assert tracker.can_scrape()
    tracker.consume_scrape()
    tracker.consume_scrape()
    assert not tracker.can_scrape()
    with pytest.raises(BudgetExceededError):
        tracker.consume_scrape()

    assert tracker.can_search()
    tracker.consume_search()
    assert not tracker.can_search()
    with pytest.raises(BudgetExceededError):
        tracker.consume_search()


def test_ssrf_guard_blocks_private_and_metadata_ips() -> None:
    blocked_urls = [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.169.254/computeMetadata/v1/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1:8080/admin",
        "http://localhost/internal",
        "http://[::1]/secret",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://[64:ff9b::169.254.169.254]/latest/meta-data/",
        "http://[2002:7f00:0001::1]/loopback-6to4",
        "http://[2002:a9fe:a9fe::1]/metadata-6to4",
        "http://100.64.0.1/cgnat",
        "http://10.0.0.1/status",
        "http://172.16.0.5/metrics",
        "http://192.168.1.1/router",
        "http://0.0.0.0/",
        "http://example.com:99999/invalid-port",
        "http://example.com:abc/non-numeric-port",
        "http://1.1.1.1:0/",
        "http://1.1.1.1/a\r\nb",
        "http://1.1.1.1/path with spaces",
        "http://[2001:4860:4860::8888%25eth0]/",
        "http://[fe80::1%25eth0]/",
        "http://[::1/malformed-ipv6",
        "file:///etc/passwd",
        "ftp://1.1.1.1/file.txt",
        "http://user:pass@1.1.1.1/private",
    ]
    for url in blocked_urls:
        assert not _is_safe_public_url(url), f"SSRF guard failed to block: {url}"

    # Literal globally routable public IP passes without requiring DNS
    assert _is_safe_public_url("https://1.1.1.1/article")
