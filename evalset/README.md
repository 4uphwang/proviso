# 평가셋

| 파일 | 내용 |
|---|---|
| `examples.jsonl` | 형식 예시 3문항(실제 평가에 쓰지 않음) |
| `retrieval.jsonl` | 검색 세트 1,000문항 |
| `agent.jsonl` | 에이전트 세트 300문항(4단계에서 생성) |

형식과 규칙은 [docs/plan.md §7.1](../docs/plan.md)에 있다. 검증:

```bash
proviso evalset check evalset/*.jsonl
```

## 검색 세트(`retrieval.jsonl`)

정답 조문을 먼저 뽑고(`proviso.eval.targets`, 시드 고정) 그 조문을 보고 질문을 썼다. 작성 규칙은 [GUIDELINES.md](GUIDELINES.md).

| 유형 | 문항 | | 형태 | 문항 |
|---|---:|---|---|---:|
| 조문 찾기(별표 50 포함) | 726 | | 키워드형 | 300 |
| 위임 따라가기 | 100 | | 짧은 구어(오타 50 포함) | 349 |
| 시점 | 95 | | 완결 문장 | 351 |
| 정의 | 79 | | | |

- dev 300 / test 700(유형별 30%). 법 이름이 들어간 질문 약 28%.
- 시점 문항은 옛 본문에 지금과 다른 규칙(숫자·기간·주체·조건)이 있는 조문만 쓴다. 용어·조문 번호만 바뀐 개정은 제외해서 처음 몫 100개를 다 채우지 못했고, 빈 5자리는 조문 찾기로 채웠다.

**검수 경로**
1. Claude가 질문 작성, 자동 검사(조문 번호, 길이, 형태별 끝맺음, 연도, 단어 겹침)
2. 리뷰 에이전트가 정답 조문 전체 본문과 대조해 1,000문항 독립 검토 → 지적된 문항은 고치거나 대상 조문을 바꾸고, 바뀐 문항만 다시 검토(3차까지)
3. 사람이 표본과 바뀐 문항을 검수

`origin`: `human` = 사람이 검수한 질문 그대로(112), `ai_verified` = 리뷰 에이전트 검토 통과(888).

## 평가 실행

```bash
proviso eval --config config/experiments/<이름>.yaml            # dev(설정 고르기)
proviso eval --config config/experiments/<이름>.yaml --split test  # 보고용
proviso compare runs/<A>.json runs/<B>.json --out runs/<A>_vs_<B>.md
```

- 채점: 검색 결과를 조문 버전 단위로 묶은 뒤 주 정답(gold[0])의 순위로 recall@1·5·10·20, MRR@10을 낸다. 시점 문항은 그 시점 버전이어야, 나머지는 실행일에 유효한 버전이어야 정답이다. 위임 문항은 법 조문까지 함께 찾았는지도 따로 센다. 규칙은 `src/proviso/eval/metrics.py` 머리말.
- 칸별: 유형, 질문 형태, 오타, 법령명 포함, 법령, 단어 겹침 구간.
- 신뢰구간: 문항 단위 부트스트랩 1,000회(95%). 비교는 짝지은 부트스트랩으로 B − A 구간이 0을 넘는지 본다.
- 결과 파일(`runs/*.json`, git 제외)에는 설정, 검색기 설명, 데이터 스냅샷 ID, 평가셋 해시, 커밋, 문항별 순위·지연이 들어간다. `compare`는 평가셋·split·문항이 다르면 거부하고, 스냅샷이 다르면 `--allow-snapshot-mismatch` 없이는 거부한다.
- `random` 검색기(우연 수준): test recall@10 0.009, recall@20 0.017.
- CI 점수 검사(`proviso gate check`, `tests/test_gate.py`): 모델·DB 없이 형태소 BM25 + 약칭 확장으로 시점 문항을 뺀 905문항을 돌려, recall@10이 `ci_gate.json`의 기준값 − 허용폭(0.01) 아래면 실패한다. 말뭉치는 `tests/fixtures/corpus.jsonl.gz`(현행 조문, 출처 법제처). 데이터를 다시 뽑으면 `proviso gate export --as-of <날짜>` 후 `proviso gate check --update`.
