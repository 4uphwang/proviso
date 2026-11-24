"""15장: 기록 → OTel span. 메모리로 내보내 구조·이름·속성을 본다(받는 서버 없이)."""

import uuid

import pytest

pytest.importorskip("opentelemetry.sdk")
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from proviso.trace.otel import export_records  # noqa: E402

TID = "dd932fa8-0000-4000-8000-000000000001"
REC = {
    "trace_id": TID, "started_at": "2026-11-24T10:00:00+00:00", "question": "질문", "answer": "답",
    "refused": False, "stop": "answered", "snapshot_id": "s1",
    "config": {"model": "openrouter:deepseek/no-such-model", "as_of": None},
    "spans": [
        {"seq": 1, "kind": "tool", "name": "search", "input": {"query": "질문"}, "latency_ms": 700,
         "output": {"shown": ["법 제1조"], "chars": 100, "error": False, "repeat": False}, "input_tokens": None, "output_tokens": None},
        {"seq": 2, "kind": "llm", "name": "chat", "input": None, "latency_ms": 2000, "input_tokens": 50, "output_tokens": 10,
         "output": {"final": True, "model": "z-ai/glm-5.3-flash", "fallback_from": "deepseek/no-such-model"}},
    ],
}


def test_record_becomes_agent_span_with_children():
    ex = InMemorySpanExporter()
    assert export_records([REC], ex) == 3
    spans = {s.name: s for s in ex.get_finished_spans()}
    root, tool, llm = spans["invoke_agent proviso"], spans["execute_tool search"], spans["chat z-ai/glm-5.3-flash"]
    assert root.context.trace_id == uuid.UUID(TID).int == tool.context.trace_id == llm.context.trace_id
    assert tool.parent.span_id == root.context.span_id == llm.parent.span_id
    assert root.attributes["gen_ai.usage.input_tokens"] == 50 and root.attributes["openinference.span.kind"] == "AGENT"
    assert llm.attributes["gen_ai.request.model"] == "deepseek/no-such-model"
    assert llm.attributes["gen_ai.response.model"] == "z-ai/glm-5.3-flash"
    assert llm.attributes["proviso.fallback_from"] == "deepseek/no-such-model"
    assert tool.attributes["output.value"] == "법 제1조"
    # 단계가 차례로 이어지고 부모가 둘을 덮는다
    assert tool.end_time == llm.start_time and root.start_time == tool.start_time and root.end_time == llm.end_time
    assert (llm.end_time - tool.start_time) == 2_700_000_000
