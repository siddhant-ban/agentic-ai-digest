"""OpenTelemetry TracerProvider, GCP Cloud Trace exporter, and GenAI SDK instrumentation."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from agentic_emailer.config import Settings

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "agentic-emailer"
DEFAULT_FLUSH_TIMEOUT_MS = 10_000


@dataclass
class TelemetryHandle:
    """Lifecycle handle guaranteeing OTel span flush and shutdown on Cloud Run Job exit."""

    provider: TracerProvider | None = None
    cloud_trace_enabled: bool = False
    _instrumentor: Any | None = field(default=None, repr=False)
    _owns_provider: bool = field(default=True, repr=False)
    _shutdown_called: bool = field(default=False, repr=False)

    def shutdown(self, timeout_millis: int = DEFAULT_FLUSH_TIMEOUT_MS) -> None:
        """Force-flush buffered spans and shut down the TracerProvider safely."""
        if self._shutdown_called:
            return
        self._shutdown_called = True

        try:
            if self._instrumentor is not None:
                try:
                    self._instrumentor.uninstrument()
                except Exception as exc:
                    logger.debug("Failed to uninstrument Google GenAI SDK: %s", exc)
        finally:
            if self.provider is not None:
                try:
                    flushed = self.provider.force_flush(timeout_millis=timeout_millis)
                    if not flushed:
                        logger.warning(
                            "OpenTelemetry force_flush did not complete within %dms",
                            timeout_millis,
                        )
                except Exception as exc:
                    logger.warning("OpenTelemetry force_flush failed: %s", exc)
                finally:
                    if self._owns_provider:
                        try:
                            self.provider.shutdown()
                        except Exception as exc:
                            logger.warning("OpenTelemetry TracerProvider shutdown failed: %s", exc)


def init_telemetry(settings: Settings) -> TelemetryHandle:
    """Initialize OpenTelemetry tracing, optional Cloud Trace export, and GenAI instrumentation."""
    service_name = os.environ.setdefault("OTEL_SERVICE_NAME", DEFAULT_SERVICE_NAME)
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.version": "1.0.0",
        }
    )

    existing_provider = trace.get_tracer_provider()
    owns_provider = True
    if isinstance(existing_provider, TracerProvider):
        provider = existing_provider
        owns_provider = False
    else:
        provider = TracerProvider(resource=resource)
        trace.set_tracer_provider(provider)

    cloud_trace_active = False
    if settings.otel_cloud_trace_enabled:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            exporter = CloudTraceSpanExporter(project_id=settings.gcp_project_id)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            cloud_trace_active = True
            logger.info(
                "OpenTelemetry Cloud Trace exporter enabled (project=%s, service=%s)",
                settings.gcp_project_id or "default",
                service_name,
            )
        except Exception as exc:
            logger.warning(
                "Failed to initialize CloudTraceSpanExporter (%s); continuing with local tracing",
                exc,
            )

    instrumentor: Any | None = None
    try:
        from opentelemetry.instrumentation.google_genai import GoogleGenAiSdkInstrumentor

        candidate = GoogleGenAiSdkInstrumentor()
        if not getattr(candidate, "is_instrumented_by_opentelemetry", False):
            candidate.instrument(tracer_provider=provider)
            instrumentor = candidate
    except Exception as exc:
        logger.warning(
            "Failed to instrument Google GenAI SDK with OpenTelemetry (%s); continuing",
            exc,
        )

    return TelemetryHandle(
        provider=provider,
        cloud_trace_enabled=cloud_trace_active,
        _instrumentor=instrumentor,
        _owns_provider=owns_provider,
    )


def get_tracer(name: str = "agentic_emailer") -> trace.Tracer:
    """Return an OpenTelemetry Tracer bound to the active TracerProvider."""
    return trace.get_tracer(name)


def get_current_trace_id() -> str | None:
    """Return the 32-char lowercase hex trace ID for the active span, or None if inactive."""
    span = trace.get_current_span()
    if span is None:
        return None
    span_ctx = span.get_span_context()
    if span_ctx is None or not span_ctx.is_valid:
        return None
    return f"{span_ctx.trace_id:032x}"
