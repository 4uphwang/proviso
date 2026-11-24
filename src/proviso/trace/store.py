"""요청 하나의 기록(6장): 단계별 도구 호출·LLM 호출을 DB(traces·spans)와 runs/traces/*.jsonl에 남긴다.

로컬 모델이라 비용은 0으로 적는다. 질문은 그대로 남으므로 개인정보를 넣은 질문을 걸러 내는 것은 8장에서 다룬다.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from proviso.agent.loop import Trace
from proviso.config import ROOT

DIR = ROOT / "runs" / "traces"


def spans(tr: Trace) -> list[dict]:
    """Trace.steps → span 목록. 도구 span의 출력은 본문 대신 보인 조문과 글자 수만 남긴다."""
    out = []
    llm_total = sum("llm" in s for s in tr.steps)
    llm_seen = 0
    for i, s in enumerate(tr.steps, 1):
        base = {"span_id": str(uuid.uuid4()), "seq": i, "latency_ms": round(s["seconds"] * 1000), "cost_usd": 0}
        if "tool" in s:
            out.append(base | {"kind": "tool", "name": s["tool"], "input": s["args"],
                               "output": {"shown": [" ".join(k) for k in s["shown"]], "chars": s["chars"], "error": s["error"],
                                          "repeat": s["repeat"]}, "input_tokens": None, "output_tokens": None})
        else:
            llm_seen += 1
            out.append(base | {"kind": "llm", "name": "chat", "input": None,
                               "output": {"final": llm_seen == llm_total,
                                          **{k: s[k] for k in ("attempts", "model", "fallback_from") if k in s}},
                               "input_tokens": s["prompt_tokens"],
                               "output_tokens": s["output_tokens"]})
    return out


def save(conn, tr: Trace, *, question: str, answer: str, refused: bool, config: dict, snapshot_id: str | None,
         out_dir: Path = DIR) -> str:
    tid = str(uuid.uuid4())
    sp = spans(tr)
    with conn.transaction():
        conn.execute("""INSERT INTO traces (trace_id, question, config, snapshot_id, answer, refused, latency_ms, input_tokens,
                        output_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0)""",
                     (tid, question, json.dumps(config, ensure_ascii=False), snapshot_id, answer, refused,
                      round(tr.seconds * 1000), tr.prompt_tokens, tr.output_tokens))
        for s in sp:
            conn.execute("""INSERT INTO spans (span_id, trace_id, seq, kind, name, input, output, latency_ms, input_tokens,
                            output_tokens, cost_usd) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                         (s["span_id"], tid, s["seq"], s["kind"], s["name"], json.dumps(s["input"], ensure_ascii=False),
                          json.dumps(s["output"], ensure_ascii=False), s["latency_ms"], s["input_tokens"], s["output_tokens"],
                          s["cost_usd"]))
    now = datetime.now(timezone.utc)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / f"{now:%Y%m%d}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"trace_id": tid, "started_at": now.isoformat(), "question": question, "config": config,
                            "snapshot_id": snapshot_id, "answer": answer, "refused": refused, "stop": tr.stop,
                            "spans": sp}, ensure_ascii=False) + "\n")
    return tid


def show(conn, trace_id: str) -> str:
    """단계별 표. trace_id는 앞 몇 글자만 줘도 된다."""
    rows = conn.execute("""SELECT trace_id, question, refused, latency_ms, input_tokens, output_tokens, answer FROM traces
                           WHERE trace_id::text LIKE %s ORDER BY started_at DESC LIMIT 2""", (trace_id + "%",)).fetchall()
    if len(rows) != 1:
        raise ValueError("그런 기록이 없음" if not rows else "여러 기록이 맞음. 더 길게 적을 것")
    tid, q, refused, ms, itok, otok, ans = rows[0]
    lines = [f"질문: {q}", f"전체 {ms / 1000:.1f}초, 토큰 {itok}+{otok}, {'거절' if refused else '답변'}", "",
             "| # | 단계 | 초 | 토큰 | 내용 |", "|---:|---|---:|---:|---|"]
    for seq, kind, name, inp, out, lat, a, b in conn.execute(
            """SELECT seq, kind, name, input, output, latency_ms, input_tokens, output_tokens FROM spans
               WHERE trace_id = %s ORDER BY seq""", (tid,)):
        if kind == "tool":
            what = f"{json.dumps(inp, ensure_ascii=False)} → {', '.join(out['shown']) or ('오류' if out['error'] else '없음')}"
        else:
            what = "답" if out["final"] else "도구 고르기"
        tok = f"{a}+{b}" if a is not None else "-"
        lines.append(f"| {seq} | {kind}:{name} | {lat / 1000:.1f} | {tok} | {what} |")
    return "\n".join(lines + ["", ans or ""])
