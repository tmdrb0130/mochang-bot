// 백엔드(FastAPI) 호출 모듈. 주소는 frontend/.env 의 VITE_API_BASE 로 바꿀 수 있음 (기본 http://localhost:8000).
export const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000";

// 프론트 form(camelCase) → 백엔드 스키마(snake_case). model 은 선택(없으면 백엔드 기본 모델).
export function toPayload(form) {
  return {
    track: form.track,
    idea: form.idea,
    is_business: form.isBusiness,
    current_item: form.currentItem || "",
    team: form.team,
    capability: form.capability || "",
    ...(form.model ? { model: form.model } : {}),
    ...(form.answers?.length ? { answers: form.answers } : {}),
    // 신청서 한 벌을 묶는 키 — 서버가 아이디어·인테이크 답·문항별 초안을 이 id 로 저장한다 (backend/storage.py).
    ...(form.draftId ? { draft_id: form.draftId } : {}),
  };
}

/** 초안 id. 서버 형식 [A-Za-z0-9_-]{8,64}. randomUUID 가 없는 오래된 브라우저·http 환경은 시각+난수로 대신. */
export function newDraftId() {
  try {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
  } catch { /* 비보안 컨텍스트 */ }
  return `d${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

async function request(path, options) {
  const res = await fetch(`${API_BASE}${path}`, options);
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json()).detail || ""; } catch { /* 본문 없음 */ }
    const err = new Error(detail || `요청 실패 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function post(path, body) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** 아이디어 인테이크 → { summary, slots[{id,label,status,known}], cards[{slot,label,type,question,why,question_ids,options[{label,hint}]}], ready, model, research }
 *  2026-09-03: 동기 POST /intake 에서 작업 큐(/jobs/intake)로. 40명 실측에서 인테이크가 10분(nginx 상한)을 넘겨 504 → "조사 실패" 가 됐다.
 *  큐 방식은 요청이 짧아 타임아웃이 없고, opts.onTick 으로 대기 순번을 보여줄 수 있다. 응답 형태는 동기와 같다. */
export function intake(form, opts) {
  return runJob("intake", toPayload(form), opts);
}

/** 카드 재생성: 보기가 안 맞는 슬롯만 다시 → { cards[슬롯별 새 카드], slots, model, error? }
 *  seen: { slot: [이미 보여준 보기 label] } — 같은 보기는 백엔드가 걸러냄. keep: { slot: [{label,hint}] } 이미 고른 보기(유지). note: 메모(선택). */
export function intakeRegenerate(form, slots, seen, note = "", keep = {}) {
  return post("/intake/regenerate", { ...toPayload(form), slots, seen, note, keep });
}

/** 웹 조사: 검색어 생성 → 검색 → 본문 추출 → 출처 검증된 사실 목록.
 *  백엔드의 조사 전용 모델(config.yaml research.llm — 지금은 로컬 라마 70B)이 처리한다.
 *  → { question_id, queries, backend, result_count, pages[{url,title}], facts[{fact,quote,source_title,url,date,publisher,use_for}], references, cached }
 *  결과의 facts 를 generate/extend 의 references 로 넘기면 [웹 참고자료] 로 주입된다. */
export function research(form, questionId, opts) {
  return runJob("research", { ...toPayload(form), question_id: questionId }, opts);   // 2026-09-03: 인테이크와 같은 이유로 큐로
}

// ───────────────────────── 작업 큐 (비동기 제출 → 폴링) ─────────────────────────
// 생성·이어쓰기는 30~60초 걸린다. 동기 POST 로 연결을 붙잡으면 사람이 몰릴 때 프록시·브라우저 타임아웃에 걸리고
// 대기 순번도 알 수 없다 → POST /jobs/{kind} 로 job_id 를 받고 GET /jobs/{job_id} 를 폴링한다.
// job_id 만 있으면 새로고침 뒤에도 같은 작업을 이어받을 수 있다 (App.jsx 가 sessionStorage 에 보관).

/** 이어받으려던 작업이 서버에 없을 때 (서버 재시작, 히스토리 500건 초과로 밀려남). */
export class JobExpiredError extends Error {
  constructor(message = "작업 정보가 서버에서 사라졌습니다 (만료 또는 재시작).") {
    super(message);
    this.name = "JobExpiredError";
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 큐에 제출 → { job_id, kind, position, queue } */
export function submitJob(kind, body) {
  return post(`/jobs/${kind}`, body);
}

/** 작업 상태 → { job_id, kind, status: queued|running|done|error, position, queued_seconds, elapsed_seconds, result, error } */
export function jobStatus(jobId) {
  return request(`/jobs/${jobId}`);
}

// 제출이 429(동시 작업 제한)로 막히면 잠시 뒤 다시 시도. 백엔드는 IP 당 동시 3건까지 허용한다.
const BUSY_RETRY = 12;
const BUSY_DELAY = 5000;
// 폴링 중 네트워크가 잠깐 끊기는 것으로 진행 중인 작업을 버리지 않는다.
const POLL_FAILS_ALLOWED = 3;

/** 큐에 넣고 끝날 때까지 폴링해 result 를 돌려준다.
 *  onSubmit(jobId): 제출 직후 (새로고침 대비 저장용) / onTick(snap): 폴링할 때마다 (대기 순번 표시용).
 *  실패한 작업은 Error, 이어받기가 불가능하면 JobExpiredError 를 던진다. */
export async function runJob(kind, body, { onSubmit, onTick, interval = 2500 } = {}) {
  let submitted;
  for (let i = 0; ; i++) {
    try {
      submitted = await submitJob(kind, body);
      break;
    } catch (e) {
      if (e.status !== 429 || i >= BUSY_RETRY) throw e;
      onTick?.({ status: "queued", position: null, busy: true });   // "다른 작업이 끝나기를 기다리는 중"
      await sleep(BUSY_DELAY);
    }
  }
  onSubmit?.(submitted.job_id);
  onTick?.({ status: "queued", position: submitted.position });
  return followJob(submitted.job_id, { onTick, interval });
}

/** 이미 제출된 작업을 끝까지 폴링한다 (새로고침 후 이어받기). */
export async function followJob(jobId, { onTick, interval = 2500 } = {}) {
  let fails = 0;
  for (;;) {
    await sleep(interval);
    let snap;
    try {
      snap = await jobStatus(jobId);
    } catch (e) {
      if (e.status === 404) throw new JobExpiredError();
      if (++fails > POLL_FAILS_ALLOWED) throw e;     // 연속 실패가 아니면 계속 시도
      continue;
    }
    fails = 0;
    onTick?.(snap);
    if (snap.status === "done") return snap.result;
    if (snap.status === "error") throw new Error(snap.error || "작업이 실패했습니다.");
  }
}

/** 문항 하나 생성 → { question_id, style, text, length, limit, model }
 *  references: /research 의 facts (없으면 조사 없이 생성). opts 는 runJob 과 같다. */
export function generate(form, questionId, styleId, references = null, opts) {
  return runJob("generate", { ...toPayload(form), question_id: questionId, style: styleId, ...(references?.length ? { references } : {}) }, opts);
}

/** 기존 글 뒤에 이어쓰기 → { text(합친 전체), added, length, limit, model } */
export function extend(form, questionId, styleId, current, references = null, opts) {
  return runJob("extend", { ...toPayload(form), question_id: questionId, style: styleId, current, ...(references?.length ? { references } : {}) }, opts);
}

/** 서버 상태 → { ok, model, base_url, fallback, usage: { used, limit, remaining, reset } } */
export function health() {
  return request("/health");
}

/** 선택 가능한 모델 → { default, models: [{ id, name, note }] } */
export function models() {
  return request("/models");
}
