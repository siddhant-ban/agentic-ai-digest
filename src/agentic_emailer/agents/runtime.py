"""Google ADK Runner execution helper, token accounting, and structured output parsing."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from agentic_emailer.budget import RunBudgetTracker

logger = logging.getLogger(__name__)

CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)
MAX_RESPONSE_SUMMARY_CHARS = 300
TRANSIENT_HTTP_CODES = frozenset({429, 500, 502, 503, 504})
MAX_TRANSIENT_RETRIES = 2


def _is_transient_genai_error(exc: BaseException) -> bool:
    if isinstance(exc, genai_errors.ServerError):
        return True
    if isinstance(exc, genai_errors.APIError):
        code = getattr(exc, "code", None)
        return code in TRANSIENT_HTTP_CODES
    return False


def _extract_event_text(event: Event) -> str:
    """Extract non-thought text parts from an ADK event."""
    if not event.content or not event.content.parts:
        return ""
    chunks: list[str] = []
    for part in event.content.parts:
        if getattr(part, "thought", False):
            continue
        text = getattr(part, "text", None)
        if text:
            chunks.append(text)
    return "".join(chunks).strip()


def _extract_token_counts(event: Event) -> tuple[int, int] | None:
    """Extract (input_tokens, output_tokens) from an ADK Event usage_metadata."""
    usage = getattr(event, "usage_metadata", None)
    if usage is None:
        return None
    prompt_tokens = int(getattr(usage, "prompt_token_count", None) or 0)
    candidates_tokens = int(getattr(usage, "candidates_token_count", None) or 0)
    thoughts_tokens = int(getattr(usage, "thoughts_token_count", None) or 0)
    output_tokens = candidates_tokens + thoughts_tokens
    if output_tokens == 0:
        total_tokens = int(getattr(usage, "total_token_count", None) or 0)
        if total_tokens > prompt_tokens:
            output_tokens = total_tokens - prompt_tokens
    return prompt_tokens, output_tokens


def _summarize_tool_response(response: Any) -> str:
    if response is None:
        return ""
    try:
        serialized = json.dumps(response, ensure_ascii=False, default=str)
    except TypeError:
        serialized = str(response)
    if len(serialized) > MAX_RESPONSE_SUMMARY_CHARS:
        return serialized[: MAX_RESPONSE_SUMMARY_CHARS - 3] + "..."
    return serialized


async def _run_adk_agent_once(
    agent: LlmAgent,
    prompt: str,
    budget: RunBudgetTracker,
    *,
    app_name: str = "agentic_emailer",
    user_id: str = "system",
) -> tuple[str, list[dict[str, Any]]]:
    budget.assert_can_call_llm()

    session_service = InMemorySessionService()
    session_id = f"{agent.name}-{uuid.uuid4().hex[:12]}"
    await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    runner = Runner(
        agent=agent,
        app_name=app_name,
        session_service=session_service,
    )

    user_message = types.Content(role="user", parts=[types.Part(text=prompt)])
    model_name = agent.model if isinstance(agent.model, str) else None

    trajectory: list[dict[str, Any]] = []
    pending_by_call_id: dict[str, dict[str, Any]] = {}
    final_text = ""
    last_non_empty_text = ""
    recorded_llm_turn = False

    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_message,
    ):
        if event.error_code or event.error_message:
            logger.warning(
                "ADK agent %s emitted error event (%s): %s",
                agent.name,
                event.error_code,
                event.error_message,
            )

        if not event.partial:
            token_counts = _extract_token_counts(event)
            if token_counts is not None:
                in_tok, out_tok = token_counts
                budget.consume_llm_call(
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    model=event.model_version or model_name,
                )
                recorded_llm_turn = True

        for func_call in event.get_function_calls():
            entry: dict[str, Any] = {
                "name": func_call.name or "",
                "args": dict(func_call.args) if func_call.args else {},
                "response_summary": "",
            }
            trajectory.append(entry)
            call_id = getattr(func_call, "id", None)
            if call_id:
                pending_by_call_id[str(call_id)] = entry

        for func_resp in event.get_function_responses():
            summary = _summarize_tool_response(func_resp.response)
            resp_id = getattr(func_resp, "id", None)
            if resp_id and str(resp_id) in pending_by_call_id:
                pending_by_call_id[str(resp_id)]["response_summary"] = summary
            else:
                for entry in reversed(trajectory):
                    if entry["name"] == (func_resp.name or "") and not entry["response_summary"]:
                        entry["response_summary"] = summary
                        break

        text = _extract_event_text(event)
        if text:
            last_non_empty_text = text
            if event.is_final_response():
                final_text = text

    if not recorded_llm_turn:
        budget.consume_llm_call(input_tokens=0, output_tokens=0, model=model_name)

    return (final_text or last_non_empty_text), trajectory


async def run_adk_agent(
    agent: LlmAgent,
    prompt: str,
    budget: RunBudgetTracker,
    *,
    app_name: str = "agentic_emailer",
    user_id: str = "system",
) -> tuple[str, list[dict[str, Any]]]:
    """Execute a Google ADK LlmAgent through Runner + InMemorySessionService.

    Returns (final_response_text, tool_call_trajectory) while accounting all LLM calls
    and token usage against `budget`, retrying transient 429/5xx API errors.
    """
    for attempt in range(MAX_TRANSIENT_RETRIES + 1):
        try:
            return await _run_adk_agent_once(
                agent,
                prompt,
                budget,
                app_name=app_name,
                user_id=user_id,
            )
        except Exception as exc:
            if not _is_transient_genai_error(exc) or attempt >= MAX_TRANSIENT_RETRIES:
                raise
            delay = 2.0 * (2**attempt)
            logger.warning(
                "Transient GenAI API error in %s (attempt %d/%d): %s; retrying in %.1fs",
                agent.name,
                attempt + 1,
                MAX_TRANSIENT_RETRIES + 1,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(f"Unreachable retry exhaustion for agent {agent.name}")


def parse_structured_output[T: BaseModel](raw_text: str, schema: type[T]) -> T:
    """Parse and validate JSON text (stripping Markdown fences) into a Pydantic model."""
    cleaned = raw_text.strip()
    if not cleaned:
        raise ValueError(f"Empty model response; expected valid {schema.__name__} JSON")

    fence_match = CODE_FENCE_RE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return schema.model_validate_json(cleaned)
    except ValidationError as first_exc:
        # Attempt fallback extraction if surrounding prose wraps a single JSON object
        start_idx = cleaned.find("{")
        end_idx = cleaned.rfind("}")
        if start_idx != -1 and end_idx > start_idx:
            candidate = cleaned[start_idx : end_idx + 1]
            try:
                return schema.model_validate_json(candidate)
            except ValidationError:
                pass
        raise ValueError(
            f"Failed to parse {schema.__name__} from model output: {first_exc}"
        ) from first_exc


async def run_structured_agent[T: BaseModel](
    agent: LlmAgent,
    prompt: str,
    schema: type[T],
    budget: RunBudgetTracker,
    *,
    app_name: str = "agentic_emailer",
    user_id: str = "system",
    allow_repair: bool = True,
) -> tuple[T, list[dict[str, Any]]]:
    """Run an ADK LlmAgent and validate structured output, with up to one repair pass."""
    raw_text, trajectory = await run_adk_agent(
        agent,
        prompt,
        budget,
        app_name=app_name,
        user_id=user_id,
    )
    try:
        return parse_structured_output(raw_text, schema), trajectory
    except ValueError as exc:
        if not allow_repair or not budget.can_call_llm():
            raise
        logger.warning(
            "Agent %s output failed %s validation (%s); attempting one schema repair pass",
            agent.name,
            schema.__name__,
            exc,
        )
        repair_prompt = (
            f"{prompt}\n\n"
            f"IMPORTANT SCHEMA REPAIR: Your previous response could not be validated as "
            f"{schema.__name__}.\n"
            f"Validation error: {exc}\n"
            f"Return ONLY valid JSON conforming strictly to the required schema."
        )
        repaired_text, repair_traj = await run_adk_agent(
            agent,
            repair_prompt,
            budget,
            app_name=app_name,
            user_id=user_id,
        )
        trajectory.extend(repair_traj)
        return parse_structured_output(repaired_text, schema), trajectory
