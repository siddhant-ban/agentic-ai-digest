"""Google ADK agents, runtime helper, and deterministic grounding verifier."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS: dict[str, tuple[str, str]] = {
    "ResearchSessionContext": ("agentic_emailer.agents.research_agent", "ResearchSessionContext"),
    "create_critic_agent": ("agentic_emailer.agents.critic_agent", "create_critic_agent"),
    "create_research_agent": ("agentic_emailer.agents.research_agent", "create_research_agent"),
    "create_synthesis_agent": ("agentic_emailer.agents.synthesis_agent", "create_synthesis_agent"),
    "parse_structured_output": ("agentic_emailer.agents.runtime", "parse_structured_output"),
    "run_adk_agent": ("agentic_emailer.agents.runtime", "run_adk_agent"),
    "run_critic": ("agentic_emailer.agents.critic_agent", "run_critic"),
    "run_research_stage": ("agentic_emailer.agents.research_agent", "run_research_stage"),
    "run_structured_agent": ("agentic_emailer.agents.runtime", "run_structured_agent"),
    "run_synthesis": ("agentic_emailer.agents.synthesis_agent", "run_synthesis"),
    "sanitize_draft_grounding": ("agentic_emailer.agents.verifier", "sanitize_draft_grounding"),
    "verify_digest_draft": ("agentic_emailer.agents.verifier", "verify_digest_draft"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        return getattr(import_module(module_name), attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
