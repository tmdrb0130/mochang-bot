# RAG 설계 초안 — 조사 자료 벡터DB 축적·재사용 (LlamaIndex)

> 2026-09-02 교수님 요청. 상태: **구현 착수 (10:2x 사용자 "진행")** — 색인·조회 모듈은 세션2, 파이프라인 연결은 세션3.
> 결정 사항(CLAUDE.md): RAG 는 **LlamaIndex**, 검색은 Vane. 임베딩·LLM 은 로컬이라 비용 없음.

## 1. 목표

지금은 조사해온 뉴스·자료가 7일 디스크 캐시(`backend/.cache/research/`)에 있다가 **그냥 버려진다**.
게다가 캐시 키가 (아이디어 텍스트, 문항) 해시라 **아이디어 문구가 한 글자만 달라도 재조사**한다.

→ 읽어온 페이지를 버리지 않고 **벡터DB에 계속 쌓아서**, 유사한 주제의 요청이 오면
외부 검색 전에 먼저 꺼내 쓴다.

**두 저장소의 역할 분담 (2026-09-02 사용자 확인):** 7일 디스크 캐시는 그대로 유지한다 —
동일 요청의 단기 즉시 응답용. 벡터DB 는 **만료 없는 영구 축적층**으로 그 뒤에 붙는다.
캐시가 7일 지나 사라져도 원문은 벡터DB 에 남아, 재검색 없이 유사도 조회로 복원된다.

효과:

- 속도: 외부 검색+본문 추출(문항당 45~100초)이 벡터 조회(1초 미만)로 대체되는 비율이 커짐
- 부하: 외부 검색 호출이 줄어 봇 차단 위험 감소 (BOTTLENECKS V2 완화)
- 사용자가 늘수록 자료가 쌓여 **서로의 조사를 재사용** — 규모가 클수록 유리

## 2. 흐름 (조사 파이프라인 변경)

```
지금:   검색어 생성 → 웹 검색(ddgs) → 본문 추출 → 사실 추출
바뀜:   검색어 생성 → ① 벡터DB 유사도 검색
                       ├ 충분 (유사도·건수 임계 통과) → 그 문서로 사실 추출  [웹 안 감]
                       └ 부족 → ② 웹 검색 → 본문 추출 → ③ 벡터DB에 색인 추가 → 사실 추출
```

- ①의 "충분" 판정이 품질의 관문. **보수적으로 시작** (예: 유사도 0.8 이상 문서 5건 이상).
  낮추면 엉뚱한 주제의 자료가 근거로 끼어든다 — 지금의 "옛 아이디어 조사 재사용 버그"를
  서버 차원에서 재발시키는 꼴이 되므로 주의.
- ②로 가더라도 ③에서 색인이 쌓이므로 시스템은 쓸수록 빨라진다.

## 3. 저장 설계

| 항목 | 안 | 비고 |
|---|---|---|
| 색인 단위 | 추출된 본문을 청크(약 512토큰)로 | LlamaIndex `SentenceSplitter` 기본 |
| 메타데이터 | url, title, publisher, date(기사 날짜), fetched_at, source_kind(news/stats/report) | **date 필수** — 신선도 필터용 |
| 중복 제거 | url 해시로 upsert (같은 url 재색인 안 함) | |
| 임베딩 | 로컬 (Ollama `bge-m3` 또는 HuggingFace 한국어 모델) | **결정 필요** — 아래 5절 |
| 벡터 스토어 | 1단계: LlamaIndex 기본 persist(디스크). 커지면 Chroma 등 검토 | `backend/.vectorstore/` (커밋 금지, .gitignore 추가) |
| 신선도 | 뉴스류는 date 2년 초과 시 검색 결과에서 제외 또는 감점 | 낡은 통계 인용 방지 |

## 4. 단계별 구현 (세션3)

1. **색인만 먼저** — 조사 파이프라인이 본문 추출 후 벡터DB에 저장만 한다. 동작 변화 없음, 위험 0.
2. **조회 연결** — 검색 전에 벡터DB 조회, 임계 통과 시 웹 생략. 임계값은 config.yaml 로 빼서 조정 가능하게.
3. **대량 선색인** — KOSIS·data.go.kr 공공데이터를 미리 색인 (PROGRESS 백로그 #6 "외부 의존 0"의 길).
   작업 8(네이버·공공데이터 API)과 합류하는 지점.

각 단계마다 mock 테스트 (실제 임베딩 모델 없이 fake embedding 으로 색인·조회 검증).

## 5. 결정 (2026-09-02 10:2x 사용자 — 세션1 추천안 그대로 진행)

- [x] 임베딩 모델: **로컬 Ollama `bge-m3`** (`ollama pull bge-m3`, 사용자 실행). LlamaIndex `OllamaEmbedding`.
- [x] "충분" 임계 초기값: **유사도 0.8 이상 · 5건 이상** — `config.yaml research.vectorstore.{min_score,min_hits}` 로 조정 가능하게.
- [x] 뉴스 신선도: **date 2년 초과 제외** — `max_age_days: 730`.

## 5-1. 모듈 인터페이스 (세션2 구현 `backend/rag/vectorstore.py`, 세션3 이 파이프라인에 연결)

세션2·세션3 가 같은 파일을 안 건드리도록 경계를 여기서 고정한다. **이 서명은 세션1 승인 없이 바꾸지 않는다.**

```python
class VectorStore:
    def __init__(self, persist_dir: str, embed_model=None, min_score=0.8, min_hits=5, max_age_days=730): ...
    # pipeline.collect_pages 가 만드는 page dict 그대로 받는다: url, title, text, publisher?, date?, query, ...
    # url 해시로 upsert — 같은 url 은 재색인하지 않는다. 반환: 새로 색인된 건수.
    def upsert_pages(self, pages: list[dict]) -> int: ...
    # 질의 텍스트로 유사도 조회. page dict 와 같은 형태 + "score" 를 붙여 반환 (신선도 컷 적용 후).
    def query(self, text: str, k: int = 8) -> list[dict]: ...
    # query() 결과가 "충분" 임계를 넘는지 — 파이프라인이 웹 검색 생략 여부를 이걸로 판단.
    def is_sufficient(self, hits: list[dict]) -> bool: ...
```

- 동기 함수로 둔다(LlamaIndex 기본이 동기). 파이프라인은 `asyncio.to_thread` 로 감싼다.
- `embed_model=None` 이면 LlamaIndex `MockEmbedding` 계열 fake 를 써서 **테스트가 Ollama 없이 돈다**.
- persist 경로 `backend/.vectorstore/` (gitignore 완료). 색인은 LlamaIndex 기본 `StorageContext.persist`.
- 의존성: `llama-index-core`, `llama-index-embeddings-ollama` 를 `backend/requirements.txt` 에 추가 (세션2 허용 범위).
- 세션3 연결 지점 (2단계): `collect_pages` 안에서 검색 전에 `query()`+`is_sufficient()`, 본문 추출 뒤 `upsert_pages()`.
  1단계(색인만)는 `upsert_pages` 한 줄이면 끝. `config.yaml research.vectorstore.enabled: false` 가 기본 — 켜는 건 사용자.

## 6. 이 설계가 해결 안 하는 것

- 출처 날조(모델이 없는 통계를 지어냄) — 모델 결정(WORKBOARD #3)의 몫
- 블로그·도메인명 출처 문제 — publisher 검증은 별도 (PROGRESS "미해결" 참조)
