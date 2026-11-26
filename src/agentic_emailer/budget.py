"""Hard per-run cost, token, search, and scrape budget guardrails."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


class BudgetExceededError(RuntimeError):
    """Raised when a run exceeds a configured LLM call, token, search, or scrape ceiling."""


# Approximate USD per 1M tokens: (input_usd_per_1m, output_usd_per_1m)
DEFAULT_MODEL_PRICING_USD_PER_1M: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.30, 2.50),
    "gemini-3.5-flash": (0.30, 2.50),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite-preview": (0.10, 0.40),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3-flash": (0.30, 2.50),
    "gemini-3-flash-preview": (0.30, 2.50),
    "gemini-2.0-flash": (0.10, 0.40),
    "gemini-2.0-flash-lite": (0.075, 0.30),
}
FALLBACK_PRICING_USD_PER_1M: tuple[float, float] = (0.15, 0.60)


@dataclass
class RunBudgetTracker:
    """Thread-safe tracker enforcing hard per-run spending and tool-call ceilings."""

    max_llm_calls: int = 60
    max_total_tokens: int = 350_000
    max_scrapes: int = 20
    max_searches: int = 20
    pricing_usd_per_1m: dict[str, tuple[float, float]] = field(
        default_factory=lambda: dict(DEFAULT_MODEL_PRICING_USD_PER_1M)
    )

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    scrape_calls: int = 0
    search_calls: int = 0
    _tokens_by_model: dict[str, list[int]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def total_tokens(self) -> int:
        with self._lock:
            return self.input_tokens + self.output_tokens

    def can_call_llm(self) -> bool:
        with self._lock:
            return (
                self.llm_calls < self.max_llm_calls
                and (self.input_tokens + self.output_tokens) < self.max_total_tokens
            )

    def assert_can_call_llm(self) -> None:
        """Pre-flight check before dispatching an LLM request."""
        with self._lock:
            if self.llm_calls >= self.max_llm_calls:
                raise BudgetExceededError(
                    f"LLM call budget exhausted ({self.llm_calls}/{self.max_llm_calls})"
                )
            total = self.input_tokens + self.output_tokens
            if total >= self.max_total_tokens:
                raise BudgetExceededError(
                    f"Token budget exhausted ({total}/{self.max_total_tokens})"
                )

    def consume_llm_call(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        *,
        model: str | None = None,
    ) -> None:
        """Record one LLM call and its token usage, raising if caps are exceeded."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts must be non-negative")

        with self._lock:
            if self.llm_calls + 1 > self.max_llm_calls:
                raise BudgetExceededError(
                    f"LLM call cap exceeded ({self.llm_calls + 1} > {self.max_llm_calls})"
                )

            self.llm_calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens

            model_key = (model or "default").lower()
            bucket = self._tokens_by_model.setdefault(model_key, [0, 0])
            bucket[0] += input_tokens
            bucket[1] += output_tokens

            total = self.input_tokens + self.output_tokens
            if total > self.max_total_tokens:
                raise BudgetExceededError(
                    f"Token cap exceeded ({total} > {self.max_total_tokens})"
                )

    def can_scrape(self) -> bool:
        with self._lock:
            return self.scrape_calls < self.max_scrapes

    def consume_scrape(self) -> None:
        with self._lock:
            if self.scrape_calls >= self.max_scrapes:
                raise BudgetExceededError(
                    f"Article scrape cap exceeded ({self.scrape_calls}/{self.max_scrapes})"
                )
            self.scrape_calls += 1

    def can_search(self) -> bool:
        with self._lock:
            return self.search_calls < self.max_searches

    def consume_search(self) -> None:
        with self._lock:
            if self.search_calls >= self.max_searches:
                raise BudgetExceededError(
                    f"Search call cap exceeded ({self.search_calls}/{self.max_searches})"
                )
            self.search_calls += 1

    def estimated_cost_usd(self, default_model: str = "gemini-3.1-flash-lite") -> float:
        """Estimate total LLM API spend in USD across all recorded calls."""
        with self._lock:
            if not self._tokens_by_model:
                return 0.0
            total_cost = 0.0
            for model_key, (in_tok, out_tok) in self._tokens_by_model.items():
                lookup = default_model.lower() if model_key == "default" else model_key
                in_rate, out_rate = self._resolve_pricing(lookup)
                total_cost += (in_tok / 1_000_000.0) * in_rate
                total_cost += (out_tok / 1_000_000.0) * out_rate
            return round(total_cost, 6)

    def _resolve_pricing(self, model_name: str) -> tuple[float, float]:
        if model_name in self.pricing_usd_per_1m:
            return self.pricing_usd_per_1m[model_name]
        # Longest-prefix match handles versioned model names (e.g., gemini-2.0-flash-lite-001)
        for prefix in sorted(self.pricing_usd_per_1m, key=len, reverse=True):
            if model_name.startswith(prefix):
                return self.pricing_usd_per_1m[prefix]
        return FALLBACK_PRICING_USD_PER_1M
