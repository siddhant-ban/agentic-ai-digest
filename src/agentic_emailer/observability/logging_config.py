"""Structured JSON logging formatted for GCP Cloud Logging with trace correlation."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

from opentelemetry import trace

_RUN_ID_CTX: ContextVar[str | None] = ContextVar("agentic_emailer_run_id", default=None)
_RECIPIENT_ID_CTX: ContextVar[str | None] = ContextVar(
    "agentic_emailer_recipient_id", default=None
)

_SEVERITY_MAP: dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}


def _map_severity(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "CRITICAL"
    if levelno >= logging.ERROR:
        return "ERROR"
    if levelno >= logging.WARNING:
        return "WARNING"
    if levelno >= logging.INFO:
        return "INFO"
    return _SEVERITY_MAP.get(levelno, "DEBUG")


@contextmanager
def log_context(
    *,
    run_id: str | None = None,
    recipient_id: str | None = None,
) -> Iterator[None]:
    """Bind run_id and/or recipient_id into async-safe context for structured logs."""
    run_token = _RUN_ID_CTX.set(run_id) if run_id is not None else None
    recipient_token = _RECIPIENT_ID_CTX.set(recipient_id) if recipient_id is not None else None
    try:
        yield
    finally:
        if recipient_token is not None:
            _RECIPIENT_ID_CTX.reset(recipient_token)
        if run_token is not None:
            _RUN_ID_CTX.reset(run_token)


class JsonCloudLoggingFormatter(logging.Formatter):
    """Emit single-line GCP Cloud Logging JSON correlated with the active OpenTelemetry trace."""

    def __init__(self, project_id: str | None = None) -> None:
        super().__init__()
        self.project_id = project_id.strip() if project_id and project_id.strip() else None

    def _resolve_project_id(self) -> str | None:
        if self.project_id:
            return self.project_id
        for env_key in ("GOOGLE_CLOUD_PROJECT", "GCP_PROJECT_ID"):
            value = os.environ.get(env_key, "").strip()
            if value:
                return value
        return None

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            exc_text = self.formatException(record.exc_info)
            message = f"{message}\n{exc_text}" if message else exc_text
        elif record.stack_info:
            stack_text = self.formatStack(record.stack_info)
            message = f"{message}\n{stack_text}" if message else stack_text

        payload: dict[str, Any] = {
            "severity": _map_severity(record.levelno),
            "message": message,
            "logger": record.name,
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
        }

        run_id = getattr(record, "run_id", None) or _RUN_ID_CTX.get()
        if run_id:
            payload["run_id"] = str(run_id)

        recipient_id = getattr(record, "recipient_id", None) or _RECIPIENT_ID_CTX.get()
        if recipient_id:
            payload["recipient_id"] = str(recipient_id)

        span = trace.get_current_span()
        span_ctx = span.get_span_context() if span is not None else None
        if span_ctx is not None and span_ctx.is_valid:
            trace_hex = f"{span_ctx.trace_id:032x}"
            span_hex = f"{span_ctx.span_id:016x}"
            project_id = self._resolve_project_id()
            if project_id:
                payload["logging.googleapis.com/trace"] = (
                    f"projects/{project_id}/traces/{trace_hex}"
                )
            else:
                payload["trace_id"] = trace_hex
            payload["logging.googleapis.com/spanId"] = span_hex
            payload["logging.googleapis.com/trace_sampled"] = bool(
                span_ctx.trace_flags.sampled
            )

        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    log_format: Literal["text", "json"] = "text",
    project_id: str | None = None,
    *,
    level: int = logging.INFO,
) -> None:
    """Configure root logging for local console text or GCP Cloud Logging structured JSON."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    if log_format == "json":
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(JsonCloudLoggingFormatter(project_id=project_id))
    else:
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )

    root.setLevel(level)
    root.addHandler(stream_handler)
