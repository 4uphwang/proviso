# 평가셋

| 파일 | 내용 |
|---|---|
| `examples.jsonl` | 형식 예시 3문항(실제 평가에 쓰지 않음) |
| `retrieval.jsonl` | 검색 세트 1,000문항(2단계에서 생성) |
| `agent.jsonl` | 에이전트 세트 300문항(4단계에서 생성) |

형식과 규칙은 [docs/plan.md §7.1](../docs/plan.md)에 있다. 검증:

```bash
proviso evalset check evalset/*.jsonl
```
