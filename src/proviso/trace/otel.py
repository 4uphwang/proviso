"""기록을 OpenTelemetry로 내보낸다(15장, 계획서 §6). pip install -e '.[otel]'

runs/traces/*.jsonl의 기록 하나 = OTel trace 하나. 질문 전체가 부모 span(invoke_agent), 그 아래 모델 호출(chat)과
도구 호출(execute_tool)이 순서대로 자식 span이 된다. 이름은 OTel 생성형 AI 규약(gen_ai.*)을 따르고,
Phoenix가 종류를 알아보도록 OpenInference 속성(openinference.span.kind 등)도 함께 단다.

- trace id는 proviso 기록 id(uuid)를 그대로 쓴다. Phoenix에서 본 id로 `proviso trace <id>`를 찾을 수 있다.
- 기록에는 단계마다 걸린 시간만 있어서, 시작 시각에 걸린 시간을 차례로 더해 각 단계의 시작·끝을 만든다.
- 질문은 11장에서 개인정보를 가린 뒤의 것이 기록에 있다. 도구 결과 본문은 기록에 없어 보인 조문 이름만 보낸다.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from proviso.trace.store import DIR


def _provider(model: str) -> str:
    return "openrouter" if model.startswith("openrouter:") else "ollama"


def _model_id(model: str) -> str:
    return model.split(":", 1)[1] if model.startswith("openrouter:") else model


def load_records(trace_ids: list[str] | None = None, day: str | None = None, root: Path = DIR) -> list[dict]:
    """trace_ids(앞 몇 글자)에 맞는 기록, 또는 day(YYYYMMDD)의 기록 전부."""
    files = [root / f"{day}.jsonl"] if day else sorted(root.glob("*.jsonl"))
    out = []
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if not trace_ids or any(r["trace_id"].startswith(t) for t in trace_ids):
                out.append(r)
    return out


def _ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


def export_records(records: list[dict], exporter) -> int:
    """기록들을 exporter(OTLPSpanExporter, InMemorySpanExporter 등)로 보낸다. 보낸 span 수."""
    from opentelemetry import trace as ot
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator

    class Ids(IdGenerator):
        """부모 span의 trace id를 proviso 기록 id로 맞춘다."""
        next_trace: int | None = None
        _rand = RandomIdGenerator()

        def generate_span_id(self) -> int:
            return self._rand.generate_span_id()

        def generate_trace_id(self) -> int:
            return self.next_trace or self._rand.generate_trace_id()

    ids = Ids()
    provider = TracerProvider(resource=Resource.create({"service.name": "proviso", "openinference.project.name": "proviso"}),
                              id_generator=ids)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("proviso.trace.otel")
    n = 0
    for r in records:
        ids.next_trace = uuid.UUID(r["trace_id"]).int
        cfg = r.get("config") or {}
        model = cfg.get("model", "")
        spans = r.get("spans") or []
        t = _ns(datetime.fromisoformat(r["started_at"]))
        total = sum(s["latency_ms"] for s in spans) * 1_000_000
        llm = [s for s in spans if s["kind"] == "llm"]
        root_attrs: dict[str, Any] = {
            "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "proviso",
            "gen_ai.provider.name": _provider(model), "gen_ai.request.model": _model_id(model),
            "gen_ai.usage.input_tokens": sum(s["input_tokens"] or 0 for s in llm),
            "gen_ai.usage.output_tokens": sum(s["output_tokens"] or 0 for s in llm),
            "openinference.span.kind": "AGENT", "input.value": r["question"], "output.value": r["answer"],
            "proviso.trace_id": r["trace_id"], "proviso.refused": bool(r["refused"]), "proviso.stop": r.get("stop") or "",
            "proviso.snapshot_id": r.get("snapshot_id") or "",
        }
        if cfg.get("as_of"):
            root_attrs["proviso.as_of"] = cfg["as_of"]
        root = tracer.start_span("invoke_agent proviso", start_time=t, attributes=root_attrs)
        ctx = ot.set_span_in_context(root)
        cur = t
        for s in spans:
            end = cur + max(s["latency_ms"], 1) * 1_000_000  # 0초 단계도 1ms를 줘 순서가 뒤섞이지 않게(Phoenix는 ms 단위)
            out = s.get("output") or {}
            if s["kind"] == "llm":
                used = out.get("model") or _model_id(model)
                a = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": _provider(model),
                     "gen_ai.request.model": _model_id(model), "gen_ai.response.model": used,
                     "gen_ai.usage.input_tokens": s["input_tokens"] or 0, "gen_ai.usage.output_tokens": s["output_tokens"] or 0,
                     "openinference.span.kind": "LLM", "llm.model_name": used,
                     "llm.token_count.prompt": s["input_tokens"] or 0, "llm.token_count.completion": s["output_tokens"] or 0,
                     "proviso.final": bool(out.get("final"))}
                if out.get("attempts"):
                    a["proviso.attempts"] = out["attempts"]
                if out.get("fallback_from"):
                    a["proviso.fallback_from"] = out["fallback_from"]
                name = f"chat {used}"
            else:
                a = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": s["name"],
                     "openinference.span.kind": "TOOL", "tool.name": s["name"],
                     "input.value": json.dumps(s.get("input"), ensure_ascii=False),
                     "output.value": ", ".join(out.get("shown") or []) or ("오류" if out.get("error") else "없음"),
                     "proviso.chars": out.get("chars") or 0, "proviso.repeat": bool(out.get("repeat"))}
                name = f"execute_tool {s['name']}"
            sp = tracer.start_span(name, context=ctx, start_time=cur, attributes=a | {"proviso.seq": s["seq"]})
            if s["kind"] == "tool" and out.get("error"):
                from opentelemetry.trace import Status, StatusCode
                sp.set_status(Status(StatusCode.ERROR, "도구 오류"))
            sp.end(end_time=end)
            cur = end
            n += 1
        root.end(end_time=max(t + total, cur))
        n += 1
    provider.shutdown()
    return n


def otlp_exporter(endpoint: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=endpoint)
