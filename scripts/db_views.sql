-- 모창봇 초안 DB 운영자용 조인 뷰 (2026-09-03). 읽기 전용 — 서비스 코드는 이 뷰를 모른다.
-- DB Browser(바탕화면 바로가기, 읽기 전용) → Browse Data 탭 → 테이블 목록에서 v_progress / v_outputs 선택.
--   v_progress: 초안 1건 = 1행. 아이디어·팀·역량·카드 답(줄바꿈)·완료 문항 수·문항별 글자수.
--   v_outputs : 생성문 1건 = 1행. 초안 아이디어 + 문항 이름 + 자동(마무리 작업자가 만든 것) + 글자수 + 생성 시각 + 본문.
--   v_research: 조사 1건 = 1행 (2026-09-03 research 테이블). 아이디어 + 문항 + 사실 수 + 검색어 + 사실 목록 JSON.
-- 다시 만들기(DB 를 새로 만들었을 때):  .venv/Scripts/python -c "import sqlite3;sqlite3.connect('backend/.data/mochang.sqlite').executescript(open('scripts/db_views.sql',encoding='utf-8').read())"
-- PostgreSQL 로 옮기면 json_each/substr 문법을 손봐야 한다.
DROP VIEW IF EXISTS v_outputs;
CREATE VIEW v_outputs AS
SELECT
  substr(d.updated_at,6,11)  AS 초안갱신,
  substr(d.idea,1,60)        AS 아이디어,
  g.question_id              AS 문항,
  CASE g.question_id
    WHEN 'q1' THEN '한줄 소개' WHEN 'q2' THEN '배경 이야기' WHEN 'q3_1' THEN '차별점·문제'
    WHEN 'q3_2' THEN '수익 계획' WHEN 'q4_1' THEN '사업화 계획' WHEN 'q4_2' THEN '멘토 도움'
    WHEN 'q7_1' THEN '기존 사업과 차이' WHEN 'q8' THEN '본인 역량' WHEN 'q10' THEN '홍보 문구' ELSE g.question_id END AS 문항이름,
  g.kind                     AS 종류,
  CASE WHEN json_valid(g.meta) AND json_extract(g.meta,'$.auto') THEN '서버 자동' ELSE '' END AS 자동,
  g.chars                    AS 글자수,
  substr(g.created_at,12,8)  AS 생성시각,
  g.text                     AS 생성문,
  d.draft_id
FROM generations g JOIN drafts d ON d.draft_id=g.draft_id
ORDER BY d.updated_at DESC,
  CASE g.question_id WHEN 'q1' THEN 1 WHEN 'q2' THEN 2 WHEN 'q3_1' THEN 3 WHEN 'q3_2' THEN 4
                     WHEN 'q4_1' THEN 5 WHEN 'q4_2' THEN 6 WHEN 'q7_1' THEN 7 WHEN 'q8' THEN 8 WHEN 'q10' THEN 9 ELSE 10 END, g.id;

DROP VIEW IF EXISTS v_progress;
CREATE VIEW v_progress AS
SELECT
  substr(d.created_at,6,11)  AS 시작,
  substr(d.updated_at,12,5)  AS 마지막,
  d.draft_id,
  d.idea                     AS 아이디어,
  d.team                     AS 팀,
  d.capability               AS 역량,
  (SELECT group_concat(json_extract(a.value,'$.label') || ': ' ||
          coalesce((SELECT group_concat(b.value,' / ') FROM json_each(json_extract(a.value,'$.answer')) b), '모름'), char(10))
     FROM json_each(d.answers) a) AS 카드답,
  (SELECT count(DISTINCT question_id) FROM generations g WHERE g.draft_id=d.draft_id AND g.kind='generate') AS 완료문항,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q1')   AS q1,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q2')   AS q2,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q3_1') AS q3_1,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q3_2') AS q3_2,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q4_1') AS q4_1,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q4_2') AS q4_2,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q8')   AS q8,
  (SELECT max(chars) FROM generations g WHERE g.draft_id=d.draft_id AND g.question_id='q10')  AS q10,
  d.request_count            AS 요청수,
  d.owner                    AS IP
FROM drafts d
ORDER BY d.updated_at DESC;

DROP VIEW IF EXISTS v_research;
CREATE VIEW v_research AS
SELECT substr(r.created_at,6,11) AS 조사시각, substr(d.idea,1,50) AS 아이디어, r.question_id AS 문항, r.facts_count AS 사실수,
       r.backend AS 검색백엔드, r.cached AS 캐시적중, r.queries AS 검색어, r.facts AS 사실목록, r.pages AS 페이지, r.draft_id
FROM research r JOIN drafts d ON d.draft_id = r.draft_id
ORDER BY r.id DESC;
