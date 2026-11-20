-- proviso 스키마. docker compose 첫 기동 때 자동 적용되고, CI에서도 같은 파일을 쓴다.
-- 모든 문장은 다시 실행해도 안전하다(IF NOT EXISTS).
CREATE EXTENSION IF NOT EXISTS vector;

-- 문서 하나: 법률, 시행령, 약관
CREATE TABLE IF NOT EXISTS documents (
  doc_id        text PRIMARY KEY,            -- config/sources.yaml의 id. 예: '전자금융거래법', '전자금융거래법 시행령'
  doc_type      text NOT NULL CHECK (doc_type IN ('act', 'decree', 'terms')),
  parent_doc_id text REFERENCES documents(doc_id),  -- 시행령 → 모법
  title         text NOT NULL,               -- 공식 명칭
  law_id        text,                        -- 법령ID(버전과 무관한 고유 번호)
  source_url    text,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- 약칭: '금소법' → '금융소비자보호법'
CREATE TABLE IF NOT EXISTS law_aliases (
  alias  text PRIMARY KEY,
  doc_id text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  origin text NOT NULL CHECK (origin IN ('api', 'manual'))
);

-- 법령의 버전 하나(공포·시행 단위)
CREATE TABLE IF NOT EXISTS law_versions (
  doc_id         text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  mst            text NOT NULL,              -- 법령일련번호(버전 고유 번호)
  promulgated_on date,                       -- 공포일
  effective_on   date NOT NULL,              -- 시행일
  revision_kind  text,                       -- 제정, 일부개정, 전부개정, 타법개정 ...
  collected_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (doc_id, mst)
);
CREATE INDEX IF NOT EXISTS law_versions_effective ON law_versions (doc_id, effective_on);

-- 조(條)의 고유 본문. 버전이 달라도 본문이 같으면 한 행이다. 원문 필드(text)는 받은 그대로 둔다.
CREATE TABLE IF NOT EXISTS article_texts (
  text_id       text PRIMARY KEY,            -- sha256(doc_id + 조 번호 + 원문)
  doc_id        text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  article_no    text NOT NULL,               -- '제7조의2'
  article_title text,
  text          text NOT NULL,               -- 원문(변형 금지)
  paragraphs    jsonb NOT NULL DEFAULT '[]', -- [{"no": 1, "text": "...", "items": [{"no": "1", "text": "..."}]}]
  refs          jsonb NOT NULL DEFAULT '[]'  -- [{"doc_id": null|"...", "article": "제5조", "paragraph": 1}]
);

-- 조문이 유효한 기간. 시점 질의는 valid_from <= as_of < valid_to(NULL이면 무기한)로 거른다.
CREATE TABLE IF NOT EXISTS article_spans (
  chunk_id      text PRIMARY KEY,            -- '<doc_id>:<article_no>@<valid_from>'
  doc_id        text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  article_no    text NOT NULL,
  sort_key      integer NOT NULL,            -- 조 번호 * 100 + 가지 번호
  text_id       text NOT NULL REFERENCES article_texts(text_id),
  mst           text NOT NULL,               -- 이 기간을 만든 버전
  valid_from    date NOT NULL,
  valid_to      date,
  valid_from_basis text NOT NULL DEFAULT 'version'
    CHECK (valid_from_basis IN ('version', 'addendum')),  -- 'addendum' = 부칙을 근거로 추정한 유효 기간
  addendum_ref  text,                        -- 보정 근거 부칙 조항
  FOREIGN KEY (doc_id, mst) REFERENCES law_versions(doc_id, mst)
);
CREATE INDEX IF NOT EXISTS article_spans_lookup ON article_spans (doc_id, article_no, valid_from);
CREATE INDEX IF NOT EXISTS article_spans_text ON article_spans (text_id);

-- 법 조문 → 시행령 조문 위임 연결("대통령령으로 정하는 …")
CREATE TABLE IF NOT EXISTS delegations (
  from_text_id text NOT NULL REFERENCES article_texts(text_id) ON DELETE CASCADE,
  to_text_id   text NOT NULL REFERENCES article_texts(text_id) ON DELETE CASCADE,
  phrase       text NOT NULL,                -- 근거 문구
  PRIMARY KEY (from_text_id, to_text_id)
);

-- 수집 스냅샷. 평가 결과가 snapshot_id를 기록한다.
CREATE TABLE IF NOT EXISTS snapshots (
  snapshot_id  text PRIMARY KEY,             -- 'YYYYMMDDTHHMMSS-<해시 앞 8자리>'
  created_at   timestamptz NOT NULL DEFAULT now(),
  latest_mst   jsonb NOT NULL,               -- {"doc_id": "mst", ...}
  article_count integer NOT NULL,
  content_hash text NOT NULL
);

-- 임베딩은 모델마다 차원이 달라 색인 단계에서 emb_<모델> 테이블을 만든다(text_id 기준).

-- 트레이스: 요청 하나 = trace, 단계 하나(LLM 호출, 도구 호출, 검색) = span
CREATE TABLE IF NOT EXISTS traces (
  trace_id      uuid PRIMARY KEY,
  started_at    timestamptz NOT NULL DEFAULT now(),
  question      text NOT NULL,
  as_of         date,
  config        jsonb NOT NULL DEFAULT '{}',
  snapshot_id   text REFERENCES snapshots(snapshot_id),
  answer        text,
  refused       boolean,
  latency_ms    integer,
  input_tokens  integer,
  output_tokens integer,
  cost_usd      numeric(12, 6)
);
CREATE TABLE IF NOT EXISTS spans (
  span_id          uuid PRIMARY KEY,
  trace_id         uuid NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
  seq              integer NOT NULL,
  kind             text NOT NULL CHECK (kind IN ('llm', 'tool', 'retrieve')),
  name             text NOT NULL,
  input            jsonb,
  output           jsonb,
  latency_ms       integer,
  input_tokens     integer,
  output_tokens    integer,
  cached_tokens    integer,
  reasoning_tokens integer,
  cost_usd         numeric(12, 6)
);
CREATE INDEX IF NOT EXISTS spans_trace ON spans (trace_id, seq);
