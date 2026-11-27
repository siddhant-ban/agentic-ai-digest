"""OpenTelemetry tracing and GCP Cloud Logging structured JSON logging."""

from __future__ import annotations

from agentic_emailer.observability.logging_config import (
    JsonCloudLoggingFormatter,
    configure_logging,
    log_context,
)
from agentic_emailer.observability.telemetry import (
    DEFAULT_SERVICE_NAME,
    TelemetryHandle,
    get_current_trace_id,
    get_tracer,
    init_telemetry,
)

__all__ = [
    "DEFAULT_SERVICE_NAME",
    "JsonCloudLoggingFormatter",
    "TelemetryHandle",
    "configure_logging",
    "get_current_trace_id",
    "get_tracer",
    "init_telemetry",
    "log_context",
]
