# proviso

> **proviso**: 단서 조항. 근거 조항이 있을 때만 답한다.

금융 법령(법률·시행령, 연혁 포함)과 약관을 근거로만 답하는 도구 사용 에이전트, 그리고 검색·임베딩·LLM을 같은 평가셋으로 비교하는 평가 하네스입니다. 진행 중이며 계획은 [docs/plan.md](docs/plan.md)에 있습니다.

> 이 저장소의 답변은 **법률 자문이 아닙니다.** 원문과 전문가 확인이 필요합니다.
> 법령 출처: 법제처 국가법령정보센터(Open API)

## 실행
```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env          # LAW_OC 등 값 채우기(커밋 금지)
docker compose up -d          # Postgres 17 + pgvector, 포트 5433
.venv/bin/proviso db init
.venv/bin/proviso evalset check evalset/examples.jsonl
.venv/bin/pytest
```

## 문서
- [계획서](docs/plan.md)
- [법령 연혁 분량 조사](docs/research-versioning.md)
- [법적 검토 메모](docs/legal-review.md)
