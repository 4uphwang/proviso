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

-- 법령의 버전 하나 = (개정 MST, 시행일). 시행일 법령 목록(eflaw) 기준이라
-- 부칙 부분 시행이 있으면 같은 MST가 시행일마다 따로 들어온다.
CREATE TABLE IF NOT EXISTS law_versions (
  doc_id         text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  mst            text NOT NULL,              -- 법령일련번호(개정 고유 번호)
  effective_on   date NOT NULL,              -- 시행일
  promulgated_on date,                       -- 공포일
  revision_kind  text,                       -- 제정, 일부개정, 전부개정, 타법개정 ...
  status         text,                       -- 현행 / 시행예정 / 연혁(수집 시점 기준)
  title          text,                       -- 이 버전의 공식 명칭(제명 변경 대비)
  collected_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (doc_id, mst, effective_on)
);
CREATE INDEX IF NOT EXISTS law_versions_effective ON law_versions (doc_id, effective_on);

-- 조(條)의 고유 본문. 버전이 달라도 본문이 같으면 한 행이다. 원문 필드(text)는 받은 그대로 둔다.
CREATE TABLE IF NOT EXISTS article_texts (
  text_id       text PRIMARY KEY,            -- sha256(doc_id + 조 번호 + 원문)
  doc_id        text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  article_no    text NOT NULL,               -- '제7조의2', 별표는 '별표 1', '별표 1의2'
  kind          text NOT NULL DEFAULT 'article' CHECK (kind IN ('article', 'appendix')),
  article_title text,
  text          text NOT NULL,               -- 원문(변형 금지)
  search_text   text,                        -- 색인용 파생 텍스트(별표: 표 테두리 제거). NULL이면 text 사용
  paragraphs    jsonb NOT NULL DEFAULT '[]', -- [{"no": 1, "text": "...", "items": [{"no": "1", "text": "..."}]}]
  refs          jsonb NOT NULL DEFAULT '[]', -- [{"doc_id": null|"...", "law_name": ..., "article": "제5조", "paragraph": 1, "item": "3"}]
  delegation_phrases jsonb NOT NULL DEFAULT '[]', -- 법률: "대통령령으로 정하는 …" 위치 [{"paragraph", "item", "subitem", "phrase"}]
  delegated_from jsonb NOT NULL DEFAULT '[]', -- 시행령: '법 제N조…에서 "대통령령으로 정하는 …"'로 밝힌 위임 조항
  deleted       boolean NOT NULL DEFAULT false  -- "제N조 삭제 <날짜>"
);

-- 조문이 유효한 기간. 시점 질의는 valid_from <= as_of < valid_to(NULL이면 무기한)로 거른다.
CREATE TABLE IF NOT EXISTS article_spans (
  chunk_id      text PRIMARY KEY,            -- '<doc_id>:<article_no>@<valid_from>' (예: '전자금융거래법:제2조@2026-10-02')
  doc_id        text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
  article_no    text NOT NULL,
  sort_key      integer NOT NULL,            -- 조 번호 * 100 + 가지 번호
  text_id       text NOT NULL REFERENCES article_texts(text_id),
  mst           text NOT NULL,               -- 이 기간을 만든 버전(같은 시행일 버전이 둘이면 늦게 공포된 쪽)
  version_effective_on date NOT NULL,        -- 그 버전의 시행일(보정 전 valid_from)
  valid_from    date NOT NULL,
  valid_to      date,
  valid_from_basis text NOT NULL DEFAULT 'version'
    CHECK (valid_from_basis IN ('version', 'addendum')),  -- 'version' = 시행일 법령 API, 'addendum' = 부칙을 근거로 직접 추정
  addendum_ref  text,                        -- 보정 근거 부칙 조항
  heading       text,                        -- 이 버전에서 속한 장·절 제목
  note          text,                        -- 조문참고자료(예: [본조신설 …], [시행일: …])
  FOREIGN KEY (doc_id, mst, version_effective_on) REFERENCES law_versions(doc_id, mst, effective_on)
);
CREATE INDEX IF NOT EXISTS article_spans_lookup ON article_spans (doc_id, article_no, valid_from);
CREATE INDEX IF NOT EXISTS article_spans_text ON article_spans (text_id);

-- 법 조문 → 시행령 조문 위임 연결("대통령령으로 정하는 …")
CREATE TABLE IF NOT EXISTS delegations (
  from_text_id text NOT NULL REFERENCES article_texts(text_id) ON DELETE CASCADE,
  to_text_id   text NOT NULL REFERENCES article_texts(text_id) ON DELETE CASCADE,
  phrase       text NOT NULL,                -- 근거 문구
  source       text NOT NULL DEFAULT 'parsed' CHECK (source IN ('parsed', 'api')),  -- 파싱으로 찾음 / 위임법령 API
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

-- 검색 색인 하나 = (청킹 방식, 임베딩 모델, 기준일, 스냅샷). 같은 이름의 색인을 다시 만들면 새 index_id가 생긴다.
CREATE TABLE IF NOT EXISTS index_versions (
  index_id     text PRIMARY KEY,             -- '<이름>-<설정·스냅샷 해시 8자리>'
  name         text NOT NULL,                -- config/indexes/<이름>.yaml
  method       jsonb NOT NULL,               -- 청킹·모델·파라미터 전체
  snapshot_id  text REFERENCES snapshots(snapshot_id),
  as_of        date NOT NULL,                -- 이날 유효한 조문으로 만든 색인(현행만 넣는 기준선)
  chunk_count  integer NOT NULL,
  build_seconds numeric,
  created_at   timestamptz NOT NULL DEFAULT now()
);
-- 17장: 새로 임베딩한 조각 수(나머지는 이전 색인에서 재사용). 전부 새로 만들면 chunk_count와 같다
ALTER TABLE index_versions ADD COLUMN IF NOT EXISTS embedded integer;

-- 색인 조각. 고정 길이 청킹은 조 경계를 무시하므로 조각 하나가 조문 여러 개에 걸친다(articles).
-- embedding은 차원을 고정하지 않는다(모델마다 다름). 조문 수천 개 규모라 정확 검색(전수 비교)으로 충분하다.
CREATE TABLE IF NOT EXISTS index_chunks (
  index_id   text NOT NULL REFERENCES index_versions(index_id) ON DELETE CASCADE,
  chunk_no   integer NOT NULL,
  doc_id     text NOT NULL REFERENCES documents(doc_id),
  text       text NOT NULL,
  articles   jsonb NOT NULL,                 -- [{"article_no", "valid_from", "valid_to", "chars"}] 조각 안 순서대로
  embedding  vector,
  PRIMARY KEY (index_id, chunk_no)
);

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
