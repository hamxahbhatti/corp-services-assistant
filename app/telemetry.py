"""OpenTelemetry setup.

Agent Framework emits spans for workflows, executors, chat calls and tool calls; the app adds its own
spans (gateway, retrieval, safety, approval). Spans go to:
  * an in-memory store that powers the trace waterfall in the demo UI
  * OTLP (e.g. the .NET Aspire dashboard / Jaeger) if OTEL_EXPORTER_OTLP_ENDPOINT is set
  * Azure Monitor / Application Insights if APPLICATIONINSIGHTS_CONNECTION_STRING is set (untested adapter)
"""
from __future__ import annotations

import os
import threading
from collections import defaultdict

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

_store: dict[str, list[dict]] = defaultdict(list)
_lock = threading.Lock()
_configured = False


class UISpanExporter(SpanExporter):
    def export(self, spans: list[ReadableSpan]) -> SpanExportResult:
        with _lock:
            for s in spans:
                tid = format(s.context.trace_id, "032x")
                _store[tid].append({
                    "name": s.name, "span_id": format(s.context.span_id, "016x"),
                    "parent_id": format(s.parent.span_id, "016x") if s.parent else None,
                    "start_ns": s.start_time, "end_ns": s.end_time,
                    "attrs": {k: (v if isinstance(v, (str, int, float, bool)) else str(v)) for k, v in (s.attributes or {}).items()
                              if not k.startswith("gen_ai.input") and not k.startswith("gen_ai.output")},
                    "status": s.status.status_code.name,
                })
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        pass


def setup() -> None:
    global _configured
    if _configured:
        return
    _configured = True
    try:
        from agent_framework.observability import configure_otel_providers
        configure_otel_providers(service_name="corp-services-assistant", enable_sensitive_data=False)
    except Exception:  # pragma: no cover
        trace.set_tracer_provider(TracerProvider())
    import logging
    for n in ("agent_framework", "httpx", "httpx2", "httpcore2", "mcp", "httpcore", "openai"):
        logging.getLogger(n).setLevel(logging.WARNING)
    provider = trace.get_tracer_provider()
    if not hasattr(provider, "add_span_processor"):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(UISpanExporter()))
    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):  # pragma: no cover
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        provider.add_span_processor(BatchSpanProcessor(AzureMonitorTraceExporter()))


def tracer():
    return trace.get_tracer("corp-assistant")


def spans_for(trace_ids: list[str]) -> list[dict]:
    with _lock:
        out = [dict(s, trace_id=t) for t in trace_ids for s in _store.get(t, [])]
    return sorted(out, key=lambda s: s["start_ns"])


def current_trace_id() -> str:
    return format(trace.get_current_span().get_span_context().trace_id, "032x")
